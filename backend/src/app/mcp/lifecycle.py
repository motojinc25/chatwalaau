"""MCP lifecycle management (CTR-0061, PRP-0031).

Manages startup and shutdown of MCP tool instances tied to the FastAPI
application lifecycle. Uses AsyncExitStack to properly enter and exit
each MCP tool's async context manager. Registers atexit and SIGTERM
handlers for zombie process prevention.

PRP-0242 (UDR-0223 D6): a server saved as ``"disabled": true`` is parsed but not
connected (a disabled stdio server starts no process), and the override store is
filled from the file on every startup and reload. ``probe_mcp_server`` connects a
candidate entry alone for the screen's Test button and always closes it.
"""

import asyncio
import atexit
from contextlib import AsyncExitStack
import logging
from pathlib import Path
import signal
import sys

from app.core.config import settings
from app.mcp.config import MCPServerConfig, parse_mcp_config
from app.mcp.provider import create_mcp_tool

logger = logging.getLogger(__name__)

# Module-level state for MCP tools and cleanup
_mcp_exit_stack: AsyncExitStack | None = None
_mcp_tools: list = []
_mcp_raw_tools: list = []  # unstarted tool instances for emergency cleanup
_mcp_server_status: list[dict] = []  # status info for API endpoint
# Serialises a full reload (re-parse + reconnect + swap) so two concurrent Reloads
# never interleave their module-state swaps (PRP-0090, UDR-0068 D3/D7). Constructed
# without a running loop (binds lazily on Python 3.12) since this is import-time.
_reload_lock = asyncio.Lock()


def get_mcp_tools() -> list:
    """Return the list of prepared MCP tool instances.

    Called by agent_factory.py to include MCP tools in Agent(tools=...).
    Returns tool instances created by prepare_mcp(). These are the same
    objects that activate_mcp() later enters as async context managers,
    so the Agent holds references to the live tools.
    Returns empty list if MCP is not configured.
    """
    return list(_mcp_tools)


def get_mcp_status() -> list[dict]:
    """Return the status of all configured MCP servers.

    Called by the /api/mcp/status endpoint to display MCP state in AG-UI.
    """
    return list(_mcp_server_status)


def get_mcp_server_names() -> list[str]:
    """Return the names of prepared MCP servers.

    Called by agent_factory.py to build MCP-specific instructions.
    """
    return [s["name"] for s in _mcp_server_status]


def _function_entries(tool: object) -> list[tuple[str, str]]:
    """Return (name, description) for every function a connected MCP tool exposes.

    Reads the MCP tool's loaded function list. Before ``activate_mcp()`` (or for a
    server that failed to connect) the list is empty, so the inventory simply shows
    a server with no tools. The names returned here are the tool's EXPOSED names,
    which is exactly what ``MCPTool.allowed_tools`` matches against (CTR-0060,
    UDR-0064 D3).
    """
    funcs = getattr(tool, "_functions", None) or []
    out: list[tuple[str, str]] = []
    for f in funcs:
        name = getattr(f, "name", None)
        if not name:
            continue
        out.append((name, getattr(f, "description", "") or ""))
    return out


def get_server_tool_names(server_name: str) -> list[str]:
    """Return the exposed tool names for one prepared server (CTR-0061).

    Used by the agent factory to compute the enabled SUBSET for
    ``MCPTool.allowed_tools`` (a whitelist) from the disabled set held in the
    override store (UDR-0064 D3).
    """
    for tool in _mcp_tools:
        if getattr(tool, "name", None) == server_name:
            return [name for name, _ in _function_entries(tool)]
    return []


def _configured_server_entries() -> list[dict]:
    """Best-effort re-parse of the MCP config for the inventory (PRP-0090, UDR-0068 D4).

    Returns ``[{name, transport, disabled}]`` for every server currently declared in
    the config file. Used to surface servers that are not connected -- ADDED since
    startup, saved as disabled (PRP-0242), or naming an unset ``${VAR}`` -- as
    ``loaded=false`` rows. Values are not resolved here, so a server with an unset
    variable is still listed. Any parse problem yields an empty list so the inventory
    degrades to the connected-only view.
    """
    try:
        from app.mcp.config import parse_mcp_document
        from app.mcp.config_store import read_document

        doc = read_document()
        if doc.read_path is None or doc.error:
            return []
        server_configs = parse_mcp_document(doc.data, resolve=False, source=str(doc.read_path))
        return [{"name": sc.name, "transport": sc.transport, "disabled": sc.disabled} for sc in server_configs]
    except Exception:
        logger.debug("MCP config re-parse for inventory failed", exc_info=True)
        return []


def get_mcp_config_path() -> str:
    """Return the resolved MCP config path as a string (or "") for the UI empty state."""
    path = _resolve_mcp_config_path()
    return str(path) if path is not None else ""


def get_mcp_tool_inventory() -> list[dict]:
    """Return the MCP tool inventory with current enabled/disabled + loaded state (CTR-0121).

    For each CONNECTED/prepared server: name, transport, connection status, the
    server-level enabled flag, a ``loaded`` flag (UDR-0068 D4: true iff the server is
    currently connected), and its tools (name, description when available, per-tool
    enabled flag, and the same ``loaded`` flag). State is read from the in-memory
    override store (CTR-0061, UDR-0064 D4).

    The inventory is then UNIONED with servers declared in the config but NOT currently
    connected: they appear as ``loaded=false`` rows with no tools, with status
    ``disabled`` for a server saved as disabled (PRP-0242, UDR-0223 D6; enabling it
    reconnects it) and ``not_loaded`` otherwise (added since startup -- Reload connects
    it; PRP-0090, UDR-0068 D4).
    """
    from app.mcp.overrides import get_override_store

    store = get_override_store()
    inventory: list[dict] = []
    seen: set[str] = set()
    for i, tool in enumerate(_mcp_tools):
        status_info = _mcp_server_status[i] if i < len(_mcp_server_status) else {}
        server_name = status_info.get("name") or getattr(tool, "name", f"server-{i}")
        seen.add(server_name)
        status = status_info.get("status", "")
        loaded = status == "connected"
        tools = [
            {
                "name": tname,
                "description": tdesc,
                "enabled": store.is_tool_enabled(server_name, tname),
                "loaded": loaded,
            }
            for tname, tdesc in _function_entries(tool)
        ]
        inventory.append(
            {
                "name": server_name,
                "transport": status_info.get("transport", ""),
                "status": status,
                "enabled": not store.server_disabled(server_name),
                "loaded": loaded,
                "tools": tools,
            }
        )

    for entry in _configured_server_entries():
        if entry["name"] in seen:
            continue
        inventory.append(
            {
                "name": entry["name"],
                "transport": entry.get("transport", ""),
                "status": "disabled" if entry.get("disabled") else "not_loaded",
                "enabled": not entry.get("disabled") and not store.server_disabled(entry["name"]),
                "loaded": False,
                "tools": [],
            }
        )
    return inventory


def _resolve_mcp_config_path() -> Path | None:
    """Resolve the MCP config file path with two-tier fallback (PRP-0060).

    Resolution order:
      1. ``MCP_CONFIG_FILE`` empty -> MCP disabled (explicit opt-out).
      2. Configured path if it exists -> use it (operator override).
      3. Derived ``<stem>.default<suffix>`` sibling if it exists ->
         use bundled default with an INFO log nudge.
      4. Otherwise INFO log "MCP integration disabled" and return None.
    """
    raw = (settings.mcp_config_file or "").strip()
    if not raw:
        logger.info("MCP integration disabled (MCP_CONFIG_FILE explicitly empty)")
        return None
    override = Path(raw)
    if not override.is_absolute():
        override = Path.cwd() / override
    if override.is_file():
        return override
    default = override.with_name(f"{override.stem}.default{override.suffix}")
    if default.is_file():
        logger.info(
            "Using bundled MCP defaults at %s. Create %s to override.",
            default,
            override.name,
        )
        return default
    logger.info(
        "MCP integration disabled (no %s or %s found)",
        override.name,
        default.name,
    )
    return None


def _seed_selection(server_configs: list[MCPServerConfig]) -> None:
    """Fill the override store from the selection saved in the file (UDR-0223 D6)."""
    from app.mcp.overrides import get_override_store

    get_override_store().seed_from_configs(server_configs)


def _enabled(server_configs: list[MCPServerConfig]) -> list[MCPServerConfig]:
    """The servers to connect: a server saved as disabled is never connected (UDR-0223 D6)."""
    out = []
    for server_config in server_configs:
        if server_config.disabled:
            logger.info("MCP server '%s' is disabled in the config; not connecting it", server_config.name)
        else:
            out.append(server_config)
    return out


async def probe_mcp_server(server_config: MCPServerConfig, timeout: float) -> list[tuple[str, str]]:
    """Connect one candidate server alone, list its tools, and close it (CTR-0278 test).

    Touches neither the config file nor the live tool list. The candidate is closed on
    every outcome, so a stdio test never leaves a process behind. Raises on a failed or
    timed-out connection.
    """
    tool = create_mcp_tool(server_config)
    try:
        await asyncio.wait_for(tool.connect(), timeout=timeout)
        return _function_entries(tool)
    finally:
        try:
            await asyncio.wait_for(tool.close(), timeout=10)
        except Exception:
            logger.warning("MCP test connection for '%s' did not close cleanly", server_config.name, exc_info=True)


def prepare_mcp() -> None:
    """Synchronous phase: parse config and create MCP tool instances.

    Called at module level (before agent creation) so that get_mcp_tools()
    returns tool instances for Agent(tools=...) registration.
    The tool instances are created but NOT yet started (async context
    managers not entered). activate_mcp() must be called later in the
    FastAPI lifespan to actually start the servers.

    Execution order:
      1. prepare_mcp()       -- synchronous, module level
      2. create_agent()      -- synchronous, module level (uses get_mcp_tools())
      3. activate_mcp()      -- async, FastAPI lifespan startup
    """
    config_path = _resolve_mcp_config_path()
    if config_path is None:
        return

    server_configs = parse_mcp_config(config_path)
    _seed_selection(server_configs)
    if not server_configs:
        logger.warning("No valid MCP server entries found in %s", config_path)
        return

    for server_config in _enabled(server_configs):
        tool = create_mcp_tool(server_config)
        _mcp_tools.append(tool)
        _mcp_raw_tools.append(tool)
        _mcp_server_status.append(
            {
                "name": server_config.name,
                "transport": server_config.transport,
                "status": "prepared",
            }
        )
        logger.info("MCP tool prepared: %s (transport=%s)", server_config.name, server_config.transport)

    logger.info("MCP preparation complete: %d tool(s) ready for activation", len(_mcp_tools))


async def activate_mcp() -> None:
    """Async phase: enter async context managers to start MCP servers.

    Called in FastAPI lifespan after prepare_mcp() and create_agent().
    Enters the async context manager for each prepared tool instance,
    which starts stdio subprocesses and establishes HTTP connections.
    The Agent already holds references to these same tool objects.
    """
    global _mcp_exit_stack

    if not _mcp_tools:
        return

    _mcp_exit_stack = AsyncExitStack()
    started_count = 0

    for i, tool in enumerate(_mcp_tools):
        # PRP-0046: the former _patch_load_prompts workaround was removed.
        # Operators declare "load_prompts": false in mcp_servers.json for
        # servers that implement only tools (e.g. filesystem, GitHub),
        # and MAF's native flag skips the prompts/list probe entirely.
        try:
            await _mcp_exit_stack.enter_async_context(tool)
            started_count += 1
            _mcp_server_status[i]["status"] = "connected"
            logger.info("MCP server started: %s", _mcp_server_status[i]["name"])
        except Exception:
            _mcp_server_status[i]["status"] = "error"
            logger.exception("Failed to start MCP server: %s", _mcp_server_status[i]["name"])

    if started_count > 0:
        logger.info("MCP integration ready: %d/%d servers started", started_count, len(_mcp_tools))
    else:
        logger.warning("MCP integration: no servers started successfully")

    # Start MCP Apps sandbox proxy server (CTR-0066, PRP-0034). It listens on the
    # same address as the app (PRP-0240, UDR-0019 D15): a browser that reaches the app
    # from another machine reaches the sandbox on the page's host too.
    if started_count > 0:
        from app.mcp_apps.sandbox import start_sandbox_server

        start_sandbox_server(settings.mcp_apps_sandbox_port, host=settings.app_host or "127.0.0.1")

    # Register emergency cleanup handlers
    _register_cleanup_handlers()

    # Discover UI-enabled MCP tools for MCP Apps (CTR-0067, PRP-0034)
    from app.mcp_apps.manager import discover_ui_tools

    await discover_ui_tools(_mcp_tools, _mcp_server_status)


async def reload_mcp() -> None:
    """Re-parse the config and reconnect all MCP servers (PRP-0090, UDR-0068 D3).

    A FULL reload that picks up out-of-band edits to ``mcp_servers.jsonc`` (added,
    removed, or changed servers) without a restart. Uses new-before-teardown so a
    concurrent request is never left without MCP tools mid-swap:

      1. Re-parse the config and create fresh tool instances.
      2. CONNECT them on a NEW AsyncExitStack (per-server failures isolated).
      3. Atomically swap the module-level tool list / status (each ``get_mcp_tools()``
         caller copies the list, so subsequent agent builds see the new tools).
      4. Re-discover MCP Apps UI tools against the new connections.
      5. Close the OLD AsyncExitStack LAST -- an in-flight request still holding an old
         agent finishes against the old connection on a best-effort basis.

    The caller (CTR-0121 reload endpoint) rebuilds the AgentRegistry AFTER this so the
    per-model agents bind to the new tools. Serialised by ``_reload_lock`` so two
    Reloads never interleave their swaps.
    """
    global _mcp_exit_stack

    async with _reload_lock:
        config_path = _resolve_mcp_config_path()
        new_tools: list = []
        new_raw: list = []
        new_status: list[dict] = []
        server_configs = parse_mcp_config(config_path) if config_path is not None else []
        _seed_selection(server_configs)
        for server_config in _enabled(server_configs):
            tool = create_mcp_tool(server_config)
            new_tools.append(tool)
            new_raw.append(tool)
            new_status.append(
                {
                    "name": server_config.name,
                    "transport": server_config.transport,
                    "status": "prepared",
                }
            )

        new_stack = AsyncExitStack()
        started = 0
        for i, tool in enumerate(new_tools):
            try:
                await new_stack.enter_async_context(tool)
                new_status[i]["status"] = "connected"
                started += 1
                logger.info("MCP server reconnected: %s", new_status[i]["name"])
            except Exception:
                new_status[i]["status"] = "error"
                logger.exception("Failed to reconnect MCP server: %s", new_status[i]["name"])

        # Atomic swap: rebind module state before tearing the old stack down.
        old_stack = _mcp_exit_stack
        _mcp_exit_stack = new_stack
        _mcp_tools[:] = new_tools
        _mcp_raw_tools[:] = new_raw
        _mcp_server_status[:] = new_status

        # Re-discover MCP Apps UI tools against the new connections (clears its own
        # caches first, so this is safe to re-run).
        try:
            from app.mcp_apps.manager import discover_ui_tools

            await discover_ui_tools(_mcp_tools, _mcp_server_status)
        except Exception:
            logger.exception("MCP Apps UI re-discovery failed during reload")

        # Close the OLD stack last (new-before-teardown).
        if old_stack is not None:
            try:
                await old_stack.aclose()
            except Exception:
                logger.exception("Error closing previous MCP exit stack during reload")

        logger.info("MCP reload complete: %d/%d server(s) connected", started, len(new_tools))


async def shutdown_mcp() -> None:
    """Stop all MCP servers gracefully.

    Closes the AsyncExitStack which triggers __aexit__ on each
    MCP tool instance, terminating stdio subprocesses and closing
    HTTP connections.
    """
    global _mcp_exit_stack

    # Stop MCP Apps sandbox proxy (CTR-0066)
    from app.mcp_apps.sandbox import stop_sandbox_server

    stop_sandbox_server()

    if _mcp_exit_stack:
        try:
            await _mcp_exit_stack.aclose()
            logger.info("All MCP servers stopped")
        except Exception:
            logger.exception("Error during MCP server shutdown")
        finally:
            _mcp_exit_stack = None
            _mcp_tools.clear()
            _mcp_raw_tools.clear()
            _mcp_server_status.clear()


def _emergency_cleanup() -> None:
    """Synchronous cleanup for abnormal termination (atexit handler).

    Attempts to terminate any child processes owned by stdio MCP tools.
    This is a best-effort backup for when the async shutdown path is
    not executed (e.g., SIGTERM without graceful shutdown).
    """
    for tool in _mcp_raw_tools:
        # MCPStdioTool may have a _process or similar attribute
        proc = getattr(tool, "_process", None)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass


def _register_cleanup_handlers() -> None:
    """Register atexit and SIGTERM handlers for zombie prevention."""
    atexit.register(_emergency_cleanup)

    # SIGTERM handler triggers sys.exit(0) which fires atexit handlers
    original_handler = signal.getsignal(signal.SIGTERM)

    def _sigterm_handler(signum: int, frame: object) -> None:
        # Call original handler if it was set
        if callable(original_handler) and original_handler not in (signal.SIG_DFL, signal.SIG_IGN):
            original_handler(signum, frame)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
