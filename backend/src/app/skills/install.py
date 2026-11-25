"""Install, reinstall and uninstall one Agent Skill (CTR-0205, PRP-0165).

Installing a skill is EXECUTING CODE, eventually (UDR-0144). A skill may ship
scripts that ``run_skill_script`` will run on this server, so every refusal in
this module is a deliberate one and none of them is a nuisance check:

- The target is resolved in the CATALOG. An id that is not in a catalog built
  from the active source table (the curated table plus the sources an
  administrator added, PRP-0266) cannot be installed; there is no path from a URL
  to this function that skips adding the source first (UDR-0247 D1).
- A NAME COLLISION across groups is refused outright, with no ``force``. MAF keeps
  the first of any duplicate skill name and silently drops the rest, so a forced
  install would write a folder that the agent never loads and leave the operator
  with no way to see why (UDR-0146 D3).
- LOCAL MODIFICATIONS are refused unless ``force`` is set, which is what turns the
  UI's "This skill has local changes. Overwrite?" into an actual decision.
- The download is bounded (``SKILL_INSTALL_MAX_BYTES`` / ``_FILES``) and every
  member path is validated before a single byte is written.

Nothing destructive happens until the staged copy is complete and valid: files
land in ``<SKILLS_DIR>/.skills-staging/<uuid>/``, and only then is the existing
skill directory deleted and the staged one moved into place. The delete-then-move
order is what keeps a file that upstream removed from surviving a reinstall.

PRP-0266 adds the reviewed update: ``diff_skill`` compares the catalog commit with
the installed folder, and ``install_skill(expected_revision=...)`` refuses with
``409 revision_changed`` when the catalog moved after that diff was shown
(UDR-0247 D4).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from pathlib import Path, PurePosixPath
import shutil
import uuid

from app.core.config import settings
from app.skills import catalog as catalog_mod
from app.skills import state as state_mod
from app.skills.hashing import hash_members, hash_skill_dir, is_ignored_relpath, iter_skill_files
from app.skills.sources import CatalogEntry, SourceError, build_source, load_source_defs
from app.skills.sources.github import DEPENDENCY_FILES

_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".js", ".mjs", ".cjs")

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"


class InstallError(Exception):
    """A refusal with a machine-readable code the UI turns into a decision."""

    def __init__(self, code: str, message: str, *, status: int = 400, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}

    def payload(self) -> dict:
        return {"error": self.code, "message": self.message, **self.detail}


def _require_root() -> Path:
    root = state_mod.skills_root()
    if root is None:
        raise InstallError(
            "skills_dir_unset",
            "SKILLS_DIR is empty, so there is nowhere to install a skill.",
        )
    return root


def _validate_members(entry: CatalogEntry, members: dict[str, bytes]) -> dict[str, bytes]:
    """Reject unsafe or over-cap payloads; drop generated artefacts."""
    return validate_members(entry.id, members)


def validate_members(label: str, members: dict[str, bytes]) -> dict[str, bytes]:
    """The install payload checks, shared with skill authoring (CTR-0289)."""
    clean: dict[str, bytes] = {}
    total = 0
    for rel, data in members.items():
        posix = rel.replace("\\", "/")
        pure = PurePosixPath(posix)
        if pure.is_absolute() or any(part in ("..", "") for part in pure.parts) or ":" in posix:
            raise InstallError(
                "unsafe_path",
                f"{label}: the skill contains an unsafe path ({rel!r}).",
            )
        if is_ignored_relpath(posix):
            continue
        clean[posix] = data
        total += len(data)

    if not clean:
        raise InstallError("empty_skill", f"{label}: the skill has no files.")
    if len(clean) > settings.skill_install_max_files:
        raise InstallError(
            "too_many_files",
            f"{label}: {len(clean)} files exceeds SKILL_INSTALL_MAX_FILES ({settings.skill_install_max_files}).",
        )
    if total > settings.skill_install_max_bytes:
        raise InstallError(
            "too_large",
            f"{label}: {total} bytes exceeds SKILL_INSTALL_MAX_BYTES ({settings.skill_install_max_bytes}).",
        )
    if not (clean.get(_SKILL_FILE) or b"").strip():
        raise InstallError(
            "no_skill_md",
            f"{label}: the skill has no readable {_SKILL_FILE}.",
        )
    return clean


def collision(root: Path, name: str, target: Path) -> str | None:
    """Public alias of the name-collision check, shared with skill authoring."""
    return _collision(root, name, target)


def _collision(root: Path, name: str, target: Path) -> str | None:
    """Return the path of an already-installed skill with the same NAME, if any.

    Uses the same discovery MAF uses, so the answer is about the set the runtime
    actually loads rather than a hand-written imitation of it.
    """
    from app.skills.inventory import discover_skill_dirs

    try:
        target_resolved = target.resolve()
    except OSError:
        target_resolved = target
    for skill_dir in discover_skill_dirs(root):
        if skill_dir.name != name:
            continue
        try:
            if skill_dir.resolve() == target_resolved:
                continue
        except OSError:
            pass
        return str(skill_dir)
    return None


async def write_skill_dir(members: dict[str, bytes], target: Path) -> None:
    """Stage ``members`` and swap them into ``target`` (the install write path)."""
    staged: Path | None = None
    try:
        staged = await asyncio.to_thread(_stage, members)
        await asyncio.to_thread(_swap, staged, target)
        staged = None
    finally:
        if staged is not None and staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        staging = state_mod.staging_root()
        if staging is not None and staging.is_dir() and not any(staging.iterdir()):
            with contextlib.suppress(OSError):
                staging.rmdir()


def _stage(members: dict[str, bytes]) -> Path:
    """Write the validated payload into a fresh staging directory."""
    staging = state_mod.staging_root()
    if staging is None:
        raise InstallError("skills_dir_unset", "SKILLS_DIR is empty, so there is nowhere to stage a skill.")
    staged = staging / uuid.uuid4().hex
    staged.mkdir(parents=True, exist_ok=False)
    for rel, data in members.items():
        path = staged / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return staged


def _swap(staged: Path, target: Path) -> None:
    """Delete the existing skill directory, then move the staged one into place."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    staged.replace(target)


def _source_for(entry: CatalogEntry):
    """Build the adapter that owns this entry, or refuse when it is gone.

    A catalog written under a different source table can outlive that table (the
    operator edited SKILL_CATALOG_SOURCES_FILE). Refusing here keeps the closed
    table authoritative at INSTALL time, not just at refresh time.
    """
    for definition in load_source_defs():
        if definition.id == entry.source_id:
            return build_source(definition)
    raise InstallError(
        "unknown_source",
        f"{entry.id}: source {entry.source_id!r} is not in the active source table.",
        status=404,
    )


async def install_skill(
    skill_id: str, *, force: bool = False, expected_revision: str | None = None
) -> state_mod.InstalledSkill:
    """Install or reinstall one catalog skill. Raises InstallError on refusal.

    ``expected_revision`` is the revision the update diff showed. When the catalog
    has moved since, nothing is written (UDR-0247 D4).
    """
    if not catalog_mod.install_available():
        raise InstallError(
            "install_disabled",
            "Skill installation is disabled in this deployment.",
            status=403,
        )

    root = _require_root()
    entry = catalog_mod.catalog_entries().get(skill_id)
    if entry is None:
        raise InstallError(
            "unknown_skill",
            f"{skill_id} is not in the current catalog. Refresh the catalog and try again.",
            status=404,
        )
    if expected_revision is not None and expected_revision != entry.revision:
        raise InstallError(
            "revision_changed",
            f"{entry.id} changed in the catalog after the update was reviewed. Review it again.",
            status=409,
            detail={"revision": entry.revision},
        )

    target = state_mod.skill_install_dir(entry.group, entry.name)
    if target is None:
        raise InstallError("skills_dir_unset", "SKILLS_DIR is empty, so there is nowhere to install a skill.")

    collided = _collision(root, entry.name, target)
    if collided:
        raise InstallError(
            "name_collision",
            f"A skill named {entry.name!r} is already installed at {collided}. Agent Skills are "
            "identified by name, so installing a second one would leave this copy unused. Remove "
            "the existing skill first.",
            status=409,
            detail={"existing_path": collided, "name": entry.name},
        )

    state = state_mod.read_state()
    previous = state.installed.get(skill_id)
    if previous is not None and target.is_dir() and not force:
        current_hash, _files = hash_skill_dir(target)
        if previous.content_hash and current_hash and current_hash != previous.content_hash:
            raise InstallError(
                "local_modifications",
                f"{entry.id} has local changes that a reinstall would overwrite.",
                status=409,
                detail={"name": entry.name},
            )

    source = _source_for(entry)
    try:
        fetched = await source.fetch_skill(entry)
    except SourceError as exc:
        raise InstallError("fetch_failed", str(exc), status=502) from exc

    members = _validate_members(entry, fetched.members)

    await write_skill_dir(members, target)

    record = state_mod.InstalledSkill(
        id=entry.id,
        name=entry.name,
        group=entry.group,
        source_id=entry.source_id,
        repo=entry.repo,
        repo_path=entry.repo_path,
        source_commit=fetched.source_commit or entry.source_commit,
        source_revision=fetched.revision or entry.revision,
        content_hash=hash_members(members),
        files=sorted(members),
        license=entry.license,
        installed_at=state_mod.now_iso(),
        trust=entry.trust,
        ref=entry.ref,
    )
    state_mod.record_install(record)
    logger.info(
        "Skill installed: %s -> %s (%d file(s), revision=%s)",
        entry.id,
        target,
        len(members),
        record.source_revision or "unknown",
    )
    return record


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dependency_file(rel: str) -> bool:
    return PurePosixPath(rel).name in DEPENDENCY_FILES


async def diff_skill(skill_id: str) -> dict:
    """Compare the catalog commit of one skill with what is installed (PRP-0266 C2).

    Downloads the catalog commit (one archive) and reports ``added``, ``changed`` and
    ``removed`` paths with sizes, ``scripts_added``, ``dependencies_changed`` and
    ``locally_modified``. Nothing is written. A skill that is not installed yet
    reports every file as added, so the same dialog reviews a first install.
    """
    entry = catalog_mod.catalog_entries().get(skill_id)
    if entry is None:
        raise InstallError(
            "unknown_skill",
            f"{skill_id} is not in the current catalog. Refresh the catalog and try again.",
            status=404,
        )
    source = _source_for(entry)
    try:
        fetched = await source.fetch_skill(entry)
    except SourceError as exc:
        raise InstallError("fetch_failed", str(exc), status=502) from exc
    incoming = _validate_members(entry, fetched.members)

    record = state_mod.read_state().installed.get(skill_id)
    target = state_mod.skill_install_dir(entry.group, entry.name)
    current: dict[str, bytes] = {}
    if target is not None and target.is_dir():
        for rel in iter_skill_files(target):
            try:
                current[rel] = (target / rel).read_bytes()
            except OSError:
                continue

    added = sorted(set(incoming) - set(current))
    removed = sorted(set(current) - set(incoming))
    changed = sorted(rel for rel in set(incoming) & set(current) if _digest(incoming[rel]) != _digest(current[rel]))
    locally_modified = False
    if record is not None and current and record.content_hash:
        locally_modified = hash_members(current) != record.content_hash

    return {
        "id": entry.id,
        "name": entry.name,
        "installed": record is not None,
        "trust": entry.trust,
        "repo": entry.repo,
        "ref": entry.ref,
        "commit": fetched.source_commit or entry.source_commit,
        "revision": entry.revision,
        "installed_revision": record.source_revision if record else "",
        "added": [{"path": rel, "size": len(incoming[rel])} for rel in added],
        "changed": [{"path": rel, "size": len(incoming[rel])} for rel in changed],
        "removed": [{"path": rel, "size": len(current[rel])} for rel in removed],
        "scripts_added": [rel for rel in added if rel.lower().endswith(_SCRIPT_SUFFIXES)],
        "dependencies_changed": [rel for rel in added + changed + removed if _dependency_file(rel)],
        "locally_modified": locally_modified,
        "skill_md": (incoming.get(_SKILL_FILE) or b"")[:4000].decode("utf-8", errors="replace"),
    }


async def uninstall_skill(skill_id: str) -> state_mod.InstalledSkill:
    """Remove one installed skill and its ledger row."""
    if not catalog_mod.install_available():
        raise InstallError(
            "install_disabled",
            "Skill installation is disabled in this deployment.",
            status=403,
        )

    state = state_mod.read_state()
    record = state.installed.get(skill_id)
    if record is None:
        raise InstallError("not_installed", f"{skill_id} is not installed.", status=404)

    target = state_mod.skill_install_dir(record.group, record.name)
    if target is not None and target.is_dir():
        try:
            await asyncio.to_thread(shutil.rmtree, target)
        except OSError as exc:
            raise InstallError(
                "remove_failed",
                f"{skill_id}: the skill directory could not be removed ({exc.__class__.__name__}).",
                status=500,
            ) from exc

    state_mod.drop_install(skill_id, name=record.name)
    logger.info("Skill uninstalled: %s (%s)", skill_id, target)
    return record


__all__ = [
    "InstallError",
    "collision",
    "diff_skill",
    "install_skill",
    "uninstall_skill",
    "validate_members",
    "write_skill_dir",
]
