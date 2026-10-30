"""AI Discussion code map and Code tool (CTR-0277, PRP-0236, UDR-0219 D1-D7).

A run may name one ``code_folder`` inside the coding workspace (``CODING_WORKSPACE_DIR``, the
CTR-0031 realpath jail). This module is pure code -- no model call anywhere:

- ``Exclusions`` -- the ONE exclusion rule the map and the Code tool share (D5): version-control,
  dependency, cache and build directories, the secret file list (never listed, outlined or read),
  binary and oversized files (listed, never read) and the folder's ``.gitignore`` files (a subset;
  unsupported syntax fails toward exclusion);
- ``build`` -- the code map, built once when the run starts (D2): the file list and a symbol
  outline (Python through the stdlib ``ast``; other languages are listed without symbols), a full
  map for Code seats and a short map for the others (D3), cached by a digest of the walked files;
- ``tool_functions`` -- the read-only Code tool (D4): ``file_outline``, ``file_glob``,
  ``file_grep`` and ``file_read``, rooted at the folder, reusing the coding tools' internals;
- ``check_citations`` -- ``path:line`` references in a reply checked against the folder (D6).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Annotated, Any

from pydantic import Field

logger = logging.getLogger(__name__)

#: Bumped when the symbol extraction changes, so a cached outline is rebuilt.
EXTRACTOR_VERSION = 1
#: Characters per token of the map's size estimate (the discussion budget's own estimate).
CHARS_PER_TOKEN = 3
#: Cached outlines kept on disk (oldest removed first).
CACHE_KEEP = 20
#: Citations checked per message.
MAX_CITATIONS = 30
#: The first bytes read to tell a binary file (a NUL byte) from text.
BINARY_PROBE_BYTES = 8192
DOC_CHARS = 120

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
#: Never listed, outlined or read (D5).
SECRET_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "*.keystore",
    ".npmrc",
    ".pypirc",
)

LANGUAGES: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".cs": "C#",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".sql": "SQL",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".md": "Markdown",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
}

CODE_LIMIT_REACHED = "Code read limit of this discussion reached; answer with what you have."


class CodeError(ValueError):
    """A code folder or path the Code feature refuses."""


# ---------------------------------------------------------------------------
# Availability and the folder (D1)
# ---------------------------------------------------------------------------


def workspace_root() -> str:
    from app.core.config import settings

    return (settings.coding_workspace_dir or "").strip()


def available() -> bool:
    """Code analysis is offered only with the coding workspace (UDR-0219 D1, PRP-0236 Q3)."""
    from app.core.config import settings

    root = workspace_root()
    return bool(settings.coding_enabled and root and Path(root).is_dir())


def _posix(rel: str) -> str:
    rel = rel.replace("\\", "/").strip().strip("/")
    return "" if rel in ("", ".") else rel


def resolve_folder(folder: str) -> tuple[str, str]:
    """``(absolute path, workspace-relative POSIX path)`` of a code folder, or ``CodeError``."""
    from app.coding.security import resolve_safe_path

    if not available():
        raise CodeError("code analysis needs CODING_ENABLED and a CODING_WORKSPACE_DIR directory")
    root = workspace_root()
    rel = _posix(str(folder or ""))
    try:
        target = resolve_safe_path(root, rel)
    except ValueError as exc:
        raise CodeError(str(exc)) from None
    if not Path(target).is_dir():
        raise CodeError(f"code folder not found: {rel or '(workspace)'}")
    rel = _posix(os.path.relpath(target, os.path.realpath(root)))
    if rel and Exclusions(os.path.realpath(root)).excluded(rel, is_dir=True):
        raise CodeError(f"code folder is excluded: {rel}")
    return target, rel


def list_folders(dir_: str = "") -> dict[str, Any]:
    """One level of sub-directories of the workspace (never files, never contents)."""
    from app.core.config import settings

    abs_dir, rel = resolve_folder(dir_)
    rules = Exclusions(os.path.realpath(workspace_root()))
    cap = max(1, int(settings.file_explorer_max_tree_entries))
    names: list[str] = []
    try:
        with os.scandir(abs_dir) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                child = f"{rel}/{entry.name}" if rel else entry.name
                if rules.excluded(child, is_dir=True):
                    continue
                names.append(entry.name)
    except OSError as exc:
        raise CodeError(f"cannot list {rel or '(workspace)'}") from exc
    names.sort(key=str.lower)
    parent = None if not rel else ("/".join(rel.split("/")[:-1]))
    return {"dir": rel, "parent": parent, "dirs": names[:cap], "truncated": len(names) > cap}


# ---------------------------------------------------------------------------
# The exclusions -- D5
# ---------------------------------------------------------------------------


def glob_regex(pattern: str) -> str:
    """A glob as a regex over a POSIX path: ``**`` any depth, ``*`` / ``?`` within one segment."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1 : end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


@dataclass
class _Rule:
    regex: re.Pattern[str]
    dir_only: bool


def _gitignore_rules(text: str) -> list[_Rule]:
    """The supported subset: globs, ``dir/``, a leading ``/``, ``**``. ``!`` lines are ignored, so a
    negated path stays excluded (the safe side)."""
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        prefix = "" if anchored else "(?:.*/)?"
        try:
            rules.append(_Rule(re.compile(f"^{prefix}{glob_regex(line)}$"), dir_only))
        except re.error:
            continue
    return rules


def is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, p) for p in SECRET_PATTERNS)


class Exclusions:
    """The shared exclusion rule of one root (D5); ``.gitignore`` files are read lazily, once."""

    def __init__(self, root: str) -> None:
        self.root = root
        self._rules: dict[str, list[_Rule]] = {}

    def _rules_of(self, dir_rel: str) -> list[_Rule]:
        if dir_rel not in self._rules:
            path = Path(self.root, dir_rel, ".gitignore") if dir_rel else Path(self.root, ".gitignore")
            try:
                text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            except OSError:
                text = ""
            self._rules[dir_rel] = _gitignore_rules(text)
        return self._rules[dir_rel]

    def _self_excluded(self, rel: str, is_dir: bool) -> bool:
        """``rel`` itself (its ancestors are checked by the caller or the walk)."""
        name = rel.rsplit("/", 1)[-1]
        if is_dir and name in ALWAYS_EXCLUDED_DIRS:
            return True
        if not is_dir and is_secret(name):
            return True
        parts = rel.split("/")
        for depth in range(len(parts)):
            base = "/".join(parts[:depth])
            tail = "/".join(parts[depth:])
            for rule in self._rules_of(base):
                if rule.dir_only and not is_dir:
                    continue
                if rule.regex.match(tail):
                    return True
        return False

    def excluded(self, rel: str, *, is_dir: bool) -> bool:
        """True when ``rel`` or any directory above it is excluded."""
        rel = _posix(rel)
        if not rel:
            return False
        parts = rel.split("/")
        for i in range(len(parts)):
            sub = "/".join(parts[: i + 1])
            last = i == len(parts) - 1
            if self._self_excluded(sub, is_dir if last else True):
                return True
        return False


# ---------------------------------------------------------------------------
# The walk and the outline (D2)
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    rel: str
    size: int
    mtime_ns: int
    language: str
    readable: bool = True


@dataclass
class Symbol:
    kind: str
    name: str
    signature: str
    doc: str
    line: int
    children: list[Symbol] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "signature": self.signature,
            "doc": self.doc,
            "line": self.line,
            "children": [c.to_dict() for c in self.children],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Symbol:
        return Symbol(
            str(d.get("kind")),
            str(d.get("name")),
            str(d.get("signature") or ""),
            str(d.get("doc") or ""),
            int(d.get("line") or 0),
            [Symbol.from_dict(c) for c in d.get("children") or []],
        )


def _read_limit() -> int:
    from app.core.config import settings

    return max(int(settings.coding_file_read_max_bytes), 1)


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(BINARY_PROBE_BYTES)
    except OSError:
        return True


def folder_rules(folder_rel: str) -> Exclusions:
    """The exclusions of a code folder, rooted at the WORKSPACE: the ``.gitignore`` files above the
    folder (up to the workspace root) apply as well as those inside it."""
    rules = Exclusions(os.path.realpath(workspace_root()))
    return _Prefixed(rules, folder_rel) if folder_rel else rules


def walk(root: str, *, max_files: int, rules: Exclusions | None = None) -> tuple[list[FileEntry], bool]:
    """The folder's files in a stable order, pruned by the exclusions; ``(files, truncated)``."""
    rules = rules or Exclusions(root)
    real_root = os.path.realpath(root)
    read_limit = _read_limit()
    files: list[FileEntry] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        rel_dir = _posix(os.path.relpath(current, root))
        kept = []
        for d in sorted(dirs, key=str.lower):
            child = f"{rel_dir}/{d}" if rel_dir else d
            if Path(current, d).is_symlink() or rules._self_excluded(child, True):
                continue
            kept.append(d)
        dirs[:] = kept
        for name in sorted(names, key=str.lower):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rules._self_excluded(rel, False):
                continue
            path = Path(current, name)
            try:
                if path.is_symlink() and not os.path.realpath(path).startswith(real_root + os.sep):
                    continue
                st = path.stat()
            except OSError:
                continue
            if len(files) >= max_files:
                return files, True
            readable = st.st_size <= read_limit and not _is_binary(path)
            language = LANGUAGES.get(path.suffix.lower(), "")
            files.append(FileEntry(rel, int(st.st_size), int(st.st_mtime_ns), language, readable))
    return files, False


def digest_of(files: list[FileEntry]) -> str:
    h = hashlib.sha256(f"v{EXTRACTOR_VERSION}\n".encode())
    for f in files:
        h.update(f"{f.rel}\t{f.size}\t{f.mtime_ns}\n".encode())
    return h.hexdigest()


def _first_line(text: str | None) -> str:
    line = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    return line if len(line) <= DOC_CHARS else line[: DOC_CHARS - 1].rstrip() + "…"


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    returns = ""
    if node.returns is not None:
        try:
            returns = " -> " + ast.unparse(node.returns)
        except Exception:
            returns = ""
    text = f"({args}){returns}"
    return text if len(text) <= 160 else text[:159] + "…"


def _public(name: str) -> bool:
    return not name.startswith("_") or name == "__init__"


def python_symbols(text: str) -> list[Symbol]:
    """Classes (with their methods), functions and the ``__all__`` constants of a Python module."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    exported: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            exported = {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    out: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and _public(node.name):
            try:
                bases = ", ".join(ast.unparse(b) for b in node.bases)
            except Exception:
                bases = ""
            cls = Symbol(
                "class", node.name, f"({bases})" if bases else "", _first_line(ast.get_docstring(node)), node.lineno
            )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(item.name):
                    kind = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                    cls.children.append(
                        Symbol(kind, item.name, _signature(item), _first_line(ast.get_docstring(item)), item.lineno)
                    )
            out.append(cls)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            out.append(Symbol(kind, node.name, _signature(node), _first_line(ast.get_docstring(node)), node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and exported:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            out.extend(
                Symbol("const", t.id, "", "", node.lineno)
                for t in targets
                if isinstance(t, ast.Name) and t.id in exported and t.id != "__all__"
            )
    return out


def _symbols_of(root: str, entry: FileEntry) -> list[Symbol]:
    if entry.language != "Python" or not entry.readable:
        return []
    try:
        text = Path(root, entry.rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return python_symbols(text)


def _count(symbols: list[Symbol]) -> int:
    return sum(1 + len(s.children) for s in symbols)


# ---- cache ----------------------------------------------------------------------


def _cache_dir() -> Path:
    from app.session.storage import sessions_dir

    return sessions_dir() / ".codemaps"


def _cache_key(root: str, digest: str) -> str:
    return hashlib.sha256(f"{os.path.realpath(root)}\n{digest}".encode()).hexdigest()[:40]


def _cache_read(key: str) -> dict[str, list[Symbol]] | None:
    path = _cache_dir() / f"{key}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {rel: [Symbol.from_dict(s) for s in items] for rel, items in raw["symbols"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _cache_write(key: str, symbols: dict[str, list[Symbol]]) -> None:
    folder = _cache_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{key}.json.tmp"
        payload = {"version": EXTRACTOR_VERSION, "symbols": {k: [s.to_dict() for s in v] for k, v in symbols.items()}}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(folder / f"{key}.json")
        cached = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in cached[CACHE_KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        logger.info("code map cache not written", exc_info=True)


# ---- rendering (D3) --------------------------------------------------------------


def _symbol_line(s: Symbol, indent: str, *, numbered: bool) -> str:
    where = f"L{s.line} " if numbered else ""
    doc = f" -- {s.doc}" if s.doc else ""
    return f"{indent}{where}{s.kind} {s.name}{s.signature}{doc}"


def _dir_counts(files: list[FileEntry], depth: int) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for f in files:
        parts = f.rel.split("/")[:-1]
        for d in range(1, min(len(parts), depth) + 1):
            key = "/".join(parts[:d]) + "/"
            counts[key] = counts.get(key, 0) + 1
    top = sum(1 for f in files if "/" not in f.rel)
    rows = sorted(counts.items())
    return ([("./", top)] if top else []) + rows


def _cut(lines: list[str], limit_chars: int, note: str) -> tuple[str, bool]:
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit_chars:
            rest = len(lines) - i
            out.append(note.format(rest=rest))
            return "\n".join(out), True
        out.append(line)
        used += len(line) + 1
    return "\n".join(out), False


def render_full(
    folder: str, files: list[FileEntry], symbols: dict[str, list[Symbol]], *, max_tokens: int
) -> tuple[str, bool]:
    total = sum(_count(v) for v in symbols.values())
    lines = [
        f"Code map of {folder or '(workspace)'}: {len(files)} files, {total} symbols "
        "(outlines for Python files; other files are listed by name). Lnn = line number.",
        "Folders:",
        *(f"  {d} ({n} files)" for d, n in _dir_counts(files, 2)),
        "Files:",
    ]
    for f in files:
        tag = f.language or "file"
        if not f.readable:
            tag += ", not readable"
        lines.append(f"{f.rel} ({tag})")
        for s in symbols.get(f.rel, []):
            lines.append(_symbol_line(s, "  ", numbered=True))
            lines.extend(_symbol_line(c, "    ", numbered=True) for c in s.children)
    return _cut(lines, max_tokens * CHARS_PER_TOKEN, "[map cut here: {rest} more lines -- use the Code tools]")


def render_short(
    folder: str, files: list[FileEntry], symbols: dict[str, list[Symbol]], *, max_tokens: int
) -> tuple[str, bool]:
    if max_tokens <= 0:
        return "", False
    lines = [
        f"Short code map of {folder or '(workspace)'}: {len(files)} files. The Code analyst can read the code.",
        "Folders:",
        *(f"  {d} ({n} files)" for d, n in _dir_counts(files, 2)),
        "Main modules (largest first, public names):",
    ]
    ranked = sorted((f for f in files if symbols.get(f.rel)), key=lambda f: (-f.size, f.rel))
    for f in ranked:
        names = ", ".join(s.name for s in symbols[f.rel])
        lines.append(f"  {f.rel}: {names}")
    return _cut(lines, max_tokens * CHARS_PER_TOKEN, "  [{rest} more modules]")


def build(
    folder: str, *, max_files: int, map_tokens: int, summary_tokens: int, parent_digest: str | None = None
) -> dict[str, Any]:
    """The run's ``code`` record (CTR-0271): built by code, never raises (D2)."""
    started = time.monotonic()
    record: dict[str, Any] = {
        "folder": _posix(folder),
        "digest": None,
        "files": 0,
        "symbols": 0,
        "languages": {},
        "truncated": False,
        "map": "",
        "summary": "",
        "map_chars": 0,
        "summary_chars": 0,
        "built_at": None,
        "changed_since_parent": None,
        "error": None,
        "read_chars": 0,
    }
    try:
        root, rel = resolve_folder(folder)
        record["folder"] = rel
        files, walk_cut = walk(root, max_files=max(int(max_files), 1), rules=folder_rules(rel))
        digest = digest_of(files)
        key = _cache_key(root, digest)
        symbols = _cache_read(key)
        if symbols is None:
            symbols = {f.rel: s for f in files if (s := _symbols_of(root, f))}
            _cache_write(key, symbols)
        full, full_cut = render_full(rel, files, symbols, max_tokens=int(map_tokens))
        short, short_cut = render_short(rel, files, symbols, max_tokens=int(summary_tokens))
        languages: dict[str, int] = {}
        for f in files:
            if f.language:
                languages[f.language] = languages.get(f.language, 0) + 1
        record.update(
            digest=digest,
            files=len(files),
            symbols=sum(_count(v) for v in symbols.values()),
            languages=dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))),
            truncated=walk_cut or full_cut or short_cut,
            map=full,
            summary=short,
            map_chars=len(full),
            summary_chars=len(short),
            built_at=_now_iso(),
            changed_since_parent=None if parent_digest is None else parent_digest != digest,
        )
        logger.info(
            "AI discussion code map: folder=%s files=%d symbols=%d chars=%d/%d %.2fs",
            rel or "(workspace)",
            record["files"],
            record["symbols"],
            record["map_chars"],
            record["summary_chars"],
            time.monotonic() - started,
        )
    except CodeError as exc:
        record["error"] = str(exc)
    except Exception as exc:  # the map is an aid: never fail the run (D2)
        logger.warning("AI discussion code map failed", exc_info=True)
        record["error"] = f"{type(exc).__name__}: the code map could not be built"
    return record


def _now_iso() -> str:
    from app.discussion.schema import now_iso

    return now_iso()


# ---------------------------------------------------------------------------
# The Code tool (D4)
# ---------------------------------------------------------------------------


class _Folder:
    """The run's folder as the Code tool sees it: the root, the exclusions and path checks."""

    def __init__(self, folder: str) -> None:
        self.root, self.rel = resolve_folder(folder)
        self.rules = folder_rules(self.rel)

    def check(self, path: str, *, is_dir: bool) -> str | None:
        """An error text for a path the tool must refuse, else None."""
        from app.coding.security import resolve_safe_path

        rel = _posix(path)
        try:
            target = resolve_safe_path(self.root, rel)
        except ValueError:
            return f"Access denied: path outside the code folder: {path}"
        rel = _posix(os.path.relpath(target, os.path.realpath(self.root)))
        if self.rules.excluded(rel, is_dir=is_dir):
            return f"Excluded from code analysis: {path}"
        return None

    def files(self, base: str, pattern: str) -> list[Path]:
        """The non-excluded files under ``base`` whose path relative to ``base`` matches ``pattern``."""
        regex = re.compile(f"^{glob_regex(pattern or '**/*')}$")
        start = Path(self.root, _posix(base)) if _posix(base) else Path(self.root)
        entries, _ = walk(str(start), max_files=1_000_000, rules=_Prefixed(self.rules, _posix(base)))
        return [start / e.rel for e in entries if regex.match(e.rel)]


class _Prefixed(Exclusions):
    """The folder's exclusions applied to a walk that starts in a sub-directory."""

    def __init__(self, rules: Exclusions, prefix: str) -> None:
        super().__init__(rules.root)
        self._inner = rules
        self._prefix = prefix

    def _self_excluded(self, rel: str, is_dir: bool) -> bool:
        return self._inner._self_excluded(f"{self._prefix}/{rel}" if self._prefix else rel, is_dir)


def outline_text(folder: _Folder, path: str) -> str:
    refused = folder.check(path, is_dir=False)
    if refused:
        return refused
    rel = _posix(path)
    file_path = Path(folder.root, rel)
    if not file_path.is_file():
        return f"Error: File not found: {path}"
    language = LANGUAGES.get(file_path.suffix.lower(), "")
    if language != "Python":
        return f"{rel}: no outline for {language or 'this'} files (outlines cover Python); use file_read."
    entry = FileEntry(rel, 0, 0, language, file_path.stat().st_size <= _read_limit() and not _is_binary(file_path))
    symbols = _symbols_of(folder.root, entry)
    if not symbols:
        return f"{rel}: no public classes or functions."
    lines = [f"Outline of {rel} (Lnn = line number):"]
    for s in symbols:
        lines.append(_symbol_line(s, "  ", numbered=True))
        lines.extend(_symbol_line(c, "    ", numbered=True) for c in s.children)
    return "\n".join(lines)


def tool_functions(folder: str) -> list[Any]:
    """The four read-only Code functions, rooted at the run's folder (D4). Never write or shell."""
    import asyncio

    from app.coding import tools as coding

    scope = _Folder(folder)

    async def file_outline(
        path: Annotated[str, Field(description="File path relative to the code folder")],
    ) -> str:
        """The classes and functions of one file with their line numbers (Python files)."""
        return await asyncio.to_thread(outline_text, scope, path)

    async def file_glob(
        pattern: Annotated[str, Field(description="Glob pattern, e.g. '**/*.py' or 'app/**/router.py'")],
        path: Annotated[
            str, Field(description="Base directory relative to the code folder (default: the folder)")
        ] = "",
    ) -> str:
        """Find files by a glob pattern in the code folder."""
        refused = scope.check(path, is_dir=True) if path else None
        if refused:
            return refused
        return await asyncio.to_thread(
            coding._file_glob_sync, pattern, path, root=scope.root, lister=lambda _b, p: scope.files(path, p)
        )

    async def file_grep(
        pattern: Annotated[str, Field(description="Regex to search for in file contents")],
        path: Annotated[str, Field(description="Directory relative to the code folder (default: the folder)")] = "",
        include: Annotated[str, Field(description="Glob filter, e.g. '**/*.py'")] = "",
    ) -> str:
        """Search file contents by a regex in the code folder; answers path:line:text."""
        refused = scope.check(path, is_dir=True) if path else None
        if refused:
            return refused
        return await asyncio.to_thread(
            coding._file_grep_sync,
            pattern,
            path,
            include,
            root=scope.root,
            lister=lambda _b, p: [f for f in scope.files(path, p) if not _binary_or_large(f)],
        )

    async def file_read(
        path: Annotated[str, Field(description="File path relative to the code folder")],
        offset: Annotated[int, Field(description="First line to read (0-based, default 0)")] = 0,
        limit: Annotated[int, Field(description="Lines to read (default 400)")] = 400,
    ) -> str:
        """Read a file of the code folder with line numbers."""
        refused = scope.check(path, is_dir=False)
        if refused:
            return refused
        target = Path(scope.root, _posix(path))
        if target.is_file() and _binary_or_large(target):
            return f"Not readable (binary or larger than the read limit): {path}"
        return await asyncio.to_thread(coding._file_read_sync, path, offset, limit, 0, root=scope.root)

    return [file_outline, file_glob, file_grep, file_read]


def _binary_or_large(path: Path) -> bool:
    try:
        return path.stat().st_size > _read_limit() or _is_binary(path)
    except OSError:
        return True


#: The Code function names (an invariant pins that nothing else is mounted).
TOOL_FUNCTION_NAMES: tuple[str, ...] = ("file_outline", "file_glob", "file_grep", "file_read")


def query_of(kwargs: dict[str, Any], fn_name: str) -> str:
    """What a Code call is recorded with (UDR-0217 D3): the function and its path / pattern."""
    target = kwargs.get("pattern") or kwargs.get("path") or ""
    return f"{fn_name} {target}".strip()


# ---------------------------------------------------------------------------
# The citations -- D6
# ---------------------------------------------------------------------------

_CITATION = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]{1,8}):(\d{1,6})(?:-(\d{1,6}))?")


def check_citations(text: str, folder: str) -> list[dict[str, Any]]:
    """``[{path, line, ok}]`` for the ``path:line`` references in a reply; the text is untouched."""
    try:
        scope = _Folder(folder)
    except CodeError:
        return []
    seen: set[tuple[str, int]] = set()
    lines_of: dict[str, int | None] = {}
    out: list[dict[str, Any]] = []
    for match in _CITATION.finditer(text or ""):
        raw, line = match.group(1), int(match.group(2))
        rel = _posix(raw)
        if scope.rel and rel.startswith(scope.rel + "/"):
            rel = rel[len(scope.rel) + 1 :]
        if (rel, line) in seen:
            continue
        seen.add((rel, line))
        if rel not in lines_of:
            lines_of[rel] = _line_count(scope, rel)
        total = lines_of[rel]
        out.append({"path": raw, "line": line, "ok": total is not None and 1 <= line <= total})
        if len(out) >= MAX_CITATIONS:
            break
    return out


def sample_citation(folder: str) -> str | None:
    """``path:line`` of the folder's first Python symbol -- what a scripted Code seat cites (e2e)."""
    try:
        scope = _Folder(folder)
    except CodeError:
        return None
    files, _ = walk(scope.root, max_files=500, rules=scope.rules)
    for entry in files:
        symbols = _symbols_of(scope.root, entry)
        if symbols:
            return f"{entry.rel}:{symbols[0].line}"
    return None


def _line_count(scope: _Folder, rel: str) -> int | None:
    if scope.check(rel, is_dir=False):
        return None
    path = Path(scope.root, rel)
    if not path.is_file() or _binary_or_large(path):
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


__all__ = [
    "ALWAYS_EXCLUDED_DIRS",
    "CODE_LIMIT_REACHED",
    "SECRET_PATTERNS",
    "TOOL_FUNCTION_NAMES",
    "CodeError",
    "Exclusions",
    "available",
    "build",
    "check_citations",
    "list_folders",
    "python_symbols",
    "query_of",
    "resolve_folder",
    "tool_functions",
    "walk",
]
