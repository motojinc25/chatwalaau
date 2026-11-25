"""Skills written in the product (CTR-0289, FEAT-0091, PRP-0266 C5, UDR-0247 D5).

A written skill lives at ``<SKILLS_DIR>/local/<name>/``. Its ledger row has
``source_id: local`` and ``trust: local``: no upstream, no update. Group ``local``
is reserved, so no added source can ever overwrite one.

Three rules shape this module.

- **One save is one write.** ``save_local`` takes the whole change set (files and
  deletions) and goes through the install write path (validate, stage, swap), so the
  router can answer it with ONE agent rebuild and ONE harness cache clear.
- **Installed skills are not edited in place.** Editing one would only mark it
  "Locally modified" and lose the next update. ``duplicate`` copies it into
  ``local`` under a new name instead.
- **The MAF rules hold.** The frontmatter ``name`` equals the folder name (lowercase
  letters, digits and hyphens, at most 64), ``description`` is not empty, and the
  name is unique across groups (UDR-0146's 409).

Every write re-checks ``install_available()`` (demo mode, ``skill_install_enabled``)
and is reached only through ``verify_key_admin`` routes.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

from app.core.config import settings
from app.skills import catalog as catalog_mod
from app.skills import state as state_mod
from app.skills.frontmatter import parse_frontmatter
from app.skills.hashing import hash_members, iter_skill_files
from app.skills.install import InstallError, collision, validate_members, write_skill_dir
from app.skills.sources import LOCAL_GROUP, TRUST_LOCAL

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
# A ZIP is read into memory once; bound it before the per-skill caps apply.
_ZIP_SLACK = 2


def _require_writable() -> None:
    if not catalog_mod.install_available():
        raise InstallError("install_disabled", "Skill installation is disabled in this deployment.", status=403)


def _require_root() -> Path:
    root = state_mod.skills_root()
    if root is None:
        raise InstallError("skills_dir_unset", "SKILLS_DIR is empty, so there is nowhere to write a skill.")
    return root


def validate_name(name: str) -> str:
    """Return ``name`` when it is a usable skill name, else raise 400."""
    value = (name or "").strip()
    if not value or len(value) > MAX_NAME or not NAME_RE.match(value):
        raise InstallError(
            "invalid_name",
            "A skill name uses lowercase letters, digits and single hyphens (at most 64 characters).",
            status=400,
        )
    return value


def _local_dir(name: str) -> Path:
    target = state_mod.skill_install_dir(LOCAL_GROUP, name)
    if target is None:
        raise InstallError("skills_dir_unset", "SKILLS_DIR is empty, so there is nowhere to write a skill.")
    return target


def _yaml_string(value: str) -> str:
    """A one-line YAML scalar. A JSON string is a valid YAML double-quoted scalar."""
    return json.dumps(" ".join((value or "").split()), ensure_ascii=False)


def build_skill_md(name: str, description: str, body: str) -> str:
    """The SKILL.md of a new skill: frontmatter plus the body."""
    text = (body or "").strip("\n")
    return f"---\nname: {name}\ndescription: {_yaml_string(description)}\n---\n\n{text}\n"


def _set_frontmatter_name(text: str, name: str) -> str:
    """Rewrite (or insert) the frontmatter ``name`` line."""
    if text.lstrip().startswith("---"):
        lines = text.splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == "---")
        for i in range(start + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped == "---":
                lines.insert(i, f"name: {name}\n")
                return "".join(lines)
            if re.match(r"^name\s*:", stripped):
                lines[i] = f"name: {name}\n"
                return "".join(lines)
    return f"---\nname: {name}\n---\n\n{text}"


def _check_skill_md(name: str, members: dict[str, bytes]) -> None:
    text = (members.get(SKILL_FILE) or b"").decode("utf-8", errors="replace")
    front = parse_frontmatter(text)
    if front.get("name") != name:
        raise InstallError(
            "name_mismatch",
            f"The frontmatter name in SKILL.md must be {name!r}, the folder name.",
            status=400,
        )
    if not front.get("description"):
        raise InstallError("no_description", "SKILL.md needs a description in its frontmatter.", status=400)


def _check_collision(name: str, target: Path) -> None:
    collided = collision(_require_root(), name, target)
    if collided:
        raise InstallError(
            "name_collision",
            f"A skill named {name!r} already exists at {collided}. Choose another name.",
            status=409,
            detail={"existing_path": collided, "name": name},
        )


def _read_dir(target: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for rel in iter_skill_files(target):
        try:
            out[rel] = (target / rel).read_bytes()
        except OSError:
            continue
    return out


def _record(name: str, members: dict[str, bytes], *, created_at: str = "") -> state_mod.InstalledSkill:
    """The ledger row of a written skill: no upstream, a fresh hash after each save."""
    record = state_mod.InstalledSkill(
        id=f"{LOCAL_GROUP}/{name}",
        name=name,
        group=LOCAL_GROUP,
        source_id=LOCAL_GROUP,
        content_hash=hash_members(members),
        files=sorted(members),
        installed_at=created_at or state_mod.now_iso(),
        trust=TRUST_LOCAL,
    )
    state_mod.record_install(record)
    return record


async def _write_new(name: str, members: dict[str, bytes]) -> state_mod.InstalledSkill:
    target = _local_dir(name)
    if target.exists():
        raise InstallError("skill_exists", f"local/{name} already exists.", status=409, detail={"name": name})
    _check_collision(name, target)
    clean = validate_members(f"local/{name}", members)
    _check_skill_md(name, clean)
    await write_skill_dir(clean, target)
    logger.info("Local skill written: local/%s (%d file(s))", name, len(clean))
    return _record(name, clean)


async def create_local(name: str, description: str, body: str) -> state_mod.InstalledSkill:
    """Create ``local/<name>`` with a SKILL.md built from the inputs."""
    _require_writable()
    name = validate_name(name)
    if not (description or "").strip():
        raise InstallError("no_description", "A skill needs a description.", status=400)
    return await _write_new(name, {SKILL_FILE: build_skill_md(name, description, body).encode("utf-8")})


async def duplicate(source_id: str, name: str) -> state_mod.InstalledSkill:
    """Copy an installed or written skill into ``local/<name>`` and rename it."""
    _require_writable()
    name = validate_name(name)
    group, _, base = (source_id or "").rpartition("/")
    source_dir = state_mod.skill_install_dir(group, base) if base else None
    if source_dir is None or not source_dir.is_dir() or not (source_dir / SKILL_FILE).is_file():
        raise InstallError("unknown_skill", f"{source_id} is not a skill on disk.", status=404)
    members = _read_dir(source_dir)
    text = (members.get(SKILL_FILE) or b"").decode("utf-8", errors="replace")
    members[SKILL_FILE] = _set_frontmatter_name(text, name).encode("utf-8")
    return await _write_new(name, members)


def get_local(name: str) -> dict:
    """The file list and the UTF-8 text files of ``local/<name>``."""
    name = validate_name(name)
    target = _local_dir(name)
    if not target.is_dir():
        raise InstallError("unknown_skill", f"local/{name} does not exist.", status=404)
    files = []
    for rel, data in sorted(_read_dir(target).items()):
        try:
            text: str | None = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        files.append({"path": rel, "size": len(data), "text": text, "binary": text is None})
    return {"id": f"{LOCAL_GROUP}/{name}", "name": name, "files": files}


async def save_local(name: str, files: dict[str, str], deleted: list[str]) -> state_mod.InstalledSkill:
    """Apply one change set (text files written, paths deleted) to ``local/<name>``."""
    _require_writable()
    name = validate_name(name)
    target = _local_dir(name)
    if not target.is_dir():
        raise InstallError("unknown_skill", f"local/{name} does not exist.", status=404)
    members = _read_dir(target)
    for rel in deleted or []:
        members.pop(str(rel).replace("\\", "/"), None)
    for rel, text in (files or {}).items():
        members[str(rel).replace("\\", "/")] = str(text).encode("utf-8")
    clean = validate_members(f"local/{name}", members)
    _check_skill_md(name, clean)
    await write_skill_dir(clean, target)
    previous = state_mod.read_state().installed.get(f"{LOCAL_GROUP}/{name}")
    logger.info("Local skill saved: local/%s (%d file(s))", name, len(clean))
    return _record(name, clean, created_at=previous.installed_at if previous else "")


async def delete_local(name: str) -> state_mod.InstalledSkill | None:
    """Remove ``local/<name>`` and its ledger row."""
    _require_writable()
    name = validate_name(name)
    target = _local_dir(name)
    record = state_mod.read_state().installed.get(f"{LOCAL_GROUP}/{name}")
    if not target.is_dir() and record is None:
        raise InstallError("unknown_skill", f"local/{name} does not exist.", status=404)
    if target.is_dir():
        await asyncio.to_thread(shutil.rmtree, target)
    state_mod.drop_install(f"{LOCAL_GROUP}/{name}", name=name)
    logger.info("Local skill deleted: local/%s", name)
    return record


def _zip_members(data: bytes) -> tuple[str, dict[str, bytes]]:
    """Read one skill out of a ZIP: a single top folder, or SKILL.md at the root."""
    limit = settings.skill_install_max_bytes * _ZIP_SLACK
    if len(data) > limit:
        raise InstallError("too_large", f"The ZIP is larger than {limit} bytes.", status=400)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InstallError("bad_zip", "The file is not a ZIP archive.", status=400) from exc
    raw: dict[str, bytes] = {}
    total = 0
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > settings.skill_install_max_files:
            raise InstallError("too_many_files", "The ZIP holds too many files.", status=400)
        for info in infos:
            total += info.file_size
            if total > settings.skill_install_max_bytes:
                raise InstallError("too_large", "The ZIP expands beyond SKILL_INSTALL_MAX_BYTES.", status=400)
            # Symlinks stored in a ZIP carry the S_IFLNK bit in the high word.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue
            raw[info.filename.replace("\\", "/")] = archive.read(info)
    if SKILL_FILE in raw:
        return "", raw
    tops = {PurePosixPath(rel).parts[0] for rel in raw if PurePosixPath(rel).parts}
    if len(tops) == 1:
        top = next(iter(tops))
        prefix = top + "/"
        inner = {rel[len(prefix) :]: body for rel, body in raw.items() if rel.startswith(prefix)}
        if SKILL_FILE in inner:
            return top, inner
    raise InstallError(
        "no_skill_md", "The ZIP must hold one skill: SKILL.md at its root or in one top folder.", status=400
    )


async def import_zip(data: bytes, name: str = "") -> state_mod.InstalledSkill:
    """Create ``local/<name>`` from a ZIP (the name defaults to the frontmatter name)."""
    _require_writable()
    top, members = _zip_members(data)
    front = parse_frontmatter((members.get(SKILL_FILE) or b"").decode("utf-8", errors="replace"))
    chosen = validate_name((name or "").strip() or front.get("name", "") or top)
    if front.get("name") != chosen:
        text = (members.get(SKILL_FILE) or b"").decode("utf-8", errors="replace")
        members[SKILL_FILE] = _set_frontmatter_name(text, chosen).encode("utf-8")
    return await _write_new(chosen, members)


def export_zip(group: str, name: str) -> tuple[str, bytes]:
    """ZIP one skill on disk as ``<name>/...``. Returns (file name, bytes)."""
    target = (
        state_mod.skill_install_dir(group, name) if name and "/" not in name and ".." not in (group, name) else None
    )
    if target is None or not target.is_dir() or not (target / SKILL_FILE).is_file():
        raise InstallError("unknown_skill", f"{group}/{name} is not a skill on disk.", status=404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel, data in sorted(_read_dir(target).items()):
            archive.writestr(f"{name}/{rel}", data)
    return f"{name}.zip", buffer.getvalue()


__all__ = [
    "build_skill_md",
    "create_local",
    "delete_local",
    "duplicate",
    "export_zip",
    "get_local",
    "import_zip",
    "save_local",
    "validate_name",
]
