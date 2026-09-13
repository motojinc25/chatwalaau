"""Canonical content hash for an installed Agent Skill (CTR-0206, PRP-0165).

One hash answers ONE question: has the operator edited this skill since it was
installed? (The other question -- has upstream moved? -- is answered by comparing
the catalog's opaque ``revision`` against the ledger's ``source_revision``, and
never touches the disk. UDR-0145.)

The canonical form is:

    sha256( "\\n".join( f"{relpath}\\0{sha256_hex(bytes)}" for relpath in sorted ) )

with ``relpath`` relative to the skill directory and using ``/`` separators.
Three properties are fixed by decision (UDR-0145 D2/D3/D4):

- The PATH LIST is inside the hash, so an added or deleted file changes it. That
  is why the ledger stores a path list and one hash instead of a hash per file.
- File MODE is NOT hashed. Windows does not carry the POSIX execute bit and this
  repository's primary development platform is Windows, so hashing the mode would
  make every skill that ships a script report as locally modified the moment it
  round-trips through a Windows checkout.
- Bytes are hashed AS-IS -- no newline normalization. Byte equality is the only
  rule that never drifts, and a CRLF rewrite genuinely IS a local modification.

Generated artefacts are excluded (``_IGNORED``). ``run_skill_script`` executes a
skill's script with the skill's OWN directory as the working directory
(``app.skills.provider``), so CPython drops ``__pycache__/*.pyc`` inside an
installed skill the first time the agent runs it. Hashing those would report
"locally modified" for a skill nobody touched, on a schedule set by the agent
rather than the operator. The exclusion list is deliberately tiny: interpreter
and file-manager droppings only, never anything a skill author could ship on
purpose.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Directory names skipped wholesale, and file names/suffixes skipped individually.
_IGNORED_DIRS = frozenset({"__pycache__"})
_IGNORED_SUFFIXES = (".pyc", ".pyo")
_IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db"})

_HASH_PREFIX = "sha256:"


def is_ignored_relpath(relpath: str) -> bool:
    """True when a POSIX-separated relative path is a generated artefact."""
    parts = relpath.split("/")
    if any(part in _IGNORED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name in _IGNORED_NAMES or name.endswith(_IGNORED_SUFFIXES)


def iter_skill_files(skill_dir: Path) -> list[str]:
    """Return the hashable files under ``skill_dir`` as sorted POSIX relpaths.

    Symlinks are skipped: MAF's own discovery rejects symlinked entries (CTR-0043,
    UDR-0128 D4), so a symlink inside a skill is content the runtime will not read
    and must not be counted as content the operator changed.
    """
    if not skill_dir.is_dir():
        return []
    out: list[str] = []
    for path in skill_dir.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(skill_dir).as_posix()
        except (OSError, ValueError):
            continue
        if is_ignored_relpath(rel):
            continue
        out.append(rel)
    return sorted(out)


def hash_members(members: dict[str, bytes]) -> str:
    """Hash an in-memory {relpath: bytes} mapping in the canonical form."""
    lines = [f"{rel}\0{hashlib.sha256(data).hexdigest()}" for rel, data in sorted(members.items())]
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return f"{_HASH_PREFIX}{digest}"


def hash_skill_dir(skill_dir: Path) -> tuple[str, list[str]]:
    """Return ``(content_hash, relpaths)`` for an installed skill directory.

    An unreadable file makes the whole hash unknown rather than silently
    different: returning a hash computed over a partial read would report a
    permission problem as a local modification.
    """
    relpaths = iter_skill_files(skill_dir)
    lines: list[str] = []
    for rel in relpaths:
        try:
            data = (skill_dir / rel).read_bytes()
        except OSError:
            return ("", relpaths)
        lines.append(f"{rel}\0{hashlib.sha256(data).hexdigest()}")
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return (f"{_HASH_PREFIX}{digest}", relpaths)


__all__ = [
    "hash_members",
    "hash_skill_dir",
    "is_ignored_relpath",
    "iter_skill_files",
]
