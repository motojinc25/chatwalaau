"""MCP Apps manager (CTR-0067, PRP-0034).

Discovers UI-enabled MCP tools (_meta.ui), fetches UI Resource HTML,
and provides metadata for the AG-UI endpoint and RPC bridge.
"""

import base64
from dataclasses import dataclass
import logging
from pathlib import Path

from app.core.config import settings
from app.mcp import connection

logger = logging.getLogger(__name__)

RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"


@dataclass
class UiToolMetadata:
    """UI metadata for a single MCP tool."""

    server_name: str
    tool_name: str
    resource_uri: str
    csp: dict | None = None
    permissions: dict | None = None


@dataclass
class UiResourceData:
    """Fetched UI Resource data."""

    html: str
    csp: dict | None = None
    permissions: dict | None = None


# Module-level registry
_ui_tools: dict[str, UiToolMetadata] = {}  # tool_name -> metadata
_html_cache: dict[str, str] = {}  # resource_uri -> html


def get_ui_tool_metadata(tool_name: str) -> UiToolMetadata | None:
    """Get UI metadata for a tool, if it has a UI resource."""
    return _ui_tools.get(tool_name)


def has_ui_tools() -> bool:
    """Check if any UI-enabled MCP tools are registered."""
    return len(_ui_tools) > 0


def is_model_visible(tool_meta: dict) -> bool:
    """Check if a tool should be visible to the LLM model.

    Tools with visibility=["app"] are UI-only and should NOT be sent to the model.
    Default (no visibility declared): visible to both model and app.
    """
    ui = tool_meta.get("ui", {})
    visibility = ui.get("visibility")
    if visibility is None:
        return True
    return "model" in visibility


def register_ui_tool(server_name: str, tool_name: str, meta: dict) -> None:
    """Register a UI-enabled tool from its _meta field."""
    ui = meta.get("ui", {})
    resource_uri = ui.get("resourceUri")
    if not resource_uri:
        return

    _ui_tools[tool_name] = UiToolMetadata(
        server_name=server_name,
        tool_name=tool_name,
        resource_uri=resource_uri,
        csp=ui.get("csp"),
        permissions=ui.get("permissions"),
    )
    logger.info("MCP App UI tool registered: %s -> %s", tool_name, resource_uri)


async def discover_ui_tools(mcp_tools: list, server_configs: list) -> None:
    """Discover UI-enabled tools from activated MCP server connections.

    Inspects each MCP tool's metadata to find _meta.ui.resourceUri declarations.
    Called after activate_mcp() when MCP server connections are established.

    MAF's load_tools() converts MCP tools to FunctionTool instances but discards
    the _meta field, so the full definitions are listed again through the MCP
    connection adapter (app/mcp/connection.py, UDR-0221 D3).
    """
    _ui_tools.clear()
    _html_cache.clear()

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


async def fetch_ui_resource(tool: object, resource_uri: str) -> UiResourceData | None:
    """Fetch UI Resource HTML from an MCP server.

    Reads through the MCP connection adapter (UDR-0221 D3).
    Returns None if the resource cannot be fetched.
    """
    # Check cache first
    if resource_uri in _html_cache:
        meta = _ui_tools.get(resource_uri)
        return UiResourceData(
            html=_html_cache[resource_uri],
            csp=meta.csp if meta else None,
            permissions=meta.permissions if meta else None,
        )

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

        # Cache HTML
        _html_cache[resource_uri] = html

        # Extract CSP/permissions metadata from content-level _meta
        content_meta = connection.meta(content) or {}
        ui_meta = content_meta.get("ui", {})

        # Fall back to tool-level metadata for CSP/permissions
        tool_meta = None
        for tm in _ui_tools.values():
            if tm.resource_uri == resource_uri:
                tool_meta = tm
                break

        csp = (ui_meta.get("csp") if isinstance(ui_meta, dict) else None) or (tool_meta.csp if tool_meta else None)
        permissions = (ui_meta.get("permissions") if isinstance(ui_meta, dict) else None) or (
            tool_meta.permissions if tool_meta else None
        )

        return UiResourceData(html=html, csp=csp, permissions=permissions)

    except Exception:
        logger.warning("Failed to fetch UI resource: %s", resource_uri, exc_info=True)
        return None


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


def get_app_only_tool_names() -> set[str]:
    """Return names of app-only tools that should be hidden from the model."""
    result = set()
    for meta in _ui_tools.values():
        if meta.csp is not None:  # Has UI metadata
            # Check visibility - this needs the original _meta from the tool definition
            # For now, tools explicitly registered as app-only are filtered
            pass
    return result
