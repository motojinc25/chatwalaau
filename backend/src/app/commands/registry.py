"""Slash Command registry: built-in commands + optional operator override (CTR-0125).

The four built-in commands (help / prompt / skill / model) are a CODE constant so
the feature works with zero operator configuration (a pip install with no file in
cwd still gets the built-ins). An optional JSONC file named by COMMANDS_CONFIG_FILE
(CTR-0006) may ADD operator-authored data commands or OVERRIDE a built-in's
metadata; it is parsed by the same comment-stripping state machine used for MCP
(CTR-0059 / PRP-0060). Two-tier resolution mirrors MCP: the operator override wins,
else a bundled ``commands.default.jsonc`` sibling. METADATA only -- behavior is code
(UDR-0066 D2/D4).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

from app.core.config import settings
from app.mcp.config import _strip_jsonc_comments

logger = logging.getLogger(__name__)

# A command token (and each alias) without the leading ``/``.
TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# The static, code-backed built-in commands (UDR-0066 D2). ``args_hint`` is a
# human-readable argument hint shown in /help and ghost completion.
DEFAULT_COMMANDS: list[dict[str, Any]] = [
    {
        "name": "help",
        "category": "Core",
        "description": "Show all available slash commands",
        "aliases": [],
        "args_hint": "",
    },
    {
        "name": "prompt",
        "category": "Prompt",
        "description": "Run a prompt template",
        "aliases": ["p"],
        "args_hint": "<template> [args...]",
    },
    {
        "name": "skill",
        "category": "Skill",
        "description": "Invoke an Agent Skill",
        "aliases": [],
        "args_hint": "<skill> [args...]",
    },
    {
        "name": "model",
        "category": "Model",
        "description": "Switch the active model",
        "aliases": ["m"],
        "args_hint": "<model>",
    },
    {
        "name": "cron",
        "category": "Core",
        "description": "Open the Cron scheduler portal",
        "aliases": [],
        "args_hint": "",
    },
    {
        "name": "files",
        "category": "Core",
        "description": "Open the File Explorer",
        "aliases": [],
        "args_hint": "",
    },
]


def _resolve_commands_config_path() -> Path | None:
    """Resolve the optional operator command file with two-tier fallback (CTR-0125).

    Resolution order (mirrors the MCP resolver):
      1. ``COMMANDS_CONFIG_FILE`` empty -> no operator file (built-ins only).
      2. Configured path if it exists -> operator override.
      3. Derived ``<stem>.default<suffix>`` sibling if it exists -> bundled default.
      4. Otherwise None (built-ins only).
    """
    raw = (settings.commands_config_file or "").strip()
    if not raw:
        return None
    override = Path(raw)
    if not override.is_absolute():
        override = Path.cwd() / override
    if override.is_file():
        return override
    default = override.with_name(f"{override.stem}.default{override.suffix}")
    if default.is_file():
        return default
    return None


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and normalize one command metadata entry; None if invalid."""
    name = str(entry.get("name", "")).strip()
    if not TOKEN_RE.match(name):
        logger.warning("Skipping command with invalid name: %r", entry.get("name"))
        return None
    aliases = [a for a in (entry.get("aliases") or []) if isinstance(a, str) and TOKEN_RE.match(a)]
    return {
        "name": name,
        "category": str(entry.get("category", "") or ""),
        "description": str(entry.get("description", "") or ""),
        "aliases": aliases,
        "args_hint": str(entry.get("args_hint", "") or ""),
    }


def _load_operator_commands() -> list[dict[str, Any]]:
    """Parse the optional operator command file; [] on any problem (never raises)."""
    path = _resolve_commands_config_path()
    if path is None:
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(_strip_jsonc_comments(raw))
    except json.JSONDecodeError:
        logger.error("Commands config file is not valid JSON/JSONC: %s", path)
        return []
    except OSError:
        logger.error("Failed to read commands config file: %s", path)
        return []
    entries = parsed.get("commands") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        logger.error("Commands config file has no 'commands' array: %s", path)
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            norm = _normalize_entry(entry)
            if norm is not None:
                out.append(norm)
    return out


def load_builtin_commands() -> list[dict[str, Any]]:
    """Return the effective built-in command metadata (code defaults + operator file).

    Operator entries with a name matching a built-in OVERRIDE that built-in's
    metadata; new names are appended. First declaration of a token wins for
    collision purposes downstream (CTR-0126).
    """

    # The /cron and /files built-ins are only meaningful when their feature is
    # enabled (UDR-0067 D10 / UDR-0069 D3); drop each from the advertised set otherwise.
    def _advertise(name: str) -> bool:
        if name == "cron":
            return settings.cron_enabled
        if name == "files":
            return settings.file_explorer_enabled
        return True

    defaults = [c for c in DEFAULT_COMMANDS if _advertise(c["name"])]
    by_name: dict[str, dict[str, Any]] = {c["name"]: dict(c) for c in defaults}
    for entry in _load_operator_commands():
        by_name[entry["name"]] = entry
    # Preserve built-in order first, then any appended operator commands.
    ordered = [by_name[c["name"]] for c in defaults]
    extras = [v for k, v in by_name.items() if k not in {c["name"] for c in defaults}]
    return ordered + extras


__all__ = ["DEFAULT_COMMANDS", "TOKEN_RE", "load_builtin_commands"]
