"""Merged slash command inventory (CTR-0126).

Combines the static built-ins (CTR-0125) with the DYNAMIC commands derived from
Prompt Templates (CTR-0047 ``slash_command``, else a name slug) and Agent Skills
(CTR-0043 inventory, optional ``command`` metadata, else the skill name). Each
entry is tagged with its ``source`` so the client dispatcher can route it
(UDR-0066 D1/D4). Token collisions across sources are surfaced; the first source
wins (built-in > prompt > skill, in scan order).
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any

from app.commands.registry import TOKEN_RE, load_builtin_commands
from app.core.config import settings

logger = logging.getLogger(__name__)


def slugify_token(name: str) -> str:
    """Derive a valid command token from a free-text name; "" if none possible.

    Lowercases, replaces runs of non-token characters with ``-``, trims leading/
    trailing separators, and drops a leading non-letter so the result matches
    TOKEN_RE (UDR-0066 D2). Returns "" when nothing valid remains.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    slug = re.sub(r"^[^A-Za-z]+", "", slug)
    return slug if TOKEN_RE.match(slug) else ""


def _prompt_commands() -> list[dict[str, Any]]:
    """Derive commands from Prompt Templates (CTR-0047)."""
    from app.prompt_templates.storage import TemplateStorage

    out: list[dict[str, Any]] = []
    try:
        storage = TemplateStorage(Path(settings.templates_dir))
        templates = storage.list_all()
    except OSError:
        return out
    for tpl in templates:
        explicit = str(tpl.get("slash_command", "") or "").lstrip("/").strip()
        token = explicit if TOKEN_RE.match(explicit) else slugify_token(str(tpl.get("name", "")))
        if not token:
            continue
        out.append(
            {
                "token": token,
                "source": "prompt",
                "description": str(tpl.get("description", "") or tpl.get("name", "")),
                "category": "Prompt",
                "aliases": [],
                "args_hint": "[args...]",
                "ref": tpl.get("id", ""),
            }
        )
    return out


async def _skill_commands() -> list[dict[str, Any]]:
    """Derive commands from Agent Skills (CTR-0043 inventory)."""
    from app.skills.inventory import get_skills_inventory

    out: list[dict[str, Any]] = []
    try:
        inventory = await get_skills_inventory()
    except Exception:
        logger.exception("Skills inventory failed while building command list")
        return out
    for group in inventory.get("groups", []):
        for skill in group.get("skills", []):
            name = str(skill.get("name", ""))
            explicit = str(skill.get("command", "") or "").lstrip("/").strip()
            token = explicit if TOKEN_RE.match(explicit) else slugify_token(name)
            if not token:
                continue
            out.append(
                {
                    "token": token,
                    "source": "skill",
                    "description": str(skill.get("description", "") or name),
                    "category": "Skill",
                    "aliases": [],
                    "args_hint": str(skill.get("args_hint", "") or "[args...]"),
                    "ref": name,
                }
            )
    return out


async def get_commands_inventory() -> dict[str, Any]:
    """Return the merged, deduplicated command inventory (CTR-0126).

    Shape: ``{"commands": [{token, source, description, category, aliases,
    args_hint, ref}], "collisions": [{token, sources: [...]}]}``. Built-ins always
    appear, so the SPA always has a baseline even when templates/skills are empty.
    """
    commands: list[dict[str, Any]] = []
    claimed: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}

    def _add(entry: dict[str, Any]) -> None:
        token = entry["token"]
        if token in claimed:
            collisions.setdefault(token, [claimed[token]])
            if entry["source"] not in collisions[token]:
                collisions[token].append(entry["source"])
            return  # first source wins (UDR-0066 D4)
        claimed[token] = entry["source"]
        commands.append(entry)

    # Built-ins first (highest precedence).
    for c in load_builtin_commands():
        _add(
            {
                "token": c["name"],
                "source": "builtin",
                "description": c["description"],
                "category": c.get("category", ""),
                "aliases": c.get("aliases", []),
                "args_hint": c.get("args_hint", ""),
                "ref": "",
            }
        )

    for entry in _prompt_commands():
        _add(entry)
    for entry in await _skill_commands():
        _add(entry)

    return {
        "commands": commands,
        "collisions": [{"token": t, "sources": s} for t, s in sorted(collisions.items())],
    }


__all__ = ["get_commands_inventory", "slugify_token"]
