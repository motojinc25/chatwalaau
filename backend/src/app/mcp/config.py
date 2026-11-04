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

PRP-0242 (UDR-0223 D4 / D6) adds two things. ``${VAR}`` placeholders in ``env``,
``headers`` and ``url`` are resolved from the environment at load; an undefined
variable skips that one server. ``"disabled": true`` and ``"disabled_tools"`` carry
the operator's saved selection: a disabled server is parsed (so the inventory lists
it) but never connected.
"""

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

logger = logging.getLogger(__name__)

#: A ``${NAME}`` placeholder (UDR-0223 D4); the name follows the shell rule.
VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class UnresolvedVariableError(ValueError):
    """A ``${NAME}`` placeholder names a variable that is not set."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


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
    # The saved selection (PRP-0242, UDR-0223 D6).
    disabled: bool = False
    disabled_tools: list[str] = field(default_factory=list)


def resolve_vars(value: str) -> str:
    """Replace every ``${NAME}`` with ``os.environ[NAME]`` (UDR-0223 D4).

    Raises :class:`UnresolvedVariableError` for the first name that is unset or empty.
    """

    def _sub(match: re.Match[str]) -> str:
        resolved = os.environ.get(match.group(1))
        if not resolved:
            raise UnresolvedVariableError(match.group(1))
        return resolved

    return VAR_PATTERN.sub(_sub, value)


def referenced_vars(entry: dict) -> list[str]:
    """Every variable NAME an entry's ``env`` / ``headers`` / ``url`` references, in order."""
    names: list[str] = []
    values: list[Any] = [entry.get("url")]
    for key in ("env", "headers"):
        block = entry.get(key)
        if isinstance(block, dict):
            values.extend(block.values())
    for value in values:
        if isinstance(value, str):
            for name in VAR_PATTERN.findall(value):
                if name not in names:
                    names.append(name)
    return names


def _resolve_block(block: Any) -> dict[str, str]:
    if not isinstance(block, dict):
        return {}
    return {str(k): resolve_vars(v) if isinstance(v, str) else v for k, v in block.items()}


def _extract_selection(entry: dict, name: str) -> dict[str, Any]:
    """The saved ``disabled`` / ``disabled_tools`` keys, validated per field."""
    out: dict[str, Any] = {}
    disabled = entry.get("disabled", False)
    if isinstance(disabled, bool):
        out["disabled"] = disabled
    else:
        logger.warning("MCP server '%s': field 'disabled' must be bool, got %r; ignored", name, disabled)
    tools = entry.get("disabled_tools", [])
    if isinstance(tools, list) and all(isinstance(t, str) for t in tools):
        out["disabled_tools"] = list(tools)
    else:
        logger.warning("MCP server '%s': field 'disabled_tools' must be a list of names; ignored", name)
    return out


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
    if not isinstance(config, dict):
        logger.warning("MCP config root is not an object in %s, skipping", config_path)
        return []
    return parse_mcp_document(config, source=str(config_path))


def parse_mcp_document(config: dict, *, resolve: bool = True, source: str = "MCP config") -> list[MCPServerConfig]:
    """Parse an already-loaded config document (PRP-0242: shared by the loader and the writer).

    With ``resolve=True`` (the loader) ``${VAR}`` placeholders are replaced from the
    environment and a server naming an unset variable is skipped with a WARNING. With
    ``resolve=False`` (the writer's validation and the inventory) values are kept as
    written, so a variable that is not set yet never blocks a save.
    """
    servers_dict = config.get("mcpServers", {})
    if not isinstance(servers_dict, dict):
        logger.warning("mcpServers key is not a dict in %s, skipping", source)
        return []

    servers: list[MCPServerConfig] = []
    for name, entry in servers_dict.items():
        if not isinstance(entry, dict):
            logger.warning("MCP server '%s': entry is not a dict, skipping", name)
            continue

        optional = {**_extract_optional(entry, name), **_extract_selection(entry, name)}
        resolve_block = _resolve_block if resolve else (lambda b: dict(b) if isinstance(b, dict) else {})

        try:
            if "command" in entry:
                servers.append(
                    MCPServerConfig(
                        name=name,
                        transport="stdio",
                        command=entry["command"],
                        args=entry.get("args", []),
                        env=resolve_block(entry.get("env", {})),
                        **optional,
                    )
                )
            elif "url" in entry:
                url = entry["url"]
                servers.append(
                    MCPServerConfig(
                        name=name,
                        transport="http",
                        url=resolve_vars(url) if resolve and isinstance(url, str) else url,
                        headers=resolve_block(entry.get("headers", {})),
                        **optional,
                    )
                )
            else:
                logger.warning("MCP server '%s': no 'command' or 'url' field, skipping", name)
        except UnresolvedVariableError as exc:
            logger.warning(
                "MCP server '%s' references undefined environment variable ${%s}; skipping it",
                name,
                exc.name,
            )

    return servers
