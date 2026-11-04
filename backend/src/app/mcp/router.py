"""MCP Tool Management API (CTR-0121) and MCP Server Configuration API (CTR-0278).

CTR-0121 (PRP-0086, PRP-0090, UDR-0064 / UDR-0068) gates the active MCP tool set:

    GET  /api/mcp/tools   -- inventory of configured MCP servers and their tools,
                             with each tool's / server's current enabled state.
    PUT  /api/mcp/tools   -- apply an enable/disable selection: save it in the config
                             file (PRP-0242, UDR-0223 D6), then rebuild all per-model
                             agents (CTR-0070) -- or reconnect through the reload when a
                             server is turned on or off, since a disabled server is not
                             connected -- answering only after that completes.
    POST /api/mcp/reload  -- re-parse the config, reconnect, rebuild the agents.

CTR-0278 (PRP-0242, UDR-0223) manages the server entries themselves:

    GET    /api/mcp/servers         -- entries (literal secrets masked), env_status,
                                       revision, and whether this request may edit stdio.
    POST   /api/mcp/servers         -- add a server.
    PUT    /api/mcp/servers/{name}  -- replace (and optionally rename) a server.
    DELETE /api/mcp/servers/{name}  -- remove a server.
    POST   /api/mcp/servers/test    -- connect a candidate alone and list its tools.

Every write validates with the loader's parser, writes the file atomically (keeping
``.bak``) and ends with the same reload as POST /api/mcp/reload; it answers
``{"tools": <the CTR-0121 inventory>, "config": <the GET /api/mcp/servers view>}``. Adding, editing or
testing a stdio server runs a command on this machine, so it is allowed only from a
local request unless ``MCP_STDIO_REMOTE_EDIT=true`` (UDR-0223 D2 / D3).

All endpoints are gated by CTR-0083 (``verify_api_key``) with the loopback bypass.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import is_local_request, verify_api_key
from app.core.config import settings
from app.mcp import config_store
from app.mcp.config import UnresolvedVariableError, parse_mcp_document
from app.mcp.lifecycle import get_mcp_config_path, get_mcp_tool_inventory
from app.mcp.overrides import get_override_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["MCP"])

#: The default bound of a Test connection when the entry sets no request_timeout.
DEFAULT_TEST_TIMEOUT = 30

STDIO_LOCKED_MESSAGE = (
    "stdio servers can be added, edited or tested only on this machine "
    "(set MCP_STDIO_REMOTE_EDIT=true in .env to allow it from elsewhere)"
)


class ToolSelection(BaseModel):
    """Desired enabled state for a single MCP tool."""

    name: str = Field(..., min_length=1, max_length=256)
    enabled: bool = True


class ServerSelection(BaseModel):
    """Desired enabled state for a server and (optionally) its tools."""

    name: str = Field(..., min_length=1, max_length=256)
    enabled: bool = True
    tools: list[ToolSelection] = Field(default_factory=list)


class McpSelection(BaseModel):
    """The full desired selection submitted by the management UI."""

    servers: list[ServerSelection] = Field(default_factory=list)


class ServerWrite(BaseModel):
    """An add or replace from the screen (CTR-0278). ``name`` renames on a replace."""

    name: str | None = Field(default=None, max_length=64)
    entry: dict[str, Any]
    # The revision GET returned; a mismatch is 409. Omitted (scripts) skips the check.
    revision: str | None = None


class ServerTest(BaseModel):
    """A candidate to test. ``name`` names the stored server whose literal values a marker keeps."""

    name: str | None = Field(default=None, max_length=64)
    entry: dict[str, Any]


def stdio_editable(request: Request) -> bool:
    """Whether this request may add, edit or test stdio servers (UDR-0223 D2)."""
    return bool(settings.mcp_stdio_remote_edit) or is_local_request(request)


def _require_stdio_edit(request: Request) -> None:
    if not stdio_editable(request):
        raise HTTPException(status_code=403, detail={"error": "stdio_not_local", "message": STDIO_LOCKED_MESSAGE})


def _http_error(exc: config_store.McpConfigError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"error": exc.code, "message": exc.message})


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def register_mcp_management(app: FastAPI, *, agent_registry) -> None:
    """Mount the MCP management endpoints, closing over the AgentRegistry.

    The router needs the live ``AgentRegistry`` (CTR-0070) so a change can rebuild it;
    the registry is the module-level singleton created in ``app.main`` and shared by
    reference with the AG-UI / OpenAI-API endpoints (UDR-0064 D2).
    """
    # Serialises config writes so two saves never interleave read-modify-write.
    write_lock = asyncio.Lock()

    async def _rebuild_agents() -> None:
        # Local import avoids a module-level import cycle (agent_factory -> app.mcp.* -> ...).
        from app.agui.agent_factory import rebuild_agent_registry

        await rebuild_agent_registry(agent_registry)

    async def _clear_harness_cache(reason: str) -> None:
        # Harness conversations hold the MCP tools they were built with (CTR-0193).
        from app.agent.harness.runtime import clear_cache

        await clear_cache(reason=reason)

    async def _reload_and_rebuild() -> None:
        """The full reload (UDR-0068 D3): reconnect, rebuild, prune, clear the harness cache."""
        from app.mcp.lifecycle import reload_mcp

        try:
            await reload_mcp()
            await _rebuild_agents()
        except Exception:
            logger.exception("MCP reload failed")
            raise HTTPException(status_code=500, detail={"error": "mcp_reload_failed"}) from None
        _prune_mcp_overrides()
        await _clear_harness_cache("mcp_reload")
        logger.info("MCP reloaded from config: %d server(s) in inventory", len(get_mcp_tool_inventory()))

    def _inventory() -> dict:
        return {"servers": get_mcp_tool_inventory(), "config_path": get_mcp_config_path()}

    # ---------------------------------------------------------------- CTR-0121

    @router.get("/tools", dependencies=[Depends(verify_api_key)])
    async def list_mcp_tools() -> dict:
        """Return the MCP tool inventory with current enabled/disabled state."""
        return _inventory()

    @router.put("/tools", dependencies=[Depends(verify_api_key)])
    async def apply_mcp_tools(body: McpSelection) -> dict:
        """Save a selection, apply it, and return the refreshed inventory (UDR-0223 D6).

        The selection is written to the config file first. Turning a server on or off
        reconnects through the reload (a disabled server is not connected); a tool-only
        change updates the store and rebuilds the agents. If that rebuild fails, the
        store and the file are rolled back so they match the prior agents that are still
        serving (UDR-0064 D5).
        """
        disabled_servers = {s.name for s in body.servers if not s.enabled}
        # A fully-disabled server's per-tool detail is irrelevant; only collect the
        # disabled tools of the servers that remain enabled.
        disabled_tools = {s.name: {t.name for t in s.tools if not t.enabled} for s in body.servers if s.enabled}

        store = get_override_store()
        prior = store.snapshot()
        async with write_lock:
            doc = config_store.read_document()
            prior_data = copy.deepcopy(doc.data)
            server_change = False
            persisted = False
            if doc.target_path is not None and not doc.error and doc.servers:
                for selection in body.servers:
                    entry = doc.servers.get(selection.name)
                    if not isinstance(entry, dict):
                        continue
                    was_disabled = entry.get("disabled") is True
                    if selection.enabled == was_disabled:
                        server_change = True
                    if selection.enabled:
                        entry.pop("disabled", None)
                        # Tools are known only for a connected server; keep the saved list otherwise.
                        if selection.tools:
                            tools = sorted(disabled_tools.get(selection.name, set()))
                            if tools:
                                entry["disabled_tools"] = tools
                            else:
                                entry.pop("disabled_tools", None)
                    else:
                        entry["disabled"] = True
                if doc.data != prior_data:
                    try:
                        config_store.write_document(doc, doc.data)
                    except Exception:
                        logger.exception("Saving the MCP tool selection failed")
                        raise HTTPException(status_code=500, detail={"error": "mcp_config_write_failed"}) from None
                    persisted = True

            if server_change:
                # reload_mcp() fills the store from the file just written.
                await _reload_and_rebuild()
                return _inventory()

            store.set_selection(disabled_servers=disabled_servers, disabled_tools=disabled_tools)
            try:
                await _rebuild_agents()
            except Exception:
                store.set_selection(
                    disabled_servers=prior["disabled_servers"],  # type: ignore[arg-type]
                    disabled_tools=prior["disabled_tools"],  # type: ignore[arg-type]
                )
                if persisted:
                    try:
                        config_store.write_document(doc, prior_data)
                    except Exception:
                        logger.exception("Restoring the MCP config after a failed rebuild failed")
                logger.exception("MCP tool selection apply failed during agent rebuild")
                raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None

        await _clear_harness_cache("mcp_selection")
        logger.info(
            "MCP tool selection applied: %d disabled server(s), %d server(s) with disabled tools",
            len(disabled_servers),
            sum(1 for t in disabled_tools.values() if t),
        )
        return _inventory()

    @router.post("/reload", dependencies=[Depends(verify_api_key)])
    async def reload_mcp_tools() -> dict:
        """Re-parse mcp_servers.jsonc, reconnect servers, rebuild agents, prune overrides.

        A FULL reload (PRP-0090, UDR-0068 D3): ``reload_mcp()`` reconnects on a fresh
        AsyncExitStack (new-before-teardown), then the agents are rebuilt so they bind
        to the new tools. On any failure the prior live tools + agents remain installed
        (``reload_mcp`` swaps only on success of its own sequence; a rebuild failure
        leaves the prior agents). Overrides naming servers/tools that no longer exist
        after the reload are pruned (UDR-0068 D2).
        """
        await _reload_and_rebuild()
        return _inventory()

    def _prune_mcp_overrides() -> None:
        """Drop disabled servers/tools that no longer appear after a reload (UDR-0068 D2)."""
        store = get_override_store()
        snap = store.snapshot()
        live = {s["name"] for s in get_mcp_tool_inventory()}
        disabled_servers = {s for s in snap["disabled_servers"] if s in live}  # type: ignore[union-attr]
        disabled_tools = {s: t for s, t in snap["disabled_tools"].items() if s in live and t}  # type: ignore[union-attr]
        if disabled_servers != snap["disabled_servers"] or disabled_tools != snap["disabled_tools"]:
            store.set_selection(disabled_servers=disabled_servers, disabled_tools=disabled_tools)

    # ---------------------------------------------------------------- CTR-0278

    def _servers_view(request: Request) -> dict:
        doc = config_store.read_document()
        servers = [
            {
                "name": name,
                "transport": config_store.transport_of(entry),
                "entry": config_store.mask_entry(entry) if isinstance(entry, dict) else {},
            }
            for name, entry in doc.servers.items()
        ]
        return {
            "servers": servers,
            "env_status": config_store.env_status(doc),
            "revision": doc.revision,
            "source": doc.source,
            "config_path": str(doc.read_path or doc.target_path or ""),
            "target_path": str(doc.target_path or ""),
            "writable": doc.target_path is not None and not doc.error,
            "error": doc.error,
            "stdio_editable": stdio_editable(request),
        }

    async def _write(request: Request, mutate) -> dict:
        """Read, mutate, validate, write, reload -- one serialised step (UDR-0223 D1 / D7)."""
        async with write_lock:
            doc = config_store.read_document()
            try:
                data = copy.deepcopy(doc.data)
                mutate(doc, data)
                config_store.write_document(doc, data)
            except config_store.McpConfigError as exc:
                raise _http_error(exc) from None
            except OSError:
                logger.exception("Writing the MCP config failed")
                raise HTTPException(status_code=500, detail={"error": "mcp_config_write_failed"}) from None
            await _reload_and_rebuild()
        return {"tools": _inventory(), "config": _servers_view(request)}

    @router.get("/servers", dependencies=[Depends(verify_api_key)])
    async def list_mcp_servers(request: Request) -> dict:
        """The server entries for the editor; literal secrets leave only as a marker (UDR-0223 D5)."""
        return _servers_view(request)

    @router.post("/servers", dependencies=[Depends(verify_api_key)])
    async def add_mcp_server(body: ServerWrite, request: Request) -> dict:
        name = body.name or ""

        def mutate(doc: config_store.ConfigDocument, data: dict) -> None:
            config_store.check_writable(doc, body.revision)
            config_store.check_name(name)
            if name in data["mcpServers"]:
                raise config_store.McpConfigError(409, "name_taken", f"a server named '{name}' already exists")
            cleaned = config_store.clean_screen_entry(body.entry)
            if config_store.transport_of(cleaned) == "stdio":
                _require_stdio_edit(request)
            data["mcpServers"][name] = cleaned

        return await _write(request, mutate)

    @router.put("/servers/{name}", dependencies=[Depends(verify_api_key)])
    async def replace_mcp_server(name: str, body: ServerWrite, request: Request) -> dict:
        new_name = body.name or name

        def mutate(doc: config_store.ConfigDocument, data: dict) -> None:
            config_store.check_writable(doc, body.revision)
            servers = data["mcpServers"]
            stored = servers.get(name)
            if not isinstance(stored, dict):
                raise config_store.McpConfigError(404, "not_found", f"no server named '{name}'")
            config_store.check_name(new_name)
            if new_name != name and new_name in servers:
                raise config_store.McpConfigError(409, "name_taken", f"a server named '{new_name}' already exists")
            cleaned = config_store.clean_screen_entry(body.entry, stored)
            if "stdio" in (config_store.transport_of(stored), config_store.transport_of(cleaned)):
                _require_stdio_edit(request)
            merged = config_store.merge_entry(cleaned, stored)
            # Rebuild the map so a rename keeps the server's position in the file.
            data["mcpServers"] = {
                (new_name if k == name else k): (merged if k == name else v) for k, v in servers.items()
            }

        return await _write(request, mutate)

    @router.delete("/servers/{name}", dependencies=[Depends(verify_api_key)])
    async def remove_mcp_server(name: str, request: Request, revision: str | None = None) -> dict:
        def mutate(doc: config_store.ConfigDocument, data: dict) -> None:
            config_store.check_writable(doc, revision)
            if name not in data["mcpServers"]:
                raise config_store.McpConfigError(404, "not_found", f"no server named '{name}'")
            del data["mcpServers"][name]

        return await _write(request, mutate)

    @router.post("/servers/test", dependencies=[Depends(verify_api_key)])
    async def test_mcp_server(body: ServerTest, request: Request) -> dict:
        """Connect the candidate alone, list its tools, disconnect (no file, no live change)."""
        from app.mcp.lifecycle import probe_mcp_server

        doc = config_store.read_document()
        stored = doc.servers.get(body.name) if body.name else None
        try:
            cleaned = config_store.clean_screen_entry(body.entry, stored if isinstance(stored, dict) else None)
        except config_store.McpConfigError as exc:
            raise _http_error(exc) from None
        if "stdio" in (config_store.transport_of(cleaned), config_store.transport_of(stored)):
            _require_stdio_edit(request)
        name = body.name or "candidate"
        try:
            configs = parse_mcp_document({"mcpServers": {name: cleaned}}, resolve=False)
            # Resolve here (not in the parser) so an unset variable is reported, not skipped.
            from app.mcp.config import resolve_vars

            candidate = configs[0]
            candidate.env = {k: resolve_vars(v) for k, v in candidate.env.items()}
            candidate.headers = {k: resolve_vars(v) for k, v in candidate.headers.items()}
            if candidate.url:
                candidate.url = resolve_vars(candidate.url)
        except UnresolvedVariableError as exc:
            return {"ok": False, "tools": [], "error": f"environment variable {exc.name} is not set"}
        secrets = [*candidate.env.values(), *candidate.headers.values()]
        timeout = candidate.request_timeout or DEFAULT_TEST_TIMEOUT
        try:
            tools = await probe_mcp_server(candidate, timeout)
        except TimeoutError:
            return {"ok": False, "tools": [], "error": f"no answer within {timeout} seconds"}
        except Exception as exc:
            message = _redact(f"{type(exc).__name__}: {exc}", secrets)[:500]
            return {"ok": False, "tools": [], "error": message}
        return {"ok": True, "tools": [{"name": n, "description": d} for n, d in tools], "error": None}

    app.include_router(router)


__all__ = ["register_mcp_management", "router", "stdio_editable"]
