"""MCP tool provider factory (CTR-0060, PRP-0031, PRP-0046).

Creates MAF MCPStdioTool / MCPStreamableHTTPTool instances from parsed
MCP server configurations. Tool instances are returned unstarted; the
lifecycle module (CTR-0061) handles startup via AsyncExitStack.

PRP-0046 forwards optional MAF constructor arguments
(``load_tools``, ``load_prompts``, ``request_timeout``) from the
config entry so operators can disable servers' unsupported capabilities
(e.g. ``prompts/list`` on a tools-only server) declaratively instead
of relying on a runtime patcher.
"""

import logging
from typing import Any

from agent_framework import MCPStdioTool, MCPStreamableHTTPTool

from app.mcp.config import MCPServerConfig

logger = logging.getLogger(__name__)


def _forwarded_kwargs(server: MCPServerConfig) -> dict[str, Any]:
    """Build MAF kwargs from the PRP-0046 optional fields.

    Mostly a passthrough that skips ``None``, with one exception:
    ``load_prompts`` is defaulted to ``False`` when unset. The real-world
    MCP server ecosystem (filesystem, git, github, batch, etc.) is
    dominated by tools-only servers that do not implement ``prompts/list``,
    so forwarding MAF's default ``True`` produces a "Method not found"
    stack trace on first connect for the common case. Operators who
    know their server supports prompts can enable them with an explicit
    ``"load_prompts": true`` in ``mcp_servers.json``.
    """
    out: dict[str, Any] = {}
    if server.load_tools is not None:
        out["load_tools"] = server.load_tools
    # Safer default than MAF's True: most community servers are
    # tools-only and returning "Method not found" aborts connection.
    out["load_prompts"] = server.load_prompts if server.load_prompts is not None else False
    if server.request_timeout is not None:
        out["request_timeout"] = server.request_timeout
    return out


def create_mcp_tool(server: MCPServerConfig) -> MCPStdioTool | MCPStreamableHTTPTool:
    """Create a single MAF MCP tool instance from config.

    Args:
        server: Parsed MCP server configuration.

    Returns:
        MCPStdioTool or MCPStreamableHTTPTool instance (not yet started).
    """
    forwarded = _forwarded_kwargs(server)

    if server.transport == "stdio":
        tool = MCPStdioTool(
            name=server.name,
            command=server.command,
            args=server.args,
            env=server.env or None,
            **forwarded,
        )
        logger.debug(
            "MCPStdioTool created: %s (command=%s, options=%s)",
            server.name,
            server.command,
            forwarded or "{}",
        )
        return tool

    # http transport
    tool = MCPStreamableHTTPTool(
        name=server.name,
        url=server.url,
        headers=server.headers or None,
        **forwarded,
    )
    logger.debug(
        "MCPStreamableHTTPTool created: %s (url=%s, options=%s)",
        server.name,
        server.url,
        forwarded or "{}",
    )
    return tool
