"""Process-local snapshot of the skills baked into the current live agent build.

PRP-0090 / UDR-0068 D4. The Skills inventory marks a skill ``loaded`` iff its name
is in this snapshot, which is refreshed every time ``create_skills_provider()``
builds a provider (startup build + every agent rebuild / reload). A skill added to
SKILLS_DIR AFTER the last build is discovered by the inventory (it re-reads disk)
but is NOT in this snapshot, so the UI shows it with a disabled enable toggle until
the operator runs Reload (CTR-0123).

Concurrency: a small ``threading.Lock`` guards the plain-set read/write. The set is
never persisted -- on restart it is empty until the first build runs.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_loaded: set[str] = set()


def set_loaded_skills(names: set[str] | None) -> None:
    """Record the skill names present in the current live agent build."""
    global _loaded
    with _lock:
        _loaded = set(names or set())


def get_loaded_skills() -> set[str]:
    """Return a copy of the skill names baked into the current live agent build."""
    with _lock:
        return set(_loaded)


def reset() -> None:
    """Clear the snapshot (used by tests)."""
    global _loaded
    with _lock:
        _loaded = set()


__all__ = ["get_loaded_skills", "reset", "set_loaded_skills"]
