"""Durable Agent Skills state: install ledger + gating selection (CTR-0206, PRP-0165).

`SKILLS_DIR` is the durable root for everything the product writes about Skills
(UDR-0143). This module owns the one file inside it that survives a restart:

    <SKILLS_DIR>/.skills-state.json
    {
      "schema_version": 1,
      "disabled": ["pdf"],                     # gating selection (UDR-0148)
      "installed": [ {...ledger entry...} ]    # install ledger  (CTR-0206)
    }

Two decisions are load-bearing here.

1. The file is a dot-FILE, not a directory. MAF's discovery walk recurses into
   ``entry.is_dir()`` only, so a file inside SKILLS_DIR is invisible to it while a
   dot-DIRECTORY would be walked (UDR-0143 D3). The transient staging area IS a
   directory and therefore has to be created, used and removed inside one install
   call -- plus swept at startup, before the first provider build, in case a crash
   left one behind.
2. Absent or unreadable means "nothing disabled, nothing installed". That is
   byte-for-byte the pre-PRP-0165 behaviour, which is what makes the upgrade
   silent: a fleet that never opens the Skills screen never grows the file.

Persistence is NOT a promise this module makes. It writes to the configured
directory and reports honestly when what it recorded is no longer there: a ledger
entry whose folder has vanished is ``missing`` (an ephemeral container, an
unmounted volume), not an error and not a silent deletion.

Writes are atomic (tmp + ``os.replace``). A half-written ledger is the one failure
that would strand installed folders with no record of where they came from.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import shutil
import threading
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STAGING_DIR_NAME = ".skills-staging"

_lock = threading.Lock()


@dataclass
class InstalledSkill:
    """One ledger row: what was installed, from where, and what it looked like."""

    id: str
    name: str
    group: str
    source_id: str
    repo: str = ""
    repo_path: str = ""
    source_commit: str = ""
    source_revision: str = ""
    content_hash: str = ""
    files: list[str] = field(default_factory=list)
    license: dict[str, Any] | None = None
    installed_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "source_id": self.source_id,
            "repo": self.repo,
            "repo_path": self.repo_path,
            "source_commit": self.source_commit,
            "source_revision": self.source_revision,
            "content_hash": self.content_hash,
            "files": list(self.files),
            "license": self.license,
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> InstalledSkill | None:
        """Parse one row, or None when it is unusable.

        A row without an id and a name cannot be addressed by any endpoint, so it
        is dropped rather than kept as an entry the UI can render but not act on.
        """
        skill_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not skill_id or not name:
            return None
        license_raw = raw.get("license")
        return cls(
            id=skill_id,
            name=name,
            group=str(raw.get("group") or ""),
            source_id=str(raw.get("source_id") or ""),
            repo=str(raw.get("repo") or ""),
            repo_path=str(raw.get("repo_path") or ""),
            source_commit=str(raw.get("source_commit") or ""),
            source_revision=str(raw.get("source_revision") or ""),
            content_hash=str(raw.get("content_hash") or ""),
            files=[str(f) for f in (raw.get("files") or []) if isinstance(f, str)],
            license=license_raw if isinstance(license_raw, dict) else None,
            installed_at=str(raw.get("installed_at") or ""),
        )


@dataclass
class SkillsState:
    """The whole durable document."""

    disabled: set[str] = field(default_factory=set)
    installed: dict[str, InstalledSkill] = field(default_factory=dict)
    present: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "disabled": sorted(self.disabled),
            "installed": [entry.to_json() for entry in sorted(self.installed.values(), key=lambda e: e.id)],
        }


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def skills_root() -> Path | None:
    """The configured Skills directory, or None when SKILLS_DIR is empty.

    Delegates to the provider's resolver so "absent" means exactly one thing
    across the codebase (UDR-0130 D1/D2): an empty SKILLS_DIR is "no Skills
    directory", not ``Path(".")``.
    """
    from app.skills.provider import skills_root as _root

    return _root()


def _resolve_under_root(raw: str) -> Path | None:
    """Resolve a configured file name under SKILLS_DIR; absolute paths win."""
    name = (raw or "").strip()
    if not name:
        return None
    path = Path(name)
    if path.is_absolute():
        return path
    root = skills_root()
    return (root / path) if root is not None else None


def state_path() -> Path | None:
    """Path of the durable state file, or None when there is no Skills directory."""
    return _resolve_under_root(settings.skill_state_file)


def catalog_path() -> Path | None:
    """Path of the catalog snapshot file, or None when there is no Skills directory."""
    return _resolve_under_root(settings.skill_catalog_file)


def staging_root() -> Path | None:
    """Transient extraction area, always inside SKILLS_DIR so the final move is atomic."""
    root = skills_root()
    return (root / STAGING_DIR_NAME) if root is not None else None


def skill_install_dir(group: str, name: str) -> Path | None:
    """Where a skill with this group and name is installed.

    ``<SKILLS_DIR>/<group>/<name>/`` -- depth 2, which is the ONLY depth MAF
    discovers (``MAX_SEARCH_DEPTH = 2``). Upstream dot-directories such as
    ``skills/.curated`` are normalized away by the source table's group, not
    reproduced on disk, because a skill at depth 3 is never found (UDR-0146 D1).
    """
    root = skills_root()
    if root is None:
        return None
    return (root / group / name) if group else (root / name)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def read_state() -> SkillsState:
    """Load the durable state. Absent or unreadable yields the empty document."""
    path = state_path()
    if path is None or not path.is_file():
        return SkillsState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Skills state file is unreadable: %s (treating as empty)", path, exc_info=True)
        return SkillsState()
    if not isinstance(raw, dict):
        logger.warning("Skills state file is not an object: %s (treating as empty)", path)
        return SkillsState()

    disabled = {str(n) for n in (raw.get("disabled") or []) if isinstance(n, str) and n.strip()}
    installed: dict[str, InstalledSkill] = {}
    for row in raw.get("installed") or []:
        if not isinstance(row, dict):
            continue
        entry = InstalledSkill.from_json(row)
        if entry is not None:
            installed[entry.id] = entry
    return SkillsState(disabled=disabled, installed=installed, present=True)


def write_state(state: SkillsState) -> None:
    """Persist the state atomically. A missing Skills directory is a no-op."""
    path = state_path()
    if path is None:
        return
    payload = json.dumps(state.to_json(), indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except OSError:
        logger.warning("Could not write the Skills state file: %s", path, exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def mutate(fn) -> SkillsState:
    """Read-modify-write the state under a lock, returning the written document."""
    with _lock:
        state = read_state()
        fn(state)
        write_state(state)
        return state


# ---------------------------------------------------------------------------
# Ledger + gating helpers
# ---------------------------------------------------------------------------


def installed_entries() -> dict[str, InstalledSkill]:
    """The install ledger, keyed by catalog id."""
    return read_state().installed


def record_install(entry: InstalledSkill) -> None:
    """Add or replace one ledger row."""

    def _apply(state: SkillsState) -> None:
        state.installed[entry.id] = entry

    mutate(_apply)


def drop_install(skill_id: str, *, name: str | None = None) -> None:
    """Remove one ledger row and any gating entry that named the same skill.

    Uninstall drops the preference with the skill, which is the same rule the
    Reload prune already applies to a skill that left the disk (UDR-0068 D2): the
    ledger never carries a preference for something that is not installed.
    """

    def _apply(state: SkillsState) -> None:
        state.installed.pop(skill_id, None)
        if name:
            state.disabled.discard(name)

    mutate(_apply)


def save_disabled(names: set[str]) -> None:
    """Persist the gating selection (UDR-0148)."""

    def _apply(state: SkillsState) -> None:
        state.disabled = set(names)

    mutate(_apply)


def now_iso() -> str:
    """UTC timestamp in the form the ledger and the catalog both use."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def cleanup_staging() -> None:
    """Delete a staging area left behind by a crashed install.

    It holds a ``SKILL.md`` mid-install and IS a directory, so leaving one in
    place would let a half-extracted skill be discovered as a real one.
    """
    staging = staging_root()
    if staging is None or not staging.is_dir():
        return
    try:
        shutil.rmtree(staging)
        logger.info("Removed a leftover Skills staging directory: %s", staging)
    except OSError:
        logger.warning("Could not remove the Skills staging directory: %s", staging, exc_info=True)


def load_state_into_stores() -> None:
    """Apply the persisted gating selection to the in-memory override store.

    Called at MODULE IMPORT time from ``app.main``, before the agent registry is
    constructed: ``create_skills_provider()`` reads the override store while it
    builds, so an apply deferred to lifespan would let the first build advertise
    skills the operator switched off (UDR-0148 D2).
    """
    cleanup_staging()

    state = read_state()
    if not state.present:
        return

    from app.skills.overrides import get_skills_override_store

    get_skills_override_store().set_disabled(state.disabled)
    logger.info(
        "Skills state loaded: %d installed, %d disabled%s",
        len(state.installed),
        len(state.disabled),
        f" ({', '.join(sorted(state.disabled))})" if state.disabled else "",
    )


__all__ = [
    "SCHEMA_VERSION",
    "STAGING_DIR_NAME",
    "InstalledSkill",
    "SkillsState",
    "catalog_path",
    "cleanup_staging",
    "drop_install",
    "installed_entries",
    "load_state_into_stores",
    "mutate",
    "now_iso",
    "read_state",
    "record_install",
    "save_disabled",
    "skill_install_dir",
    "skills_root",
    "staging_root",
    "state_path",
    "write_state",
]
