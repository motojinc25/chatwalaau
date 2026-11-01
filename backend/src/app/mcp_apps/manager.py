"""MCP Apps manager (CTR-0067, PRP-0034, PRP-0240).

Discovers UI-enabled MCP tools (_meta.ui), records every tool's visibility, fetches UI
Resource HTML, and provides metadata for the AG-UI endpoint and RPC bridge.

PRP-0240 (UDR-0019 D11-D14) aligns the host with the stable MCP Apps specification
(2026-01-26):

* Visibility: a tool whose ``_meta.ui.visibility`` lacks ``"model"`` is not given to the
  agent (``app_only_tool_names`` feeds the ``allowed_tools`` computation), and a View may
  call only tools whose visibility includes ``"app"`` (``is_app_callable``).
* CSP / permissions come from the ``resources/read`` content ``_meta.ui``; the tool-level
  declaration of the pre-stable draft is a deprecated fallback with a WARNING.
* The deprecated ``_meta["ui/resourceUri"]`` key is still read, with a WARNING.
* The full ``CallToolResult`` reaches the View: ``host_payload`` reads the copy MAF 1.23
  retains on the function result.
"""

import base64
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Any

from app.core.config import settings
from app.mcp import connection

logger = logging.getLogger(__name__)

RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
LEGACY_RESOURCE_URI_KEY = "ui/resourceUri"
DEFAULT_VISIBILITY = ("model", "app")

# MAF 1.23 keeps the complete MCP result (CallToolResult, protocol keys) on the
# function-result Content under this key, separate from the model-visible result.
# MAF-private: pinned by tests/invariants/test_prp0240_mcp_apps_spec.py.
HOST_PAYLOAD_KEY = "_mcp_tool_result_host_payload"


@dataclass
class UiToolMetadata:
    """UI metadata for a single MCP tool."""

    server_name: str
    tool_name: str
    resource_uri: str
    csp: dict | None = None
    permissions: dict | None = None
    visibility: list[str] = field(default_factory=lambda: list(DEFAULT_VISIBILITY))


@dataclass
class UiResourceData:
    """Fetched UI Resource data."""

    html: str
    csp: dict | None = None
    permissions: dict | None = None


# Module-level registry
_ui_tools: dict[str, UiToolMetadata] = {}  # tool_name -> metadata
_resource_cache: dict[tuple[str, str], UiResourceData] = {}  # (server, resource_uri) -> data
_tool_visibility: dict[tuple[str, str], list[str]] = {}  # (server, MCP tool name) -> visibility


def get_ui_tool_metadata(tool_name: str) -> UiToolMetadata | None:
    """Get UI metadata for a tool, if it has a UI resource."""
    return _ui_tools.get(tool_name)


def has_ui_tools() -> bool:
    """Check if any UI-enabled MCP tools are registered."""
    return len(_ui_tools) > 0


def _visibility(meta: dict) -> list[str]:
    ui = meta.get("ui") if isinstance(meta, dict) else None
    visibility = ui.get("visibility") if isinstance(ui, dict) else None
    if not isinstance(visibility, list):
        return list(DEFAULT_VISIBILITY)
    return [v for v in visibility if isinstance(v, str)]


def is_model_visible(tool_meta: dict) -> bool:
    """Check if a tool should be visible to the LLM model.

    Tools with visibility=["app"] are UI-only and are NOT given to the model.
    Default (no visibility declared): visible to both model and app.
    """
    return "model" in _visibility(tool_meta)


def _exposed_name(name: str) -> str:
    """The function name MAF exposes for an MCP tool name (its own normalization rule)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)


def app_only_tool_names(server_name: str) -> set[str]:
    """Exposed names of this server's tools that the agent must not get (UDR-0019 D11)."""
    return {
        _exposed_name(name)
        for (server, name), visibility in _tool_visibility.items()
        if server == server_name and "model" not in visibility
    }


def is_app_callable(server_name: str, tool_name: str) -> bool:
    """True when a View may call this tool: its visibility includes "app" (UDR-0019 D11)."""
    visibility = _tool_visibility.get((server_name, tool_name), list(DEFAULT_VISIBILITY))
    return "app" in visibility


def _resource_uri(name: str, meta: dict) -> str | None:
    ui = meta.get("ui")
    uri = ui.get("resourceUri") if isinstance(ui, dict) else None
    if isinstance(uri, str) and uri:
        return uri
    legacy = meta.get(LEGACY_RESOURCE_URI_KEY)
    if isinstance(legacy, str) and legacy:
        logger.warning(
            "MCP tool %s uses the deprecated _meta[%r]; use _meta.ui.resourceUri (MCP Apps 2026-01-26)",
            name,
            LEGACY_RESOURCE_URI_KEY,
        )
        return legacy
    return None


def register_ui_tool(server_name: str, tool_name: str, meta: dict) -> None:
    """Record a tool's visibility and, when it declares a UI resource, its UI metadata."""
    visibility = _visibility(meta)
    _tool_visibility[(server_name, tool_name)] = visibility
    resource_uri = _resource_uri(tool_name, meta)
    if not resource_uri:
        return

    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    _ui_tools[tool_name] = UiToolMetadata(
        server_name=server_name,
        tool_name=tool_name,
        resource_uri=resource_uri,
        csp=ui.get("csp"),
        permissions=ui.get("permissions"),
        visibility=visibility,
    )
    logger.info("MCP App UI tool registered: %s -> %s", tool_name, resource_uri)


async def discover_ui_tools(mcp_tools: list, server_configs: list) -> None:
    """Discover UI-enabled tools from activated MCP server connections.

    Inspects each MCP tool's metadata to find _meta.ui.resourceUri declarations and
    records every tool's _meta.ui.visibility. Called after activate_mcp() when MCP
    server connections are established.

    MAF's load_tools() converts MCP tools to FunctionTool instances but discards
    the _meta field, so the full definitions are listed again through the MCP
    connection adapter (app/mcp/connection.py, UDR-0221 D3).
    """
    _ui_tools.clear()
    _resource_cache.clear()
    _tool_visibility.clear()

    for i, tool in enumerate(mcp_tools):
        server_name = server_configs[i]["name"] if i < len(server_configs) else f"server_{i}"

        try:
            definitions = await connection.list_tools(tool)
        except connection.McpConnectionUnavailable as exc:
            logger.debug("No MCP connection for %s (%s), skipping UI discovery", server_name, exc)
            continue
        except Exception:
            logger.warning("Error discovering UI tools for %s", server_name, exc_info=True)
            continue

        for td in definitions:
            name = getattr(td, "name", "")
            meta = connection.meta(td)
            if name and meta:
                register_ui_tool(server_name, name, meta)

    if _ui_tools:
        logger.info("MCP Apps: %d UI-enabled tool(s) discovered", len(_ui_tools))
    else:
        logger.info("MCP Apps: no UI-enabled tools found")
    hidden = sorted(f"{s}/{n}" for (s, n), v in _tool_visibility.items() if "model" not in v)
    if hidden:
        logger.info("MCP Apps: app-only tool(s) withheld from the model: %s", ", ".join(hidden))
    _withhold_app_only(mcp_tools)


def _withhold_app_only(mcp_tools: list) -> None:
    """Narrow each shared MCP tool's ``allowed_tools`` by its app-only tools (UDR-0019 D11).

    The agents are first built before the servers connect, when no tool name is known
    and the factory leaves ``allowed_tools`` open. Discovery is the first moment the
    visibility is known, so the shared instances are narrowed here; every later agent
    build applies the same rule in the factory (``app_only_tool_names``).
    """
    for tool in mcp_tools:
        server = getattr(tool, "name", "") or ""
        hidden = app_only_tool_names(server)
        if not hidden:
            continue
        current = getattr(tool, "allowed_tools", None)
        if current is None:
            current = [getattr(f, "name", "") for f in (getattr(tool, "_functions", None) or [])]
        tool.allowed_tools = [n for n in current if n and n not in hidden]


def _tool_meta_for(resource_uri: str) -> UiToolMetadata | None:
    for tm in _ui_tools.values():
        if tm.resource_uri == resource_uri:
            return tm
    return None


async def fetch_ui_resource(tool: object, resource_uri: str, server_name: str = "") -> UiResourceData | None:
    """Fetch UI Resource HTML from an MCP server.

    Reads through the MCP connection adapter (UDR-0221 D3). The HTML, CSP and
    permissions are cached together per server and resource (UDR-0019 D14).
    Returns None if the resource cannot be fetched.
    """
    key = (server_name, resource_uri)
    cached = _resource_cache.get(key)
    if cached is not None:
        return cached

    try:
        result = await connection.read_resource(tool, resource_uri)

        if not result:
            logger.warning("UI resource not found: %s", resource_uri)
            return None

        # Extract content (handle both dict and object access patterns)
        contents = getattr(result, "contents", [])
        if not contents:
            logger.warning("UI resource has no contents: %s", resource_uri)
            return None

        content = contents[0]
        mime_type = getattr(content, "mime_type", None) or ""

        if mime_type != RESOURCE_MIME_TYPE:
            logger.warning(
                "Invalid MIME type for UI resource %s: got '%s', expected '%s'",
                resource_uri,
                mime_type,
                RESOURCE_MIME_TYPE,
            )
            return None

        # Extract HTML from text or blob content
        html = getattr(content, "text", None)
        if not html:
            blob = getattr(content, "blob", None)
            if blob:
                html = base64.b64decode(blob).decode()
        if not html:
            logger.warning("UI resource has no text or blob content: %s", resource_uri)
            return None

        # CSP / permissions come from the content-level _meta.ui (MCP Apps 2026-01-26).
        content_meta = connection.meta(content) or {}
        ui_meta = content_meta.get("ui", {})
        ui_meta = ui_meta if isinstance(ui_meta, dict) else {}
        csp = ui_meta.get("csp")
        permissions = ui_meta.get("permissions")

        # Deprecated fallback: a tool-level declaration (pre-stable draft).
        tool_meta = _tool_meta_for(resource_uri)
        if csp is None and tool_meta is not None and tool_meta.csp is not None:
            logger.warning(
                "MCP App %s declares its CSP on the tool; the stable spec reads it from the "
                "resources/read content _meta.ui (tool-level CSP is deprecated)",
                resource_uri,
            )
            csp = tool_meta.csp
        if permissions is None and tool_meta is not None:
            permissions = tool_meta.permissions
        if csp:
            logger.info("MCP App %s CSP: %s", resource_uri, csp)

        data = UiResourceData(html=html, csp=csp, permissions=permissions)
        _resource_cache[key] = data
        return data

    except Exception:
        logger.warning("Failed to fetch UI resource: %s", resource_uri, exc_info=True)
        return None


def host_payload(function_result: Any) -> dict | None:
    """The complete MCP CallToolResult MAF retained for the host, or None (UDR-0019 D12)."""
    props = getattr(function_result, "additional_properties", None)
    payload = props.get(HOST_PAYLOAD_KEY) if isinstance(props, dict) else None
    return payload if isinstance(payload, dict) else None


def store_app_html(thread_id: str, call_id: str, html: str) -> str:
    """Store MCP App HTML in the session directory.

    Returns the filename for session JSON reference.
    """
    sessions_dir = Path(settings.sessions_dir)
    app_dir = sessions_dir / thread_id
    app_dir.mkdir(parents=True, exist_ok=True)

    filename = f"mcp_app_{call_id}.html"
    file_path = app_dir / filename
    file_path.write_text(html, encoding="utf-8")

    return filename


def read_app_html(thread_id: str, filename: str) -> str | None:
    """Read stored MCP App HTML from session directory."""
    sessions_dir = Path(settings.sessions_dir)
    file_path = sessions_dir / thread_id / filename

    # Path traversal prevention
    try:
        resolved = file_path.resolve()
        if not resolved.is_relative_to(sessions_dir.resolve()):
            return None
    except (ValueError, OSError):
        return None

    if file_path.is_file():
        return file_path.read_text(encoding="utf-8")
    return None
