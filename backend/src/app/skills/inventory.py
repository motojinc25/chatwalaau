"""Agent Skills inventory, grouped by folder (CTR-0043 / CTR-0123, PRP-0087).

Builds the operator-facing Skills inventory for the management API (CTR-0123).
The canonical loadable set (name + description + enabled state) comes from the MAF
file source's ``get_skills()`` (post-validation, post-dedup), and the GROUP is
derived from each skill's filesystem path relative to SKILLS_DIR (UDR-0065 D2):

    A/B/SKILL.md  -> group "A", skill "B"
    B/SKILL.md    -> ungrouped (group ""), skill "B"

MAF does NOT capture the parent "group" folder as metadata, so the group is an
inventory/UI organization concept only; the persisted identity is the skill name.

Name-collision detection (UDR-0065 D2): MAF requires the skill directory basename
to equal the frontmatter ``name`` and keeps only the FIRST of any duplicate name
(``FileSkillsSource.get_skills`` is name-keyed), so a second ``*/B/SKILL.md`` is
silently dropped from the loadable set. We surface such collisions by counting
duplicate basenames among ALL discovered SKILL.md directories (a duplicate basename
== a duplicate skill name, by MAF's basename==name rule), so the UI can warn the
operator. The disabled name still gates EVERY same-named skill, so none is left
silently un-gateable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.skills.loaded import get_loaded_skills
from app.skills.overrides import get_skills_override_store

logger = logging.getLogger(__name__)

_SKILL_FILE_NAME = "SKILL.md"
# Mirror MAF's FileSkillsSource discovery depth (agent_framework MAX_SEARCH_DEPTH).
_MAX_SEARCH_DEPTH = 2


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Return directories containing a SKILL.md, searched up to 2 levels deep.

    Mirrors MAF ``FileSkillsSource._discover_skill_directories`` so the collision
    scan sees exactly the same candidate set MAF discovers.
    """
    out: list[Path] = []

    def _search(directory: Path, depth: int) -> None:
        if (directory / _SKILL_FILE_NAME).is_file():
            out.append(directory)
        if depth >= _MAX_SEARCH_DEPTH:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                _search(entry, depth + 1)

    if root.is_dir():
        _search(root, 0)
    return out


def _read_command_meta(skill_dir: Path) -> dict[str, str]:
    """Best-effort read of optional slash-command metadata from SKILL.md frontmatter.

    Scans the leading ``---`` ... ``---`` YAML frontmatter for optional ``command``
    and ``args_hint`` keys (CTR-0043 v-note, PRP-0088). Additive and tolerant: any
    read/parse problem yields an empty dict, so a skill with no such keys (the common
    case) contributes nothing. No yaml dependency -- a minimal ``key: value`` scan.
    """
    out: dict[str, str] = {}
    path = skill_dir / _SKILL_FILE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    if not text.lstrip().startswith("---"):
        return out
    lines = text.splitlines()
    started = False
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            if started:
                break
            started = True
            continue
        if not started:
            continue
        for key in ("command", "args_hint"):
            prefix = f"{key}:"
            if stripped.startswith(prefix):
                value = stripped[len(prefix) :].strip().strip("'\"")
                if value:
                    out[key] = value
    return out


def _group_for(skill_dir: Path, root: Path) -> str:
    """Derive the group name from the skill directory's path relative to SKILLS_DIR.

    Returns the first path segment for a nested skill (``A/B`` -> ``A``) or the
    empty string for an ungrouped (depth-1) or root-level skill (UDR-0065 D2).
    """
    try:
        rel = skill_dir.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return ""
    parts = rel.parts
    if len(parts) >= 2:
        return parts[0]
    return ""


async def get_skills_inventory() -> dict[str, Any]:
    """Return the grouped Skills inventory with current enabled/disabled state.

    Shape: ``{"groups": [{"name", "skills": [{"name", "description", "enabled",
    "loaded"}]}], "collisions": [name, ...], "skills_dir": str}``. The ungrouped
    bucket carries group name ``""``. ``loaded`` (UDR-0068 D4) is true iff the skill
    is in the current live agent build; a skill discovered on disk but not yet loaded
    (added since the last build/Reload) is ``loaded=false`` so the UI disables its
    toggle. ``skills_dir`` is always returned so the UI can render an empty state and
    a Reload action even when SKILLS_DIR is absent (UDR-0068 D5).
    """
    skills_path = Path(settings.skills_dir)
    if not skills_path.is_dir():
        return {"groups": [], "collisions": [], "skills_dir": str(skills_path)}

    # Local import avoids a module import cycle (provider -> overrides; inventory
    # -> provider). ``build_skill_source`` returns the UNFILTERED, deduplicated
    # file source, so names match exactly what the FilteringSkillsSource predicate
    # gates against.
    from app.skills.provider import build_skill_source

    source = build_skill_source()
    skills = await source.get_skills()
    store = get_skills_override_store()
    loaded = get_loaded_skills()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        name = skill.frontmatter.name
        description = (skill.frontmatter.description or "").strip()
        skill_dir = Path(getattr(skill, "path", "") or "")
        group = _group_for(skill_dir, skills_path)
        entry: dict[str, Any] = {
            "name": name,
            "description": description,
            "enabled": store.is_enabled(name),
            "loaded": name in loaded,
        }
        # Optional slash-command metadata (CTR-0043 v-note, PRP-0088); additive.
        entry.update(_read_command_meta(skill_dir))
        grouped.setdefault(group, []).append(entry)

    # Collision scan: duplicate basenames among all discovered SKILL.md dirs.
    seen: set[str] = set()
    collisions: set[str] = set()
    for skill_dir in _discover_skill_dirs(skills_path):
        base = skill_dir.name
        if base in seen:
            collisions.add(base)
        seen.add(base)

    # Named groups alphabetically first, the ungrouped bucket ("") last; skills
    # within a group sorted by name for a stable UI.
    groups_out = [
        {"name": group, "skills": sorted(grouped[group], key=lambda s: s["name"].lower())}
        for group in sorted(grouped, key=lambda g: (g == "", g.lower()))
    ]
    return {"groups": groups_out, "collisions": sorted(collisions), "skills_dir": str(skills_path)}


__all__ = ["get_skills_inventory"]
