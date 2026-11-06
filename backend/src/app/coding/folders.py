"""Workspace folders: listing, creation and the harness project root (PRP-0244).

Shared by the AI discussion's code-folder picker (CTR-0277) and the harness project folders
(CTR-0194 v3, CTR-0280, UDR-0225 D1/D2). Pure file-system code, no model call:

- ``list_subfolders`` -- one level of sub-directories (never files, never contents); a link or
  junction is never listed or followed;
- ``create_folder`` -- one new, empty folder (one path segment);
- ``resolve_project_root`` -- the harness project root: relative, inside the CTR-0031 realpath
  jail, an existing directory, no link or junction on the way, not the file-memory folder.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

#: Directories never listed as a folder to choose (version control, dependencies, caches,
#: build output, chat data). The AI discussion's exclusion rule starts from the same set.
ALWAYS_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "out",
        "target",
        ".next",
        ".turbo",
        "coverage",
        ".sessions",
    }
)

#: The harness file memory (UDR-0162 D3, UDR-0225 D2): never a project, never listed.
FILE_MEMORY_DIR = "agent-file-memory"
RESERVED_DIRS: frozenset[str] = frozenset({FILE_MEMORY_DIR})

#: The longest project path accepted (UDR-0225 D2).
MAX_PATH_CHARS = 1024

_DRIVE = re.compile(r"^[A-Za-z]:")
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)


class FolderError(ValueError):
    """A folder path or name that is refused (the message names the reason)."""


class FolderNotFoundError(FolderError):
    """The folder does not exist (or is not a directory)."""


class FolderExistsError(FolderError):
    """A folder with that name already exists."""


def posix(rel: str) -> str:
    """A workspace-relative path in POSIX form; ``""`` is the workspace root."""
    rel = str(rel or "").replace("\\", "/").strip().strip("/")
    return "" if rel in ("", ".") else rel


def check_relative(raw: str) -> str:
    """The POSIX form of ``raw``, or ``FolderError`` for an absolute path, a drive or ``..``."""
    text = str(raw or "")
    if len(text) > MAX_PATH_CHARS:
        raise FolderError(f"folder path is longer than {MAX_PATH_CHARS} characters")
    stripped = text.strip()
    if stripped.startswith(("/", "\\")) or _DRIVE.match(stripped):
        raise FolderError("folder path must be relative to the workspace")
    rel = posix(stripped)
    if any(part in ("..", ".") for part in rel.split("/") if rel):
        raise FolderError("folder path must not contain '..' or '.' segments")
    return rel


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def is_link(path: str | Path) -> bool:
    """True for a symbolic link or a Windows junction / reparse point."""
    p = Path(path)
    try:
        if p.is_symlink():
            return True
        is_junction = getattr(p, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
    except OSError:
        return True
    return False


def resolve_dir(root: str, rel: str) -> tuple[str, str]:
    """``(absolute real path, workspace-relative POSIX path)`` of an existing folder.

    Refuses a path outside ``root`` (CTR-0031 realpath jail), a path that is not an existing
    directory, and a path reached through a link or junction anywhere below ``root``.
    """
    from app.coding.security import resolve_safe_path

    rel = check_relative(rel)
    root_real = os.path.realpath(root)
    try:
        target = resolve_safe_path(root_real, rel)
    except ValueError as exc:
        raise FolderError(str(exc)) from None
    # A link or junction resolves elsewhere, so the real path differs from the joined one.
    if not _same_path(target, str(Path(root_real, *rel.split("/"))) if rel else root_real):
        raise FolderError(f"folder path goes through a link: {rel}")
    if not Path(target).is_dir():
        raise FolderNotFoundError(f"folder not found: {rel or '(workspace)'}")
    real_rel = posix(os.path.relpath(target, root_real))
    return target, real_rel


def is_reserved(rel: str) -> bool:
    """True for the file-memory folder or anything inside it (UDR-0225 D2)."""
    first = posix(rel).split("/", 1)[0]
    return first.lower() in {name.lower() for name in RESERVED_DIRS}


def resolve_project_root(root: str, rel: str) -> tuple[str, str]:
    """The harness project root (UDR-0225 D2): ``resolve_dir`` plus the reserved-folder rule."""
    rel = check_relative(rel)
    if is_reserved(rel):
        raise FolderError(f"{FILE_MEMORY_DIR} holds the agent's file memory and cannot be a project folder")
    try:
        return resolve_dir(root, rel)
    except FolderNotFoundError:
        raise FolderNotFoundError(f"project folder not found: {rel or '(workspace)'}") from None


def list_subfolders(
    root: str,
    rel: str,
    *,
    cap: int,
    skip: Callable[[str], bool] | None = None,
    annotate: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One level of sub-directories of ``rel`` (never files, never contents).

    ``skip(child_rel)`` hides a folder; ``annotate(child_abs)`` adds fields to its entry.
    Links and junctions are never listed. Returns ``{dir, parent, dirs, truncated}`` where
    ``dirs`` holds names, or ``entries`` dicts when ``annotate`` is given.
    """
    abs_dir, rel = resolve_dir(root, rel)
    names: list[str] = []
    try:
        with os.scandir(abs_dir) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False) or is_link(entry.path):
                    continue
                child = f"{rel}/{entry.name}" if rel else entry.name
                if skip is not None and skip(child):
                    continue
                names.append(entry.name)
    except OSError as exc:
        raise FolderError(f"cannot list {rel or '(workspace)'}") from exc
    names.sort(key=str.lower)
    cap = max(1, int(cap))
    parent = None if not rel else "/".join(rel.split("/")[:-1])
    result: dict[str, Any] = {"dir": rel, "parent": parent, "dirs": names[:cap], "truncated": len(names) > cap}
    if annotate is not None:
        result["entries"] = [{"name": name, **annotate(str(Path(abs_dir, name)))} for name in names[:cap]]
    return result


def check_folder_name(name: str) -> str:
    """One new folder name: one path segment, no reserved or device name (FolderError)."""
    text = str(name or "").strip()
    if not text or text in (".", ".."):
        raise FolderError("folder name is empty")
    if len(text) > 255:
        raise FolderError("folder name is longer than 255 characters")
    if _BAD_NAME_CHARS.search(text) or text.endswith((".", " ")):
        raise FolderError('folder name must be one name without / \\ : * ? " < > | or a trailing dot')
    if text.split(".", 1)[0].lower() in _WINDOWS_DEVICE_NAMES:
        raise FolderError(f"folder name is reserved by Windows: {text}")
    return text


def create_folder(root: str, dir_rel: str, name: str, *, refuse: Callable[[str], bool] | None = None) -> str:
    """Create one empty folder ``name`` inside ``dir_rel``; returns its workspace-relative path."""
    abs_dir, rel = resolve_dir(root, dir_rel)
    name = check_folder_name(name)
    child = f"{rel}/{name}" if rel else name
    if refuse is not None and refuse(child):
        raise FolderError(f"a folder named {name} cannot be created here")
    try:
        Path(abs_dir, name).mkdir()
    except FileExistsError:
        raise FolderExistsError(f"folder already exists: {child}") from None
    except OSError as exc:
        raise FolderError(f"cannot create {child}") from exc
    return child


__all__ = [
    "ALWAYS_EXCLUDED_DIRS",
    "FILE_MEMORY_DIR",
    "MAX_PATH_CHARS",
    "RESERVED_DIRS",
    "FolderError",
    "FolderExistsError",
    "FolderNotFoundError",
    "check_folder_name",
    "check_relative",
    "create_folder",
    "is_link",
    "is_reserved",
    "list_subfolders",
    "posix",
    "resolve_dir",
    "resolve_project_root",
]
