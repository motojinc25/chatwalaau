"""In-memory Agent Skills override store (CTR-0043, PRP-0087, UDR-0065 D4).

Holds the operator's runtime selection of which Agent Skills are DISABLED. The
store is process-local and is NEVER persisted: on restart every Skill is enabled
again, so the default (empty) state is byte-for-byte the pre-PRP-0087 behaviour
(UDR-0065 D4/D7).

The disable UNIT is the skill NAME (the SKILL.md frontmatter ``name``, which MAF
requires to equal the skill's directory basename). The store records only the
DISABLED names as a flat set; the live set of available skills comes from
filesystem discovery (``app.skills.inventory``), so the store never goes stale
against a changed skills directory. A folder "group" is a UI organization concept
derived from the path and is NOT part of the persisted identity (UDR-0065 D2).

Concurrency: a small ``threading.Lock`` guards the plain-set reads/writes. The
heavier apply path (agent rebuild) is serialised separately by the AgentRegistry's
asyncio lock (CTR-0070, UDR-0065 D5).
"""

from __future__ import annotations

import threading


class SkillsOverrideStore:
    """Process-local record of disabled Agent Skill names."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._disabled: set[str] = set()

    def is_enabled(self, name: str) -> bool:
        """Return True when a skill (by name) is currently exposed."""
        with self._lock:
            return name not in self._disabled

    def disabled_names(self) -> set[str]:
        """Return a copy of the disabled skill-name set."""
        with self._lock:
            return set(self._disabled)

    def set_disabled(self, names: set[str] | None) -> None:
        """Replace the whole disabled set atomically."""
        with self._lock:
            self._disabled = set(names or set())

    def reset(self) -> None:
        """Clear all overrides (all Skills enabled). Used by tests."""
        with self._lock:
            self._disabled = set()

    def snapshot(self) -> set[str]:
        """Return a copy of the current disabled set for rollback/diagnostics."""
        with self._lock:
            return set(self._disabled)


_store = SkillsOverrideStore()


def get_skills_override_store() -> SkillsOverrideStore:
    """Return the process-wide Skills override store singleton."""
    return _store


__all__ = ["SkillsOverrideStore", "get_skills_override_store"]
