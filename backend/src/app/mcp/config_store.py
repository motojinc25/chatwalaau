"""MCP configuration writer (CTR-0278 / CTR-0059, PRP-0242, UDR-0223).

The screen writes the same ``mcp_servers.jsonc`` the operator edits by hand (D1);
there is no second store. This module owns the file side of that:

- where a write goes (the configured ``MCP_CONFIG_FILE``; the bundled
  ``*.default.jsonc`` is read as a seed and never modified);
- the ``revision`` (a hash of the bytes the API last read), so a write never
  silently overwrites a hand edit (D7);
- masking: a literal ``env`` / header value leaves the server only as
  ``{"literal": true}`` (D5);
- the screen-entry rules: secrets are ``${VAR}`` references only (D4);
- the atomic write, keeping the previous file as ``<name>.bak`` (D7).

Validation reuses the loader's parser (``parse_mcp_document``) so the screen can
never write a file the loader would read differently. The write does not reconnect
anything; the router runs the existing reload afterwards (UDR-0068).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any

from app.core.config import settings
from app.mcp.config import _strip_jsonc_comments, parse_mcp_document, referenced_vars

logger = logging.getLogger(__name__)

#: What a literal env / header value looks like on the wire (UDR-0223 D5). Sent back
#: unchanged on a replace, it keeps the stored value.
LITERAL_MARKER: dict[str, bool] = {"literal": True}

#: Fields the screen edits. Any other key in a stored entry survives a replace (D7).
EDITABLE_KEYS = ("command", "args", "env", "url", "headers", "load_tools", "load_prompts", "request_timeout")
#: The saved selection, written only by PUT /api/mcp/tools (D6).
SELECTION_KEYS = ("disabled", "disabled_tools")

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
#: An env value saved from the screen is exactly one reference.
ENV_REF = re.compile(rf"^\$\{{{_NAME}\}}$")
#: A header value saved from the screen is a reference, optionally after an auth scheme.
HEADER_REF = re.compile(rf"^(?:[A-Za-z][A-Za-z0-9._~+-]* )?\$\{{{_NAME}\}}$")
SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
ENV_KEY = re.compile(rf"^{_NAME}$")


class McpConfigError(Exception):
    """A refused configuration change; ``status`` is the HTTP status the API answers."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class ConfigDocument:
    """The config as the API sees it: where it was read from and where a write goes."""

    source: str  # "configured" | "default" | "none" | "disabled"
    read_path: Path | None
    target_path: Path | None
    revision: str
    data: dict[str, Any] = field(default_factory=lambda: {"mcpServers": {}})
    error: str | None = None

    @property
    def servers(self) -> dict[str, Any]:
        servers = self.data.setdefault("mcpServers", {})
        return servers if isinstance(servers, dict) else {}


def _revision(raw: bytes | None) -> str:
    return hashlib.sha256(raw).hexdigest()[:16] if raw is not None else ""


def target_path() -> Path | None:
    """Where a write goes: the configured file, or None when MCP is off (``MCP_CONFIG_FILE=""``)."""
    raw = (settings.mcp_config_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path.cwd() / path


def read_document() -> ConfigDocument:
    """Read the config the runtime uses (same two-tier resolution as the loader)."""
    target = target_path()
    if target is None:
        return ConfigDocument(source="disabled", read_path=None, target_path=None, revision="")
    default = target.with_name(f"{target.stem}.default{target.suffix}")
    if target.is_file():
        source, read_path = "configured", target
    elif default.is_file():
        source, read_path = "default", default
    else:
        return ConfigDocument(source="none", read_path=None, target_path=target, revision="")
    try:
        raw = read_path.read_bytes()
    except OSError as exc:
        return ConfigDocument(source, read_path, target, "", error=f"cannot read the MCP config: {exc}")
    doc = ConfigDocument(source=source, read_path=read_path, target_path=target, revision=_revision(raw))
    try:
        data = json.loads(_strip_jsonc_comments(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        doc.error = "the MCP config is not valid JSON/JSONC"
        return doc
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        doc.error = "the MCP config has no 'mcpServers' object"
        return doc
    data.setdefault("mcpServers", {})
    doc.data = data
    return doc


def transport_of(entry: Any) -> str:
    if isinstance(entry, dict):
        if "command" in entry:
            return "stdio"
        if "url" in entry:
            return "http"
    return ""


def _mask_block(block: Any, pattern: re.Pattern[str]) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {}
    return {k: v if isinstance(v, str) and pattern.match(v) else dict(LITERAL_MARKER) for k, v in block.items()}


def mask_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to return: literal env / header values become the marker (D5)."""
    out = dict(entry)
    if "env" in out:
        out["env"] = _mask_block(out["env"], ENV_REF)
    if "headers" in out:
        out["headers"] = _mask_block(out["headers"], HEADER_REF)
    return out


def env_status(doc: ConfigDocument) -> dict[str, bool]:
    """Whether each referenced variable NAME is set (never its value)."""
    names: list[str] = []
    for entry in doc.servers.values():
        if isinstance(entry, dict):
            names.extend(n for n in referenced_vars(entry) if n not in names)
    return {name: bool((os.environ.get(name) or "").strip()) for name in names}


def _clean_block(
    block: Any, *, kind: str, key_pattern: re.Pattern[str], value_pattern: re.Pattern[str], stored: Any
) -> dict[str, str]:
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise McpConfigError(422, "invalid_entry", f"'{kind}' must be an object")
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, str] = {}
    for key, value in block.items():
        if not isinstance(key, str) or not key_pattern.match(key):
            raise McpConfigError(422, "invalid_entry", f"'{kind}' has an invalid name: {key!r}")
        if value == LITERAL_MARKER:
            if key not in stored:
                raise McpConfigError(422, "invalid_entry", f"{kind} '{key}' has no stored value to keep")
            out[key] = stored[key]
        elif isinstance(value, str) and value_pattern.match(value):
            out[key] = value
        else:
            example = "${NAME}" if kind == "env" else "${NAME} or Bearer ${NAME}"
            raise McpConfigError(
                422,
                "secret_not_referenced",
                f"{kind} '{key}' must reference an environment variable ({example}); "
                "values are never saved from the screen",
            )
    return out


def clean_screen_entry(entry: Any, stored: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate an entry sent by the screen and return the fields to store (D4 / D5).

    ``stored`` is the entry being replaced: its literal values are kept where the
    screen sent the marker back, and its other keys are merged in by :func:`merge_entry`.
    """
    if not isinstance(entry, dict):
        raise McpConfigError(422, "invalid_entry", "the server entry must be an object")
    stored = stored or {}
    has_command, has_url = bool(entry.get("command")), bool(entry.get("url"))
    if has_command == has_url:
        raise McpConfigError(422, "invalid_entry", "give either a command (stdio) or a url (HTTP)")
    out: dict[str, Any] = {}
    if has_command:
        command, args = entry["command"], entry.get("args") or []
        if not isinstance(command, str) or not command.strip():
            raise McpConfigError(422, "invalid_entry", "'command' must be a non-empty string")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise McpConfigError(422, "invalid_entry", "'args' must be a list of strings")
        out["command"] = command.strip()
        if args:
            out["args"] = args
        env = _clean_block(
            entry.get("env"), kind="env", key_pattern=ENV_KEY, value_pattern=ENV_REF, stored=stored.get("env")
        )
        if env:
            out["env"] = env
    else:
        url = entry["url"]
        if not isinstance(url, str) or not (url.startswith(("http://", "https://", "${"))):
            raise McpConfigError(422, "invalid_entry", "'url' must start with http:// or https://")
        out["url"] = url.strip()
        headers = _clean_block(
            entry.get("headers"),
            kind="header",
            key_pattern=HEADER_NAME,
            value_pattern=HEADER_REF,
            stored=stored.get("headers"),
        )
        if headers:
            out["headers"] = headers
    for key in ("load_tools", "load_prompts"):
        if entry.get(key) is not None:
            if not isinstance(entry[key], bool):
                raise McpConfigError(422, "invalid_entry", f"'{key}' must be true or false")
            out[key] = entry[key]
    timeout = entry.get("request_timeout")
    if timeout is not None:
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise McpConfigError(422, "invalid_entry", "'request_timeout' must be a positive number of seconds")
        out["request_timeout"] = timeout
    return out


def merge_entry(cleaned: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
    """The entry to store: the screen's fields, plus the stored keys the screen does not edit (D7).

    Unknown keys (another tool's settings) and the saved selection (D6) survive a replace.
    """
    out = dict(cleaned)
    for key, value in (stored or {}).items():
        if key not in EDITABLE_KEYS and key not in out:
            out[key] = value
    return out


def check_name(name: Any) -> str:
    if not isinstance(name, str) or not SERVER_NAME.match(name):
        raise McpConfigError(
            422,
            "invalid_name",
            "a server name is 1-64 letters, digits, '-', '_' or '.', starting with a letter or digit",
        )
    return name


def check_writable(doc: ConfigDocument, expected_revision: str | None) -> None:
    """Refuse a write that could lose data (D7)."""
    if doc.source == "disabled" or doc.target_path is None:
        raise McpConfigError(409, "mcp_disabled", "MCP is turned off (MCP_CONFIG_FILE is empty); nothing can be saved")
    if doc.error:
        raise McpConfigError(409, "config_invalid", f"{doc.error}; fix the file by hand first")
    if expected_revision is not None and expected_revision != doc.revision:
        raise McpConfigError(
            409, "stale_revision", "the MCP config changed since it was opened; reopen it and try again"
        )


def serialize(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_document(doc: ConfigDocument, data: dict[str, Any]) -> Path:
    """Validate with the loader's parser and write atomically, keeping ``<name>.bak`` (D1 / D7).

    The caller has already run :func:`check_writable` against the same ``doc``.
    """
    assert doc.target_path is not None
    parse_mcp_document(data, resolve=False, source="the new MCP config")
    target = doc.target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(serialize(data), encoding="utf-8", newline="\n")
    if target.is_file():
        shutil.copy2(target, target.with_name(f"{target.name}.bak"))
    tmp.replace(target)
    logger.info("MCP config written: %s (%d server(s))", target, len(data.get("mcpServers", {})))
    return target
