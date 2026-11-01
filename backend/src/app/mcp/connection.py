"""MCP protocol access beneath a MAF MCP tool (CTR-0060, PRP-0238, UDR-0221 D3).

The ONLY module in ``app/`` that reaches the MCP protocol under a MAF ``MCPTool``
(MCP Apps discovery and UI resource reads, the CTR-0067 host bridge), and the one place
that knows the MCP SDK 2 field names (the Computer Use adapter, CTR-0237, reads result
fields through it too).

Why it exists: MAF 1.23 rebuilt ``MCPTool`` on a normalized connection. A tool that
opens its own stdio / streamable-HTTP transport -- every tool ``app.mcp.provider``
builds -- holds a high-level ``mcp.Client``, and the deprecated ``MCPTool.session``
returns ``None`` (#9237, #9452). MCP Apps went dark without an error (PRP-0238 F2).
The connection is reached through ``tool._require_connection()``, the seam upstream's
own declarative package uses; a tool without it falls back to ``tool.session``; a tool
with neither raises :class:`McpConnectionUnavailable`. The private seam is pinned by
name and signature in ``tests/invariants/test_prp0238_maf_123_upgrade.py``.

Connections stay framework-owned (CTR-0061 unchanged): nothing here opens, closes or
re-configures a transport.

MCP 2 types are snake_case attributes with camelCase aliases on the wire only:
``getattr(result, "isError")`` is never set. Results handed to a View or a client go
through :func:`to_wire` (the protocol's keys, ``_meta`` included); Python reads use
:func:`is_error`, :func:`structured_content` and :func:`meta`.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Upper bound on ``tools/list`` pages followed for one server (a server that never ends
#: its cursor chain must not hang discovery).
MAX_TOOL_PAGES = 100


class McpConnectionUnavailable(RuntimeError):
    """The MCP tool has no live connection (not entered, closed, or an unknown MAF shape)."""


def _connection(tool: object) -> Any:
    """The live MCP connection beneath ``tool``: MAF's normalized one, else a session."""
    require = getattr(tool, "_require_connection", None)
    if callable(require):
        try:
            return require()
        except RuntimeError as exc:
            raise McpConnectionUnavailable(str(exc) or "MCP tool is not connected") from exc
    session = getattr(tool, "session", None)
    if session is None:
        raise McpConnectionUnavailable("MCP tool has no connection")
    return session


async def list_tools(tool: object) -> list[Any]:
    """Every tool definition the server lists, with ``meta`` (``_meta`` on the wire)."""
    conn = _connection(tool)
    list_page = getattr(conn, "list_tools_page", None)
    if not callable(list_page):  # a bare ClientSession (the fallback)
        return list(getattr(await conn.list_tools(), "tools", None) or [])

    from mcp import types

    tools: list[Any] = []
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        page = await list_page(types.PaginatedRequestParams(cursor=cursor) if cursor else None)
        tools.extend(getattr(page, "tools", None) or [])
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            return tools
    logger.warning("MCP tools/list stopped after %d pages", MAX_TOOL_PAGES)
    return tools


async def read_resource(tool: object, uri: str) -> Any:
    """``resources/read`` on the server beneath ``tool``."""
    conn = _connection(tool)
    if hasattr(conn, "list_tools_page"):
        return await conn.read_resource(uri)
    from pydantic import AnyUrl  # MCP 1.x ClientSession takes a URL object

    return await conn.read_resource(AnyUrl(uri))


async def call_tool(tool: object, name: str, arguments: dict[str, Any] | None) -> Any:
    """``tools/call`` on the server beneath ``tool``, bypassing MAF's function layer."""
    conn = _connection(tool)
    if hasattr(conn, "list_tools_page"):
        return await conn.call_tool(name, arguments, meta=None)
    return await conn.call_tool(name, arguments=arguments)


# ---- MCP 2 result shapes (one place) ------------------------------------------------------------


def to_wire(result: Any) -> Any:
    """A result as the protocol spells it: camelCase keys, ``_meta``, no nulls.

    ``exclude_none`` drops null fields (e.g. ``annotations: None``) that the MCP Apps View
    SDK's strict validation rejects.
    """
    dump = getattr(result, "model_dump", None)
    if not callable(dump):
        return result
    return dump(by_alias=True, exclude_none=True, mode="json")


def is_error(result: Any) -> bool:
    """``isError`` of a ``CallToolResult``."""
    return bool(getattr(result, "is_error", False))


def structured_content(result: Any) -> Any:
    """``structuredContent`` of a ``CallToolResult`` (``None`` when absent)."""
    return getattr(result, "structured_content", None)


def meta(obj: Any) -> dict[str, Any] | None:
    """``_meta`` of a protocol object (a tool definition, a resource content) as a dict."""
    raw = getattr(obj, "meta", None)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    dump = getattr(raw, "model_dump", None)
    return dump(by_alias=True, exclude_none=True) if callable(dump) else None


__all__ = [
    "MAX_TOOL_PAGES",
    "McpConnectionUnavailable",
    "call_tool",
    "is_error",
    "list_tools",
    "meta",
    "read_resource",
    "structured_content",
    "to_wire",
]
