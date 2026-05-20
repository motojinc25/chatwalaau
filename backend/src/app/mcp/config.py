"""MCP configuration parser (CTR-0059, PRP-0031, PRP-0046, PRP-0060).

Parses a Claude Desktop-compatible mcp_servers.jsonc configuration
file and returns structured server definitions for tool creation.

PRP-0046 extends the Claude Desktop format with optional fields that
map directly to MAF ``MCPStdioTool`` / ``MCPStreamableHTTPTool``
constructor arguments. Unknown keys in an entry are ignored so that
Claude Desktop configs remain valid.

PRP-0060 widens the accepted format from strict JSON to JSONC by
stripping ``//`` line comments and ``/* ... */`` block comments
before parsing. Strict-JSON contents continue to parse byte-for-byte
because JSONC is a strict-JSON superset. No new dependency.
"""

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Recognized optional fields (PRP-0046). Claude Desktop ignores unknown
# keys; we mirror that behavior so a shared config file works in both
# places.
_OPTIONAL_BOOL_FIELDS = ("load_tools", "load_prompts")
_OPTIONAL_INT_FIELDS = ("request_timeout",)


@dataclass
class MCPServerConfig:
    """Parsed MCP server configuration entry."""

    name: str
    transport: str  # "stdio" or "http"
    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http fields
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # Optional MAF passthrough (PRP-0046). ``None`` means "use MAF default".
    load_tools: bool | None = None
    load_prompts: bool | None = None
    request_timeout: int | None = None


def _extract_optional(entry: dict, name: str) -> dict[str, Any]:
    """Pull optional PRP-0046 fields out of an entry with per-field validation."""
    out: dict[str, Any] = {}
    for key in _OPTIONAL_BOOL_FIELDS:
        if key in entry:
            value = entry[key]
            if isinstance(value, bool):
                out[key] = value
            else:
                logger.warning(
                    "MCP server '%s': field '%s' must be bool, got %r; ignored",
                    name,
                    key,
                    value,
                )
    for key in _OPTIONAL_INT_FIELDS:
        if key in entry:
            value = entry[key]
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                out[key] = value
            else:
                logger.warning(
                    "MCP server '%s': field '%s' must be positive int, got %r; ignored",
                    name,
                    key,
                    value,
                )
    return out


def _strip_jsonc_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments from JSONC source (PRP-0060).

    Single-pass state machine that preserves double-quoted JSON string
    contents (including escape sequences). Stripping happens entirely
    outside strings, so strict-JSON input is returned byte-for-byte
    identical.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i + 2)
            i = n if j == -1 else j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_mcp_config(config_path: Path) -> list[MCPServerConfig]:
    """Parse Claude Desktop-compatible MCP configuration file.

    Args:
        config_path: Path to mcp_servers.jsonc (or any JSONC superset
            of strict JSON; legacy mcp_servers.json contents still
            parse unchanged via the comment-strip pass).

    Returns:
        List of MCPServerConfig entries. Invalid entries are skipped with warnings.
    """
    try:
        raw = config_path.read_text(encoding="utf-8")
        config = json.loads(_strip_jsonc_comments(raw))
    except json.JSONDecodeError:
        logger.error("MCP config file is not valid JSON/JSONC: %s", config_path)
        return []
    except OSError:
        logger.error("Failed to read MCP config file: %s", config_path)
        return []

    servers_dict = config.get("mcpServers", {})
    if not isinstance(servers_dict, dict):
        logger.warning("mcpServers key is not a dict in %s, skipping", config_path)
        return []

    servers: list[MCPServerConfig] = []
    for name, entry in servers_dict.items():
        if not isinstance(entry, dict):
            logger.warning("MCP server '%s': entry is not a dict, skipping", name)
            continue

        optional = _extract_optional(entry, name)

        if "command" in entry:
            servers.append(
                MCPServerConfig(
                    name=name,
                    transport="stdio",
                    command=entry["command"],
                    args=entry.get("args", []),
                    env=entry.get("env", {}),
                    **optional,
                )
            )
        elif "url" in entry:
            servers.append(
                MCPServerConfig(
                    name=name,
                    transport="http",
                    url=entry["url"],
                    headers=entry.get("headers", {}),
                    **optional,
                )
            )
        else:
            logger.warning("MCP server '%s': no 'command' or 'url' field, skipping", name)

    return servers
