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

    Shape: ``{"groups": [{"name", "skills": [{"name", "description", "enabled"}]}],
    "collisions": [name, ...]}``. The ungrouped bucket carries group name ``""``.
    Returns an empty inventory when SKILLS_DIR is absent (FEAT-0009 graceful
    degradation), so the UI hides the management icon.
    """
    skills_path = Path(settings.skills_dir)
    if not skills_path.is_dir():
        return {"groups": [], "collisions": []}

    # Local import avoids a module import cycle (provider -> overrides; inventory
    # -> provider). ``build_skill_source`` returns the UNFILTERED, deduplicated
    # file source, so names match exactly what the FilteringSkillsSource predicate
    # gates against.
    from app.skills.provider import build_skill_source

    source = build_skill_source()
    skills = await source.get_skills()
    store = get_skills_override_store()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        name = skill.frontmatter.name
        description = (skill.frontmatter.description or "").strip()
        group = _group_for(Path(getattr(skill, "path", "") or ""), skills_path)
        grouped.setdefault(group, []).append(
            {"name": name, "description": description, "enabled": store.is_enabled(name)}
        )

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
    return {"groups": groups_out, "collisions": sorted(collisions)}


__all__ = ["get_skills_inventory"]
