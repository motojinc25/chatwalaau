"""Ontology store and catalog (CTR-0170, PRP-0105, UDR-0084 D3/D7/D10).

File layout under ``ONTOLOGY_DIR`` (created on demand):

    catalog.json              -- the catalog SSOT: [{id, name, description, file,
                                 created_at, updated_at}]
    <id>.ttl                  -- ONE self-contained file per ontology (SSOT): Turtle,
    <id>.trig                    or TriG once it has a named graph (UDR-0182 D3)
    <id>.ttl.bak-<timestamp>  -- automatic backup on every save / pre-delete
    <id>.history.jsonl        -- one line per write (CTR-0170 v5, UDR-0184 D1);
                                 backups are bounded by retention (D3)

The catalog reader is tolerant and self-healing (per-entry normalization +
write-back; an unparseable file is backed up to ``catalog.corrupt-<ts>.json``
and the catalog restarts empty -- the CTR-0015 folder-index precedent). Every
save writes a timestamped backup then a temp file + atomic ``os.replace``
(the CTR-0166 / UDR-0080 D3 convention); delete is backup-then-remove.

Query execution is READ-ONLY by construction: the ONE executor runs
``pyoxigraph.Store.query()`` (SELECT / CONSTRUCT / ASK / DESCRIBE), which is
structurally incapable of mutating; SPARQL UPDATE strings fail its parser.
Results are restored to the file's lexical forms and blank-node labels through
``app.ontology.lexical`` (CTR-0170 v2, UDR-0181), with ``notices`` for what the
Store's value encoding cannot give back. For a dataset the query Store's default
graph is the set merge of the graphs in the requested scope, every named graph
stays available to GRAPH patterns, and CONSTRUCT answers are TriG with each triple
under the graphs that hold it (CTR-0170 v3, UDR-0182 D4 / D5).
Filenames are catalog-derived single segments, so no path outside ONTOLOGY_DIR
is ever resolved (the CTR-0022 confinement precedent).

A search may ask for an entailment mode (CTR-0170 v7, UDR-0191): once per cached
Store, ``app.ontology.entailment`` computes the inferred statements -- of the merge,
and of each named graph with the dataset's schema -- into an ENTAILED copy of the
Store that answers that mode's queries; the stated Store is never changed and
nothing is written to a file.

Loaded query Stores are cached per (ontology, revision, scope) within the
``ontology_query_cache_mb`` App Settings budget; the revision is the SHA-256 of
the bytes on disk, so a cached entry can never answer for another file version
(CTR-0170 v4, UDR-0183 D1-D3). The callers run this module's work in worker
threads (D4): catalog read-modify-writes hold ``_CATALOG_LOCK`` and every
"check revision -> read -> apply -> write" holds ``write_lock(id)``.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import TYPE_CHECKING, Any
import uuid

from app.core.config import settings
from app.ontology.lexical import (
    LexicalIndex,
    build_index,
    computed_variables,
    is_restorable,
    restore_term,
    restore_triples,
)
from app.ontology.lexical import notices as lexical_notices

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

_CATALOG_NAME = "catalog.json"
_ID_RE = re.compile(r"^ont_[0-9a-f]{12}$")

# The stored file is Turtle, or TriG once the ontology has a named graph (UDR-0182 D3).
TURTLE_SUFFIX = ".ttl"
TRIG_SUFFIX = ".trig"
FILE_SUFFIXES = (TURTLE_SUFFIX, TRIG_SUFFIX)

# Result-shape caps for the read-only executor (payload bound, not a security
# boundary -- CTR-0083 gates the callers).
# PRP-0211 (UDR-0193 D6): the row bound of the screen lanes is the App Settings key
# ``ontology_query_max_rows`` (it replaced the constant SELECT_MAX_ROWS = 1000).


def max_rows() -> int:
    """The ``ontology_query_max_rows`` App Settings bound (SELECT rows, triples table)."""
    return max(1, int(settings.ontology_query_max_rows))


# Locks (UDR-0183 D4). Order: ``write_lock(id)`` before ``_CATALOG_LOCK``, never the
# reverse. On Windows ``os.replace`` fails while another thread has the target open,
# so reads of the catalog and of an ontology file take the same locks for the moment
# they read bytes (never while parsing).
_CATALOG_LOCK = threading.RLock()
_WRITE_LOCKS: dict[str, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


@contextmanager
def write_lock(ontology_id: str) -> Iterator[None]:
    """Serialize "check revision -> read -> apply -> write" for one ontology (UDR-0183 D4)."""
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.setdefault(ontology_id, threading.RLock())
    with lock:
        yield


# PRP-0211 (UDR-0193 D7): called with the ontology id after every write -- create,
# save (statement operations and version restores included), trash restore, delete --
# so the entailment builds can follow. Listeners must not raise; errors are logged.
_write_listeners: list[Callable[[str, str], None]] = []


def add_write_listener(listener: Callable[[str, str], None]) -> None:
    """Register ``listener(ontology_id, kind)`` (idempotent).

    ``kind`` is "write", "delete", or "shacl" (only the catalog's SHACL block changed,
    PRP-0212 / UDR-0194 D5).
    """
    if listener not in _write_listeners:
        _write_listeners.append(listener)


def remove_write_listener(listener: Callable[[str, str], None]) -> None:
    if listener in _write_listeners:
        _write_listeners.remove(listener)


def notify_written(ontology_id: str, kind: str = "write") -> None:
    """Tell the write listeners that ``ontology_id`` was written (or deleted)."""
    for listener in _write_listeners:
        try:
            listener(ontology_id, kind)
        except Exception:
            logger.warning("ontology write listener failed for %s", ontology_id, exc_info=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def ontology_dir() -> Path:
    """The configured ontology folder (CTR-0006 ONTOLOGY_DIR), created on demand."""
    path = Path(settings.ontology_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _catalog_path() -> Path:
    return ontology_dir() / _CATALOG_NAME


def _atomic_write_text(path: Path, content: str) -> None:
    """Temp file + atomic ``os.replace`` (never ``open('w')`` in place)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    # Bytes, not write_text: no newline translation on Windows, so the stored
    # file is exactly what was serialized (the CTR-0171 revision hashes these bytes).
    tmp.write_bytes(content.encode("utf-8"))
    tmp.replace(path)


def _backup(path: Path) -> str | None:
    """Copy ``path`` to ``<name>.bak-<timestamp>``; None when it does not exist.

    A backup failure raises BEFORE the target is touched, so the existing file
    is never corrupted (UDR-0080 D3).
    """
    if not path.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    backup = path.parent / f"{path.name}.bak-{stamp}"
    backup.write_bytes(path.read_bytes())
    return backup.name


# ---- Catalog (tolerant, self-healing; the CTR-0015 precedent) ---------------


def _normalize_entry(raw: Any) -> dict[str, str] | None:
    """Normalize one catalog entry; None drops an unusable record."""
    if not isinstance(raw, dict):
        return None
    entry_id = str(raw.get("id") or "").strip()
    if not _ID_RE.match(entry_id):
        return None
    file_name = str(raw.get("file") or "").strip() or f"{entry_id}.ttl"
    # Single-segment confinement: a catalog-derived filename must never traverse.
    if Path(file_name).name != file_name or not file_name.endswith(FILE_SUFFIXES):
        file_name = f"{entry_id}.ttl"
    normalized: dict[str, Any] = {
        "id": entry_id,
        "name": str(raw.get("name") or "").strip() or entry_id,
        "description": str(raw.get("description") or "").strip(),
        "file": file_name,
        "created_at": str(raw.get("created_at") or "").strip() or _now(),
        "updated_at": str(raw.get("updated_at") or "").strip() or _now(),
    }
    shacl = normalize_shacl(raw.get("shacl"), entry_id)
    if shacl is not None:
        normalized["shacl"] = shacl
    return normalized  # type: ignore[return-value]


# ---- SHACL validation settings of a catalog entry (PRP-0212, UDR-0194 D1) ------------

SHACL_INFERENCE = ("none", "rdfs", "owl-rl")
MAX_SHAPES_ONTOLOGIES = 10
MAX_SHAPES_GRAPHS = 50
_GRAPH_IRI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s<>\"{}|\\^`]*$")


def normalize_shacl(raw: Any, own_id: str | None = None) -> dict[str, Any] | None:
    """The catalog ``shacl`` block, tolerant (unusable values are dropped; None = no block).

    ``shapes_graphs``: graph IRIs of the same ontology; ``shapes_ontologies``: other
    catalog ids (kept when the ontology no longer exists -- reported as missing);
    ``inference``: one of ``SHACL_INFERENCE`` (default ``none``, operator R11);
    ``strict``: block saves with violations (default false, operator Q1).
    """
    if not isinstance(raw, dict):
        return None
    graphs: list[str] = []
    for item in raw.get("shapes_graphs") or []:
        text = str(item or "").strip()
        if text.startswith("<") and text.endswith(">"):
            text = text[1:-1]
        if _GRAPH_IRI_RE.match(text) and text not in graphs:
            graphs.append(text)
    ontologies: list[str] = []
    for item in raw.get("shapes_ontologies") or []:
        text = str(item or "").strip()
        if _ID_RE.match(text) and text != own_id and text not in ontologies:
            ontologies.append(text)
    inference = str(raw.get("inference") or "none").strip().lower()
    return {
        "shapes_graphs": graphs[:MAX_SHAPES_GRAPHS],
        "shapes_ontologies": ontologies[:MAX_SHAPES_ONTOLOGIES],
        "inference": inference if inference in SHACL_INFERENCE else "none",
        "strict": raw.get("strict") is True,
    }


def shacl_config(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """The entry's ``shacl`` block when it declares at least one shapes source, else None."""
    block = (entry or {}).get("shacl")
    if not isinstance(block, dict):
        return None
    if not block.get("shapes_graphs") and not block.get("shapes_ontologies"):
        return None
    return block


def set_shacl(ontology_id: str, block: dict[str, Any] | None) -> dict[str, Any] | None:
    """Replace (or with None remove) the entry's ``shacl`` block; returns the entry."""
    with _CATALOG_LOCK:
        entries = read_catalog()
        updated: dict[str, Any] | None = None
        for entry in entries:
            if entry["id"] == ontology_id:
                normalized = normalize_shacl(block, ontology_id) if block is not None else None
                if normalized is None:
                    entry.pop("shacl", None)  # type: ignore[misc]
                else:
                    entry["shacl"] = normalized  # type: ignore[assignment]
                entry["updated_at"] = _now()
                updated = entry
                break
        if updated is None:
            return None
        write_catalog(entries)
    logger.info("ontology validation settings changed: %s", ontology_id)
    notify_written(ontology_id, "shacl")
    return updated


def read_catalog() -> list[dict[str, str]]:
    """Read the catalog, normalizing per entry and self-healing on corruption."""
    with _CATALOG_LOCK:
        return _read_catalog()


def _read_catalog() -> list[dict[str, str]]:
    path = _catalog_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unparseable catalog: back it up and restart empty (never raise).
        try:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            path.replace(path.parent / f"catalog.corrupt-{stamp}.json")
            logger.warning("ontology catalog was unreadable; backed up and restarted empty")
        except OSError:
            logger.warning("ontology catalog unreadable and could not be quarantined", exc_info=True)
        return []
    items = raw.get("ontologies") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    normalized = [entry for entry in (_normalize_entry(item) for item in items) if entry is not None]
    if normalized != items:
        try:
            write_catalog(normalized)  # self-heal drifted/partial records
        except OSError:
            logger.warning("ontology catalog self-heal write failed", exc_info=True)
    return normalized


def write_catalog(entries: list[dict[str, str]]) -> None:
    with _CATALOG_LOCK:
        _atomic_write_text(_catalog_path(), json.dumps({"ontologies": entries}, ensure_ascii=False, indent=2))


def get_entry(ontology_id: str) -> dict[str, str] | None:
    for entry in read_catalog():
        if entry["id"] == ontology_id:
            return entry
    return None


def reorder_catalog(ontology_ids: list[str]) -> list[dict[str, str]]:
    """Reorder the catalog from an explicit id sequence (UDR-0195 D7 / D8).

    The list order IS the presentation order. Idempotent bulk set: unknown ids are
    ignored; catalog ids absent from ``ontology_ids`` are appended in their prior
    relative order (the folder-order rule, UDR-0046 D6).
    """
    with _CATALOG_LOCK:
        entries = read_catalog()
        by_id = {entry["id"]: entry for entry in entries}
        ordered: list[dict[str, str]] = []
        consumed: set[str] = set()
        for ontology_id in ontology_ids:
            entry = by_id.get(ontology_id)
            if entry is not None and ontology_id not in consumed:
                ordered.append(entry)
                consumed.add(ontology_id)
        ordered.extend(entry for entry in entries if entry["id"] not in consumed)
        if ordered != entries:
            write_catalog(ordered)
        return ordered


# ---- Saved table mappings (CTR-0170 v12, PRP-0217, UDR-0199 D4) ----------------------
#
# ``<id>.mappings.json`` beside the ontology file: the named CSV mappings of one
# ontology, outside its content and its history. A delete keeps it (the trash restores
# the ontology under the same id); the trash expiry removes it with the backups.

MAPPINGS_FORMAT = "chatwalaau.table-mappings/1"


def mappings_path(ontology_id: str) -> Path:
    if not _ID_RE.match(ontology_id):
        raise ValueError(f"not an ontology id: {ontology_id!r}")
    return ontology_dir() / f"{ontology_id}.mappings.json"


def read_mappings(ontology_id: str) -> list[dict[str, Any]]:
    """The saved mappings, name order; an unreadable file reads as none (logged)."""
    path = mappings_path(ontology_id)
    with write_lock(ontology_id):
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("saved table mappings of %s are unreadable; ignored", ontology_id)
            return []
    items = raw.get("mappings") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    out = [item for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)]
    return sorted(out, key=lambda item: item["name"].casefold())


def _write_mappings(ontology_id: str, items: list[dict[str, Any]]) -> None:
    path = mappings_path(ontology_id)
    if not items:
        if path.is_file():
            path.unlink()
        return
    body = {"format": MAPPINGS_FORMAT, "mappings": sorted(items, key=lambda item: item["name"].casefold())}
    _atomic_write_text(path, json.dumps(body, ensure_ascii=False, indent=2))


def save_mapping(ontology_id: str, mapping: dict[str, Any]) -> dict[str, Any]:
    """Save or replace the mapping named ``mapping["name"]`` (already validated)."""
    saved = {**mapping, "updated_at": _now()}
    with write_lock(ontology_id):
        items = [item for item in read_mappings(ontology_id) if item["name"] != mapping["name"]]
        items.append(saved)
        _write_mappings(ontology_id, items)
    logger.info("table mapping saved: %s / %s", ontology_id, mapping["name"])
    return saved


def delete_mapping(ontology_id: str, name: str) -> bool:
    with write_lock(ontology_id):
        items = read_mappings(ontology_id)
        kept = [item for item in items if item["name"] != name]
        if len(kept) == len(items):
            return False
        _write_mappings(ontology_id, kept)
    logger.info("table mapping deleted: %s / %s", ontology_id, name)
    return True


def _touch_entry(ontology_id: str) -> None:
    with _CATALOG_LOCK:
        entries = read_catalog()
        for entry in entries:
            if entry["id"] == ontology_id:
                entry["updated_at"] = _now()
        write_catalog(entries)


# ---- Ontology file lifecycle -------------------------------------------------


def _file_path(entry: dict[str, str]) -> Path:
    return ontology_dir() / entry["file"]


def create_ontology(
    name: str,
    description: str,
    *,
    initial_turtle: str | Callable[[str], str] = "",
    dataset: bool | Callable[[], bool] = False,
    history_kind: str = "create",
) -> dict[str, str]:
    """Create a new catalog entry + its file.

    ``initial_turtle`` is the file content, or a factory called with the new id
    (a new ontology's header binds ``:`` to its own minting namespace). ``dataset``
    (or a callable asked after the factory ran) stores the content as TriG.
    ``history_kind`` ("create" / "import") is the first history log line (UDR-0184 D1).
    """
    entry_id = f"ont_{uuid.uuid4().hex[:12]}"
    if callable(initial_turtle):
        initial_turtle = initial_turtle(entry_id)
    is_dataset = dataset() if callable(dataset) else dataset
    entry = {
        "id": entry_id,
        "name": (name or "").strip() or entry_id,
        "description": (description or "").strip(),
        "file": f"{entry_id}{TRIG_SUFFIX if is_dataset else TURTLE_SUFFIX}",
        "created_at": _now(),
        "updated_at": _now(),
    }
    _atomic_write_text(_file_path(entry), initial_turtle)
    with _CATALOG_LOCK:
        entries = read_catalog()
        entries.insert(0, entry)  # new entries go to the top (UDR-0195 D9)
        write_catalog(entries)
    from app.ontology import history

    history.append_entry(
        entry_id,
        {
            "kind": history_kind,
            "backup": None,
            "revision_before": None,
            "revision_after": revision_of(initial_turtle.encode("utf-8")),
        },
    )
    logger.info("ontology created: %s (%s)", entry_id, entry["name"])
    notify_written(entry_id)
    return entry


def rename_ontology(
    ontology_id: str, *, name: str | None = None, description: str | None = None
) -> dict[str, str] | None:
    """Rename a catalog entry (name and/or description); id + Turtle unchanged.

    PRP-0116 / CTR-0171: additive catalog CRUD gap fill. The projection file is
    untouched -- only the catalog entry's name/description and updated_at change.
    Returns the updated entry, or None when the id is unknown.
    """
    with _CATALOG_LOCK:
        entries = read_catalog()
        updated: dict[str, str] | None = None
        for entry in entries:
            if entry["id"] == ontology_id:
                if name is not None:
                    entry["name"] = name.strip() or entry["id"]
                if description is not None:
                    entry["description"] = description.strip()
                entry["updated_at"] = _now()
                updated = entry
                break
        if updated is None:
            return None
        write_catalog(entries)
    logger.info("ontology renamed: %s (%s)", ontology_id, updated["name"])
    return updated


def delete_ontology(ontology_id: str) -> bool:
    """Backup-then-remove the file and drop the catalog entry (UDR-0084 D10).

    The delete is logged with the name and description, so the trash can show and
    restore it under the same id (UDR-0184 D1 / D6); expired trash entries go.
    """
    from app.ontology import history

    with write_lock(ontology_id):
        entry = get_entry(ontology_id)
        if entry is None:
            return False
        path = _file_path(entry)
        before = path.read_bytes() if path.is_file() else None
        backup_name = _backup(path)
        if path.is_file():
            path.unlink()
        with _CATALOG_LOCK:
            write_catalog([e for e in read_catalog() if e["id"] != ontology_id])
        history.append_entry(
            ontology_id,
            {
                "kind": "delete",
                "backup": backup_name,
                "revision_before": revision_of(before) if before is not None else None,
                "revision_after": None,
                "name": entry["name"],
                "description": entry["description"],
            },
        )
    invalidate_query_cache(ontology_id)
    # PRP-0211 (UDR-0193 D8): the stored entailment results go with the ontology.
    from app.ontology import entailment_results

    entailment_results.remove(ontology_id)
    history.expire_trash()
    logger.info("ontology deleted: %s (backup kept)", ontology_id)
    notify_written(ontology_id, "delete")
    return True


def read_ontology_bytes(ontology_id: str) -> bytes | None:
    with write_lock(ontology_id):  # held only while reading (see the lock notes above)
        entry = get_entry(ontology_id)
        if entry is None:
            return None
        path = _file_path(entry)
        return path.read_bytes() if path.is_file() else b""


def read_entry_and_bytes(ontology_id: str) -> tuple[dict[str, str], bytes] | None:
    """The catalog entry and the stored bytes as one consistent pair (None when unknown)."""
    with write_lock(ontology_id):
        entry = get_entry(ontology_id)
        if entry is None:
            return None
        path = _file_path(entry)
        return entry, (path.read_bytes() if path.is_file() else b"")


def revision_of(data: bytes) -> str:
    """The optimistic-concurrency revision of a stored file (CTR-0171 v3, UDR-0180 D8)."""
    return hashlib.sha256(data).hexdigest()


def is_dataset(entry: dict[str, str]) -> bool:
    """True when the stored file is TriG (the ontology has a named graph; UDR-0182 D3)."""
    return entry["file"].endswith(TRIG_SUFFIX)


def save_ontology_text(
    ontology_id: str,
    turtle: str,
    *,
    dataset: bool | None = None,
    history: dict[str, Any] | None = None,
) -> str | None:
    """Guarded save: size cap -> backup -> temp + atomic replace. Returns backup name.

    ``dataset`` switches the stored file between ``<id>.ttl`` (Turtle) and
    ``<id>.trig`` (TriG) when it differs from the current one (UDR-0182 D3): the new
    file is written first, then the catalog points at it, and the previous file is
    kept only as its backup. ``None`` keeps the current file.

    Every save is logged (``history`` adds ``kind`` -- default "save" -- and counts /
    ``restored_from``), then the backups are pruned by the retention rules, all under
    the write lock (UDR-0184 D1 / D3).
    """
    from app.ontology import history as history_log

    with write_lock(ontology_id):
        entry = get_entry(ontology_id)
        current = _file_path(entry) if entry else None
        before = current.read_bytes() if current is not None and current.is_file() else None
        backup_name = _save_locked(ontology_id, turtle, dataset)
        history_log.append_entry(
            ontology_id,
            {
                "kind": "save",
                **(history or {}),
                "backup": backup_name,
                "revision_before": revision_of(before) if before is not None else None,
                "revision_after": revision_of(turtle.encode("utf-8")),
            },
        )
        history_log.prune(ontology_id)
    invalidate_query_cache(ontology_id)
    notify_written(ontology_id)
    return backup_name


def restore_deleted_ontology(ontology_id: str, text: str, *, dataset: bool, restored_from: str) -> dict[str, str]:
    """Recreate a deleted ontology under the SAME id from one of its backups (UDR-0184 D6).

    Raises ``FileExistsError`` when the id is in the catalog again (409).
    """
    from app.ontology import history

    deleted = None
    for item in reversed(history.read_entries(ontology_id)):
        if item.get("kind") == "delete":
            deleted = item
            break
    with write_lock(ontology_id):
        if get_entry(ontology_id) is not None:
            raise FileExistsError(ontology_id)
        encoded = text.encode("utf-8")
        if len(encoded) > settings.ontology_max_file_bytes:
            raise ValueError(
                f"Ontology is {len(encoded)} bytes but the limit is {settings.ontology_max_file_bytes} bytes"
            )
        entry = {
            "id": ontology_id,
            "name": str((deleted or {}).get("name") or ontology_id),
            "description": str((deleted or {}).get("description") or ""),
            "file": f"{ontology_id}{TRIG_SUFFIX if dataset else TURTLE_SUFFIX}",
            "created_at": _now(),
            "updated_at": _now(),
        }
        target = _file_path(entry)
        _backup(target)  # a stray file of the target name is kept, never overwritten silently
        _atomic_write_text(target, text)
        with _CATALOG_LOCK:
            entries = read_catalog()
            entries.insert(0, entry)  # a restored entry goes to the top (UDR-0195 D9)
            write_catalog(entries)
        history.append_entry(
            ontology_id,
            {
                "kind": "restore",
                "backup": None,
                "revision_before": None,
                "revision_after": revision_of(encoded),
                "restored_from": restored_from,
            },
        )
    invalidate_query_cache(ontology_id)
    logger.info("deleted ontology restored: %s from %s", ontology_id, restored_from)
    notify_written(ontology_id)
    return entry


def _save_locked(ontology_id: str, turtle: str, dataset: bool | None) -> str | None:
    entry = get_entry(ontology_id)
    if entry is None:
        raise KeyError(ontology_id)
    encoded = turtle.encode("utf-8")
    if len(encoded) > settings.ontology_max_file_bytes:
        raise ValueError(f"Ontology is {len(encoded)} bytes but the limit is {settings.ontology_max_file_bytes} bytes")
    path = _file_path(entry)
    backup_name = _backup(path)
    if dataset is None or dataset == is_dataset(entry):
        _atomic_write_text(path, turtle)
        _touch_entry(ontology_id)
        return backup_name
    target = ontology_dir() / f"{ontology_id}{TRIG_SUFFIX if dataset else TURTLE_SUFFIX}"
    _backup(target)  # a stray file of the target name is kept, never overwritten silently
    _atomic_write_text(target, turtle)
    with _CATALOG_LOCK:
        entries = read_catalog()
        for item in entries:
            if item["id"] == ontology_id:
                item["file"] = target.name
                item["updated_at"] = _now()
        write_catalog(entries)
    if path != target and path.is_file():
        path.unlink()  # its content is in ``backup_name``
    logger.info("ontology %s stored as %s", ontology_id, target.name)
    return backup_name


# ---- pyoxigraph load + read-only query executor (UDR-0084 D3/D7) -------------


@dataclass
class QueryGraph:
    """An ontology loaded for the read-only query lanes (CTR-0170 v3, UDR-0181, UDR-0182).

    ``store`` is the in-memory pyoxigraph Store the SPARQL runs on: its DEFAULT graph
    is the RDF merge (a set) of the graphs in scope, and every named graph of the file
    is also kept under its name for ``GRAPH`` patterns (UDR-0182 D4). ``index`` maps
    the Store's normalized terms back to the file's own (built from the same parse);
    ``document`` holds the file's prefixes / base / VERSION; ``graphs`` the named
    graphs in file order. ``provenance`` (datasets only) maps each in-scope triple to
    the graphs that hold it, so CONSTRUCT answers keep where a triple came from (D5).
    """

    store: Any
    index: LexicalIndex
    document: dict[str, Any]
    graphs: list[Any] = field(default_factory=list)
    scope: Any = "all"
    provenance: dict[Any, list[Any]] | None = None
    # The NL schema summary, computed on first use and kept with a cached entry
    # (UDR-0183 D2). It is derived from the fields above, which never change.
    nl_summary: str | None = None
    # PRP-0207 / UDR-0189 D5: (id, revision) of the imported ontologies merged in
    # (None = none), and the import notices this answer carries.
    imports: tuple[tuple[str, str], ...] | None = None
    import_notices: list[dict[str, Any]] = field(default_factory=list)
    # PRP-0209 / UDR-0191 D3 / D7: per entailment mode, the ENTAILED copy of ``store``
    # (every stated quad, plus the inferred statements in the default graph and in each
    # of the file's named graphs), the number of inferred statements, and the named
    # graphs each inferred statement was inferred in. Filled at most once per mode under
    # ``entailment_lock`` (UDR-0183 D2 amended); shared with the shallow copies
    # ``_with_notices`` makes. ``store`` itself never changes.
    entailed: dict[str, Any] = field(default_factory=dict)
    entailments: dict[str, int] = field(default_factory=dict)
    inferred_graphs: dict[str, dict[Any, list[Any]]] = field(default_factory=dict)
    entailment_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)
    # PRP-0210 / UDR-0192 D5 / D8: per mode, the notices every answer of that mode carries
    # (inconsistencies, owl:sameAs pairs), and a remembered output-bound refusal
    # (message, detail) so the engine does not run again for the same files.
    entailment_notices: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    entailment_refusals: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    # PRP-0211 (UDR-0193 D8): the query-cache key this graph was loaded for (None when it
    # was built outside ``load_store``); the stored entailment results are kept per key.
    key: tuple[Any, ...] | None = None


def parse_scope(value: Any) -> Any:
    """A query scope: ``"all"`` (default), ``"default"``, or one graph as Term JSON / text.

    Text accepts ``<iri>``, a bare absolute IRI or ``_:label`` (the agent tool's form).
    Raises ``ValueError`` for anything else.
    """
    import pyoxigraph as ox

    from app.ontology.vocabulary import ProjectionError, graph_from_json

    if isinstance(value, ox.NamedNode | ox.BlankNode):
        return value  # already parsed
    if value is None or value == "" or value == "all":
        return "all"
    if value == "default":
        return "default"
    if isinstance(value, str):
        text = value.strip()
        try:
            if text.startswith("_:"):
                return ox.BlankNode(text[2:])
            return ox.NamedNode(text[1:-1] if text.startswith("<") and text.endswith(">") else text)
        except ValueError as exc:
            raise ValueError(f"Invalid graph {value!r}: {exc}") from exc
    try:
        graph = graph_from_json(value, "scope")
    except ProjectionError as exc:
        raise ValueError(f"Invalid scope: {exc}") from exc
    if isinstance(graph, ox.DefaultGraph):
        return "default"
    return graph


def scope_label(scope: Any) -> str:
    """How a scope is named in answers and notices."""
    if scope == "all":
        return "all graphs"
    if scope == "default":
        return "the default graph"
    return str(scope)


def _in_scope(scope: Any, quad: Any) -> bool:
    import pyoxigraph as ox

    if scope == "all":
        return True
    if scope == "default":
        return isinstance(quad.graph_name, ox.DefaultGraph)
    return quad.graph_name == scope


def _merge_into_default(store: Any, graphs: list[Any]) -> None:
    """Copy the named ``graphs`` into the private Store's default graph (the scope's merge).

    The copy is written as N-Triples and parsed back -- both inside the engine, with
    blank-node labels kept (``parse``, never ``Store.load``) -- which is several times
    faster than building a default-graph copy of every quad in Python, and needs no
    SPARQL UPDATE (the query lane stays read-only by construction, UDR-0084 D7). The
    Store is a set, so the default graph becomes the RDF merge (UDR-0182 D4).
    """
    import pyoxigraph as ox

    for graph in graphs:
        triples = (q.triple for q in store.quads_for_pattern(None, None, None, graph))
        store.extend(ox.parse(ox.serialize(triples, format=ox.RdfFormat.N_TRIPLES), format=ox.RdfFormat.N_TRIPLES))


def _blank_prefix(data: bytes, imported: list[Any]) -> str:
    """A blank-node label prefix no merged file uses, for standardizing apart (UDR-0189 D5).

    Stored files write blank nodes as ``_:label`` and parser-made labels are hex, so a
    prefix with a non-hex letter that no file contains after ``_:`` is unused.
    """
    blobs = [data, *(item.data for item in imported)]
    for letter in "ijklmnopqrstuvwxyz":
        prefix = f"cw{letter}"
        if not any(f"_:{prefix}".encode() in blob for blob in blobs):
            return prefix
    return f"cwz{len(data)}"


_BLANK_LABEL_RE = re.compile(rb"_:([^\s;,()\[\]{}<>\"'^|]+)")


class _StableBlanks:
    """Parser-made blank-node labels made the same in every process (UDR-0193 D8).

    A parser labels the blank nodes a file writes without a label (``[ ... ]``, list
    cells) with random ids, so two loads of one file version disagree. Stored entailment
    results name blank nodes of the query Store, so such labels are replaced, in parse
    order, by ``<prefix><n>``; labels the file writes (``_:x``) are kept as they are.
    """

    def __init__(self, data: bytes, prefix: str) -> None:
        import pyoxigraph as ox

        self._ox = ox
        found = _BLANK_LABEL_RE.findall(data)
        self._explicit = {m.decode("utf-8", "replace") for m in found} | {
            m.rstrip(b".").decode("utf-8", "replace") for m in found
        }
        self._prefix = prefix
        self._map: dict[str, Any] = {}

    def term(self, term: Any) -> Any:
        ox = self._ox
        if isinstance(term, ox.BlankNode):
            if term.value in self._explicit:
                return term
            node = self._map.get(term.value)
            if node is None:
                node = self._map[term.value] = ox.BlankNode(f"{self._prefix}{len(self._map)}")
            return node
        if isinstance(term, ox.Triple):
            subject, obj = self.term(term.subject), self.term(term.object)
            if subject is term.subject and obj is term.object:
                return term
            return ox.Triple(subject, term.predicate, obj)
        return term

    def quad(self, quad: Any) -> Any:
        ox = self._ox
        subject, obj, graph = quad.subject, quad.object, quad.graph_name
        hidden = ox.BlankNode | ox.Triple
        if not isinstance(subject, hidden) and not isinstance(obj, hidden) and not isinstance(graph, ox.BlankNode):
            return quad
        new_subject, new_object = self.term(subject), self.term(obj)
        new_graph = self.term(graph) if isinstance(graph, ox.BlankNode) else graph
        if new_subject is subject and new_object is obj and new_graph is graph:
            return quad
        return ox.Quad(new_subject, quad.predicate, new_object, new_graph)


def _merge_imports(
    store: Any,
    imported: list[Any],
    data: bytes,
    own_graphs: dict[Any, None],
    kept: list[Any],
    own_named: int,
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, str]], list[Any]]:
    """Add the imported files to the query Store, read-only and standardized apart (D5).

    Each imported ontology is loaded as the named graph of its import IRI (unless the own
    file already has a graph of that name: then straight into the default graph), its
    blank nodes relabelled with a prefix no merged file uses; the named graphs are then
    merged into the default graph inside the engine (the Store is a set, so triples the
    own scope already has count once). Returns (default triples added, named quads
    added, notices, extra prefixes, the import graphs).
    """
    import pyoxigraph as ox

    prefix = _blank_prefix(data, imported)
    default_graph = ox.DefaultGraph()
    named_quads = 0
    notices: list[dict[str, Any]] = []
    prefixes: list[dict[str, str]] = []
    import_graphs: list[Any] = []
    default_before = len(store) - own_named
    for k, item in enumerate(imported):
        relabel: dict[str, Any] = {}

        def term(t: Any, k: int = k, relabel: dict[str, Any] = relabel) -> Any:
            if isinstance(t, ox.BlankNode):
                node = relabel.get(t.value)
                if node is None:
                    node = relabel[t.value] = ox.BlankNode(f"{prefix}{k}x{t.value}")
                return node
            if isinstance(t, ox.Triple):
                return ox.Triple(term(t.subject), t.predicate, term(t.object))
            return t

        graph_name = ox.NamedNode(item.iri)
        named_ok = graph_name not in own_graphs and graph_name not in import_graphs
        target = graph_name if named_ok else default_graph
        parser = ox.parse(item.data, format=ox.RdfFormat.TRIG)
        stable = _StableBlanks(item.data, f"{prefix}{k}y")

        def quads(parser: Any = parser, target: Any = target, term: Any = term, stable: Any = stable) -> Any:
            for parsed in parser:
                quad = stable.quad(parsed)
                subject, obj = quad.subject, quad.object
                if not isinstance(subject, ox.NamedNode):
                    subject = term(subject)
                if isinstance(obj, ox.BlankNode | ox.Triple):
                    obj = term(obj)
                if is_restorable(obj):
                    kept.append(ox.Triple(subject, quad.predicate, obj))
                yield ox.Quad(subject, quad.predicate, obj, target)

        before = len(store)
        try:
            store.extend(quads())
            for name, iri in (parser.prefixes or {}).items():
                prefixes.append({"prefix": name, "iri": iri})
        except (SyntaxError, ValueError):
            logger.warning("imported ontology %s could not be parsed", item.ontology_id, exc_info=True)
            notices.append(
                {
                    "code": "import_unreadable",
                    "message": f"The imported ontology <{item.iri}> could not be read; part of it may be missing.",
                }
            )
        if named_ok:
            named_quads += len(store) - before
            import_graphs.append(graph_name)
        else:
            notices.append(
                {
                    "code": "import_graph_name_in_use",
                    "message": (
                        f"The imported ontology <{item.iri}> is included in the default graph only: "
                        "this ontology already has a named graph of that name."
                    ),
                }
            )
    if import_graphs:
        _merge_into_default(store, import_graphs)
    added = len(store) - own_named - named_quads - default_before
    return added, named_quads, notices, prefixes, import_graphs


def _build_query_graph(entry: dict[str, str], data: bytes, scope: Any, imported: list[Any] | None = None) -> QueryGraph:
    """Parse one file version ONCE into an in-memory Store plus a lexical index.

    The parse streams straight into ``Store.extend`` rather than ``Store.load``,
    which relabels blank nodes: result blank nodes then carry the labels the
    projection shows (UDR-0181 D1). Only the triples whose object the engine may
    re-encode (typed literals, triple terms) are kept aside for the index (D2).

    The Store's default graph is the SET merge of the graphs in ``scope``; the
    engine's ``use_default_graph_as_union`` is not used because it returns one row
    per graph for a triple held in several graphs (UDR-0182 D4).
    """
    import pyoxigraph as ox

    from app.ontology.vocabulary import document_of

    dataset = is_dataset(entry)
    store = ox.Store()
    kept: list[Any] = []
    count = 0
    named_quads = 0
    graphs: dict[Any, None] = {}
    # Datasets only: in-scope triple -> the graphs that hold it. Also the set the merge
    # dedupes on (each triple is counted and indexed once).
    provenance: dict[Any, list[Any]] | None = {} if dataset else None
    document: dict[str, Any] = {"prefixes": [], "base": None, "version": None}
    if data.strip():
        parser = ox.parse(data, format=ox.RdfFormat.TRIG)
        stable = _StableBlanks(data, f"{_blank_prefix(data, imported or [])}p")

        def quads() -> Any:
            nonlocal count, named_quads
            for parsed in parser:
                quad = stable.quad(parsed)
                if provenance is None:
                    # A graph file (.ttl): every quad is in the default graph -- the
                    # PRP-0199 hot path, no extra objects per quad.
                    count += 1
                    if is_restorable(quad.object):
                        kept.append(quad.triple)
                    yield quad
                    continue
                named = not isinstance(quad.graph_name, ox.DefaultGraph)
                if named:
                    graphs.setdefault(quad.graph_name, None)
                    named_quads += 1
                    yield quad  # every named graph stays available to GRAPH patterns
                if not _in_scope(scope, quad):
                    continue
                triple = quad.triple
                holders = provenance.get(triple)
                if holders is not None:
                    if quad.graph_name not in holders:
                        holders.append(quad.graph_name)
                    continue  # the merge is a set: count and index each triple once
                provenance[triple] = [quad.graph_name]
                count += 1
                if is_restorable(quad.object):
                    kept.append(triple)
                if not named:
                    yield quad  # a default-graph quad in scope is already where it belongs

        try:
            store.extend(quads())
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"RDF parse error: {exc}") from exc
        document = document_of(parser, data)
        if graphs and scope != "default":
            _merge_into_default(store, list(graphs) if scope == "all" else [scope])
    import_notices: list[dict[str, Any]] = []
    import_graphs: list[Any] = []
    if imported:
        # PRP-0207 / UDR-0189 D5: the import closure, read-only and standardized apart.
        added, imported_named, import_notices, extra, import_graphs = _merge_imports(
            store, imported, data, graphs, kept, named_quads
        )
        count += added
        named_quads += imported_named
        bound = {p["prefix"] for p in document["prefixes"]} | {p["iri"] for p in document["prefixes"]}
        for item in extra:
            if item["prefix"] not in bound and item["iri"] not in bound:
                document = {**document, "prefixes": [*document["prefixes"], item]}
                bound |= {item["prefix"], item["iri"]}
        if provenance is not None:
            # A dataset's CONSTRUCT answers name where a triple came from (UDR-0182 D5).
            for graph_name in import_graphs:
                for quad in store.quads_for_pattern(None, None, None, graph_name):
                    provenance.setdefault(quad.triple, [graph_name])
    # The stored file is written by the codec, which never repeats a quad, so the named
    # quads are stored as parsed and (merged triples) - (default graph size) is what the
    # value encoding merged.
    store_triples = len(store) - named_quads
    index = build_index(kept, file_triples=count, store_triples=store_triples)
    return QueryGraph(
        store=store,
        index=index,
        document=document,
        graphs=list(graphs),
        scope=scope,
        provenance=provenance if graphs else None,
        imports=tuple((item.ontology_id, item.revision) for item in imported) if imported else None,
        import_notices=import_notices,
    )


# ---- Query cache (CTR-0170 v4, UDR-0183 D1-D3) ----------------------------------

# Memory charged per byte of file, measured on v0.177.0 at the 10 MB cap (PRP-0201
# 1.1: 25x graph file, 46x dataset all graphs, 27-30x dataset one graph) plus a margin.
CACHE_FACTOR_GRAPH = 28
CACHE_FACTOR_DATASET_ALL = 50
CACHE_FACTOR_DATASET_SCOPED = 32


@dataclass
class _CacheEntry:
    graph: QueryGraph
    cost: int


_cache: OrderedDict[tuple[Any, ...], _CacheEntry] = OrderedDict()
_cache_lock = threading.Lock()
_cache_total = 0
# One load per key at a time (D3): later requests for the key wait on its lock.
_loading: dict[tuple[Any, ...], threading.Lock] = {}
cache_stats = {"hits": 0, "misses": 0, "evictions": 0}


def cache_budget_bytes() -> int:
    """The ``ontology_query_cache_mb`` App Settings budget in bytes (0 = cache off)."""
    return max(0, int(settings.ontology_query_cache_mb)) * 2**20


def cache_cost(size: int, dataset: bool, scope: Any) -> int:
    """The memory an entry is charged: the file size times the factor of its kind."""
    if not dataset:
        factor = CACHE_FACTOR_GRAPH
    elif scope == "all":
        factor = CACHE_FACTOR_DATASET_ALL
    else:
        factor = CACHE_FACTOR_DATASET_SCOPED
    return max(1, size) * factor


def cache_key(
    ontology_id: str, revision: str, dataset: bool, scope: Any, imports: tuple[Any, ...] | None = None
) -> tuple[Any, ...]:
    """(id, revision, kind, scope[, imports]); a graph file's "all" and "default" share one entry (D1).

    ``imports`` is the (id, revision) of every imported ontology merged in (UDR-0189 D5):
    a save of any of them changes the key. Without imports the key keeps its shape.
    """
    if not dataset and scope == "default":
        scope = "all"
    if imports:
        return (ontology_id, revision, dataset, scope, tuple(imports))
    return (ontology_id, revision, dataset, scope)


def _evict_to(budget: int) -> None:
    """Drop least-recently-used entries until the total fits ``budget`` (lock held)."""
    global _cache_total
    while _cache and _cache_total > budget:
        _, dropped = _cache.popitem(last=False)
        _cache_total -= dropped.cost
        cache_stats["evictions"] += 1


def invalidate_query_cache(ontology_id: str | None = None) -> None:
    """Drop the cached entries of one ontology (all when None) to return memory early.

    Correctness never depends on this: a changed file has a new revision, so its
    old entries can no longer be looked up (D1).
    """
    global _cache_total
    with _cache_lock:
        for key in [k for k in _cache if ontology_id is None or k[0] == ontology_id]:
            _cache_total -= _cache.pop(key).cost


def cached_entries() -> list[tuple[Any, ...]]:
    """The keys currently cached, least recently used first (tests and diagnostics)."""
    with _cache_lock:
        return list(_cache)


def include_imports(value: bool | None) -> bool:
    """The ``imports`` choice of a query lane: as given, else the App Settings default (D5)."""
    if value is None:
        return bool(settings.ontology_query_include_imports)
    return bool(value)


def resolve_query(
    ontology_id: str, scope: Any = "all", imports: bool | None = None
) -> tuple[dict[str, str], bytes, Any, list[Any], list[dict[str, Any]], tuple[Any, ...]]:
    """What a query lane request reads: (entry, bytes, scope, imported, notices, cache key).

    The stored bytes are read and hashed (about 20 ms at the 10 MB cap); the key carries
    that revision and the (id, revision) of every imported ontology (UDR-0183 D1,
    UDR-0189 D5). Raises ``KeyError`` for an unknown id, ``ValueError`` for a bad scope.
    """
    pair = read_entry_and_bytes(ontology_id)
    if pair is None:
        raise KeyError(ontology_id)
    entry, data = pair
    scope = parse_scope(scope)
    imported: list[Any] = []
    closure_notices: list[dict[str, Any]] = []
    if include_imports(imports):
        # PRP-0207 / UDR-0189 D4 / D5: the import closure, resolved in the catalog only.
        from app.ontology.imports import compute_closure

        closure = compute_closure(ontology_id)
        imported = closure.loaded
        closure_notices = [{"code": "imports", "message": text} for text in closure.notices]
    signature = tuple((item.ontology_id, item.revision) for item in imported) or None
    key = cache_key(ontology_id, revision_of(data), is_dataset(entry), scope, signature)
    return entry, data, scope, imported, closure_notices, key


def query_key(ontology_id: str, scope: Any = "all", imports: bool | None = None) -> tuple[Any, ...]:
    """The cache key a query lane request would load (UDR-0193 D8: stored results use it)."""
    return resolve_query(ontology_id, scope, imports)[5]


def load_store(ontology_id: str, scope: Any = "all", imports: bool | None = None) -> QueryGraph:
    """The ontology loaded for the query lanes, from the cache when its revision is cached.

    The stored bytes are read and hashed on every call (about 20 ms at the 10 MB
    cap); the key carries that revision, so an entry built from other bytes is never
    returned (UDR-0183 D1). Within the ``ontology_query_cache_mb`` budget entries are
    kept least-recently-used first; one larger than the budget is built, used and not
    kept; a budget of 0 builds every time (D3). Concurrent requests for one key share
    one load. A cached entry is never changed (D2): the Store only ever answers
    ``query()`` and the lexical index fills its lookup tables under its own lock.
    """
    global _cache_total
    entry, data, scope, imported, closure_notices, key = resolve_query(ontology_id, scope, imports)
    budget = cache_budget_bytes()
    if budget <= 0:
        if _cache:
            invalidate_query_cache()
        graph = _build_query_graph(entry, data, scope, imported)
        graph.key = key
        return _with_notices(graph, closure_notices)
    dataset = is_dataset(entry)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            cache_stats["hits"] += 1
            return _with_notices(hit.graph, closure_notices)
        slot = _loading.setdefault(key, threading.Lock())
    with slot:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None:  # another request loaded it while this one waited
                _cache.move_to_end(key)
                cache_stats["hits"] += 1
                return _with_notices(hit.graph, closure_notices)
            cache_stats["misses"] += 1
        try:
            graph = _build_query_graph(entry, data, scope, imported)
            graph.key = key
        except BaseException:
            with _cache_lock:
                _loading.pop(key, None)
            raise
        imported_cost = sum(len(item.data) for item in imported) * CACHE_FACTOR_GRAPH
        cost = cache_cost(len(data), dataset, scope) + imported_cost
        with _cache_lock:
            # Insert and release the key in one step, so no request starts a second load.
            _loading.pop(key, None)
            budget = cache_budget_bytes()  # the setting may have changed during the load
            if cost <= budget:
                _cache[key] = _CacheEntry(graph, cost)
                _cache_total += cost
            _evict_to(budget)
        return _with_notices(graph, closure_notices)


def _with_notices(graph: QueryGraph, notices: list[dict[str, Any]]) -> QueryGraph:
    """The graph with this request's closure notices (a shallow copy; the cached entry is unchanged)."""
    if not notices:
        return graph
    from dataclasses import replace

    return replace(graph, import_notices=[*notices, *graph.import_notices])


def _term_to_str(term: Any) -> str:
    """A display string for a binding term (IRI value, literal value, or bnode label)."""
    value = getattr(term, "value", None)
    if value is not None:
        return str(value)
    return str(term) if term is not None else ""


def _construct_prefixes(document: dict[str, Any]) -> dict[str, str]:
    """The document's prefixes first, then built-ins for unbound names (UDR-0181 D5)."""
    from app.ontology.vocabulary import _with_builtin_prefixes

    return {p["prefix"]: p["iri"] for p in _with_builtin_prefixes(list(document.get("prefixes") or []))}


def _serialize_answer(items: list[Any], fmt: Any, prefixes: dict[str, str]) -> str:
    import pyoxigraph as ox

    try:
        return ox.serialize(items, format=fmt, prefixes=prefixes).decode("utf-8")
    except ValueError:  # an unusable document prefix -- fall back to the built-ins
        from app.ontology.vocabulary import TURTLE_PREFIXES

        return ox.serialize(items, format=fmt, prefixes=TURTLE_PREFIXES).decode("utf-8")


# ---- Entailment (CTR-0170 v7, PRP-0209, UDR-0191) --------------------------------

# Memory charged per inferred statement held in a cached Store (UDR-0191 D8): measured
# on v0.186.0 at about 310 bytes per quad in the in-memory Store, charged twice that.
CACHE_COST_PER_INFERRED = 600


def _charge_cache(graph: QueryGraph, extra: int) -> None:
    """Add ``extra`` bytes to the cached entry holding ``graph``'s Store, then evict (D8)."""
    global _cache_total
    with _cache_lock:
        for entry in _cache.values():
            if entry.graph.store is graph.store:
                entry.cost += extra
                _cache_total += extra
                break
        _evict_to(cache_budget_bytes())


# Inconsistency items an answer lists (PRP-0210 2.5; operator Q3 left it to the proposal).
INCONSISTENCY_NOTICE_ITEMS = 20

_LOCAL_NAME = re.compile(r"[A-Za-z_][\w-]*")


def _display(term: Any, prefixes: dict[str, str]) -> str:
    """A term as an answer writes it: ``prefix:local`` when a prefix fits, else N-Triples."""
    import pyoxigraph as ox

    if isinstance(term, ox.NamedNode):
        best: tuple[str, str] | None = None
        for prefix, iri in prefixes.items():
            if iri and term.value.startswith(iri) and (best is None or len(iri) > len(best[1])):
                local = term.value[len(iri) :]
                if not local or _LOCAL_NAME.fullmatch(local):
                    best = (prefix, iri)
        if best is not None:
            return f"{best[0]}:{term.value[len(best[1]) :]}"
        return f"<{term.value}>"
    return str(term)


def display_finding(
    graph: QueryGraph, finding: dict[str, Any], prefixes: dict[str, str] | None = None
) -> dict[str, Any]:
    """One inconsistency finding as answers show it: kind, graph, message, terms (UDR-0192 D5)."""
    if prefixes is None:
        prefixes = _construct_prefixes(graph.document)
    shown = [
        term if isinstance(term, str) else _display(restore_term(graph.index, term)[0], prefixes)
        for term in finding["terms"]
    ]
    try:
        message = finding["template"] % tuple(shown)
    except (TypeError, ValueError):
        message = finding["template"]
    graph_name = finding.get("graph")
    return {
        "kind": finding["kind"],
        "graph": None if graph_name is None else _display(graph_name, prefixes),
        "message": message,
        "terms": shown,
    }


def _entailment_notices(
    graph: QueryGraph, mode: str, findings: list[dict[str, Any]], same_as: int
) -> list[dict[str, Any]]:
    """The notices every ``mode`` answer carries (UDR-0192 D5, D8)."""
    from app.ontology import entailment

    notices: list[dict[str, Any]] = []
    prefixes = _construct_prefixes(graph.document)
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        item = display_finding(graph, finding, prefixes)
        key = (item["kind"], item["message"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    if items:
        order = {kind: i for i, kind in enumerate(entailment.FINDING_KINDS)}
        items.sort(key=lambda item: (order.get(item["kind"], len(order)), item["message"]))
        notices.append(
            {
                "code": "entailment_inconsistent",
                "message": (
                    f"The ontology is inconsistent under {entailment.MODE_LABELS[mode]} "
                    f"({len(items)} finding(s)). An inconsistent ontology entails everything; this "
                    "answer holds only what the rules derived."
                ),
                "count": len(items),
                "truncated": len(items) > INCONSISTENCY_NOTICE_ITEMS,
                "items": items[:INCONSISTENCY_NOTICE_ITEMS],
            }
        )
    if same_as:
        notices.append(
            {
                "code": "entailment_same_as",
                "message": (
                    f"{same_as} owl:sameAs pair(s) between different names: the statements of each "
                    "name are copied to the other."
                ),
                "count": same_as,
            }
        )
    return notices


def _same_as_pairs(statements: list[Any]) -> int:
    """owl:sameAs pairs between different names (UDR-0192 D8)."""
    same_as = "http://www.w3.org/2002/07/owl#sameAs"
    return len(
        {frozenset((t.subject, t.object)) for t in statements if t.predicate.value == same_as and t.subject != t.object}
    )


@dataclass
class EntailmentComputation:
    """The inferred statements of one mode for one query Store (UDR-0191 D3 / D7)."""

    merge_inferred: list[Any]
    per_graph: dict[Any, list[Any]]
    findings: list[dict[str, Any]]
    same_as: int
    count: int
    stated: int


def compute_entailment(graph: QueryGraph, mode: str) -> EntailmentComputation:
    """Run the closure of ``mode`` over ``graph``'s Store: the merge and each named graph.

    Raises ``EntailmentTooLarge`` above the input bound (``reason: "input"``) or, for
    ``owl-rl``, above K inferred statements per stated one (``reason: "inferred_ratio"``,
    UDR-0192 D8), and ``EntailmentFailed`` when the engine fails.
    """
    import pyoxigraph as ox

    from app.ontology import entailment

    default_graph = ox.DefaultGraph()
    merged = [quad.triple for quad in graph.store.quads_for_pattern(None, None, None, default_graph)]
    named = {
        name: [quad.triple for quad in graph.store.quads_for_pattern(None, None, None, name)] for name in graph.graphs
    }
    owl_rl = mode == entailment.OWL_RL
    findings: list[dict[str, Any]] | None = [] if owl_rl else None
    merge_inferred, per_graph = entailment.scoped_closure(merged, named, mode=mode, findings=findings)
    count = len(merge_inferred) + sum(len(inferred) for inferred in per_graph.values())
    same_as = _same_as_pairs([*merged, *merge_inferred]) if owl_rl else 0
    if owl_rl:
        ratio_limit = entailment.max_inferred_ratio()
        if count > ratio_limit * max(1, len(merged)):
            ratio = count / max(1, len(merged))
            message = (
                f"OWL 2 RL entailment inferred {count:,} statements from {len(merged):,} "
                f"({ratio:.1f} per statement); at most {ratio_limit:,} per statement are accepted "
                f"(App Settings: {entailment.RATIO_SETTING})."
            )
            if same_as:
                message += f" {same_as:,} owl:sameAs pair(s) copy statements between names."
            raise entailment.EntailmentTooLarge(
                message,
                statements=len(merged),
                inferred=count,
                ratio=round(ratio, 1),
                limit=ratio_limit,
                reason="inferred_ratio",
                setting=entailment.RATIO_SETTING,
                same_as_pairs=same_as,
            )
    return EntailmentComputation(merge_inferred, per_graph, findings or [], same_as, count, len(merged))


def _apply_entailment(graph: QueryGraph, mode: str, result: EntailmentComputation) -> int:
    """Build ``mode``'s entailed copy of ``graph``'s Store from ``result`` (lock held)."""
    import pyoxigraph as ox

    default_graph = ox.DefaultGraph()
    entailed = ox.Store()
    entailed.extend(graph.store)
    entailed.extend(ox.Quad(t.subject, t.predicate, t.object, default_graph) for t in result.merge_inferred)
    where: dict[Any, list[Any]] = {}
    for name, inferred in result.per_graph.items():
        entailed.extend(ox.Quad(t.subject, t.predicate, t.object, name) for t in inferred)
        for t in inferred:
            where.setdefault(t, []).append(name)
    graph.entailed[mode] = entailed
    graph.inferred_graphs[mode] = where
    graph.entailment_notices[mode] = _entailment_notices(graph, mode, result.findings, result.same_as)
    graph.entailments[mode] = result.count
    return len(entailed)


def ensure_entailment(graph: QueryGraph, mode: str, *, compute: bool = True) -> int:
    """Build ``mode``'s entailed copy of the query Store, once (D3, D7, D8).

    The copy holds every quad of ``graph.store`` plus the inferred statements: those of
    the merge (the default graph -- the scope merge, imports included when loaded with
    them) in the default graph, and those of each of the file's named graphs in that
    graph (``entailment.scoped_closure``). ``graph.store`` is never changed, so
    ``simple`` answers are exactly the stated ones. Returns the number of inferred
    statements. ``simple`` needs nothing.

    PRP-0211 (UDR-0193 D8 / D9): the inferred statements come from the STORED result of
    the graph's key when there is one (computed after a save); a stored refusal is
    raised as it was recorded. Without one, ``compute`` runs the closure here (and stores
    the result); ``compute=False`` raises ``EntailmentPending`` instead.

    ``owl-rl`` (PRP-0210, UDR-0192): the inconsistencies found become a notice every
    answer of the mode carries (D5); a result with more than K inferred statements per
    stated one is refused (D8), and the refusal is remembered with the Store.
    """
    from app.ontology import entailment
    from app.ontology import entailment_results as results

    if mode == entailment.SIMPLE:
        return 0
    with graph.entailment_lock:
        held = graph.entailments.get(mode)
        if held is not None:
            return held
        refused = graph.entailment_refusals.get(mode)
        if refused is not None:
            if not results.refusal_stale(refused[1], mode):
                raise entailment.EntailmentTooLarge(refused[0], **refused[1])
            graph.entailment_refusals.pop(mode, None)  # a bound was raised since
        record = results.lookup(graph.key, mode)
        if record is not None:
            if record.get("state") != results.READY:
                exc = results.refusal(record)
                if record.get("state") in (results.TOO_LARGE, results.FAILED):
                    graph.entailment_refusals[mode] = (
                        str(exc),
                        {k: v for k, v in exc.detail.items() if k not in ("code", "message")},
                    )
                raise exc
            quads = _apply_entailment(graph, mode, results.load(graph.key, mode, record))
        elif not compute:
            raise entailment.EntailmentPending(
                f"{entailment.MODE_LABELS[mode]} of this ontology is still being computed; try again shortly.",
                mode=mode,
                state="queued",
            )
        else:
            started = time.monotonic()
            try:
                result = compute_entailment(graph, mode)
            except entailment.EntailmentTooLarge as exc:
                if exc.detail.get("reason") == "inferred_ratio":
                    graph.entailment_refusals[mode] = (
                        str(exc),
                        {k: v for k, v in exc.detail.items() if k not in ("code", "message")},
                    )
                results.save_refusal(graph.key, mode, exc, elapsed_ms=int((time.monotonic() - started) * 1000))
                raise
            except entailment.EntailmentFailed as exc:
                results.save_refusal(graph.key, mode, exc, elapsed_ms=int((time.monotonic() - started) * 1000))
                raise
            results.save_ready(
                graph.key, mode, result, elapsed_ms=int((time.monotonic() - started) * 1000), graph=graph
            )
            quads = _apply_entailment(graph, mode, result)
        count = graph.entailments[mode]
    _charge_cache(graph, quads * CACHE_COST_PER_INFERRED)
    return count


def _run(store: Any, sparql: str) -> Any:
    try:
        return store.query(sparql)
    except Exception as exc:  # SyntaxError and friends from the SPARQL parser
        raise ValueError(f"SPARQL error: {exc}") from exc


def _row_key(solution: Any, variables: list[Any]) -> tuple[str | None, ...]:
    """A Store-level identity of one solution (restoration is a function of it)."""
    out: list[str | None] = []
    for variable in variables:
        term = solution[variable]
        out.append(None if term is None else str(term))
    return tuple(out)


def execute_query(
    graph: QueryGraph, sparql: str, *, max_construct_triples: int = 0, entailment: str = "simple"
) -> dict[str, Any]:
    """Run a READ-ONLY SPARQL query and shape the result for CTR-0171 / CTR-0172.

    - SELECT   -> {kind, columns, rows, cells, row_count, truncated, entity_iris, notices}
                  (entity_iris = the IRIs bound anywhere in the results, so the
                  UI can drive the strong/dim canvas highlight -- RESULT-1;
                  cells = the same values as typed RDF 1.2 Term JSON)
    - CONSTRUCT/DESCRIBE -> {kind, format, turtle, triple_count, truncated, notices}
                  (``format`` is "trig" for an ontology with named graphs: each
                  triple is written under every in-scope graph that holds it, and
                  ``turtle`` then carries TriG text -- UDR-0182 D5)
    - ASK      -> {kind, value, notices}

    Every response also carries ``scope`` and ``entailment``. Results are restored to
    the file's lexical forms and blank-node labels (UDR-0181 D2); ``notices`` state
    what could not be (D3). ``Store.query()`` cannot mutate; a SPARQL UPDATE string
    fails its parser. Raises ``ValueError`` with the parser message on an invalid query.

    With an entailment mode other than ``simple`` (PRP-0209, UDR-0191 D6) the query
    runs on the mode's entailed copy of the Store (stated + inferred, also inside the
    named graphs) and again on the stated Store; what only the first run gives is
    marked inferred: SELECT ``row_inferred`` (one bool per row) and ``inferred_count``;
    CONSTRUCT / DESCRIBE ``turtle`` (the stated part), ``inferred_turtle`` (the
    answer's format) and ``inferred_count``; ASK ``inferred``.
    Raises ``EntailmentError`` (a ``ValueError``) for a refused mode or input size.
    """
    import pyoxigraph as ox

    from app.ontology import entailment as ent
    from app.ontology.vocabulary import term_to_json

    mode = ent.parse_mode(entailment)
    row_bound = max_rows()
    index = graph.index
    scope = graph.scope if isinstance(graph.scope, str) else term_to_json(graph.scope)
    entailed = mode != ent.SIMPLE
    if entailed:
        ensure_entailment(graph, mode)
    # The entailed copy answers every query form as written -- GRAPH, FROM and FROM NAMED
    # included (UDR-0191 D3 / D7); the stated Store gives the baseline for the marks.
    result = _run(graph.entailed[mode] if entailed else graph.store, sparql)
    baseline = _run(graph.store, sparql) if entailed else None

    def notices_with(ambiguous: int) -> list[dict[str, Any]]:
        return [
            *graph.import_notices,
            *graph.entailment_notices.get(mode, []),
            *lexical_notices(index, sparql, ambiguous),
        ]

    if isinstance(result, ox.QuerySolutions):
        columns = [str(v).lstrip("?") for v in result.variables]
        rows: list[list[str]] = []
        cells: list[list[dict[str, Any] | None]] = []
        row_inferred: list[bool] = []
        entity_iris: set[str] = set()
        truncated = False
        ambiguous = 0
        computed = computed_variables(sparql)
        stated_rows: Counter[tuple[str | None, ...]] | None = None
        if baseline is not None:
            stated_rows = Counter(_row_key(solution, baseline.variables) for solution in baseline)
        for solution in result:
            if len(rows) >= row_bound:
                truncated = True
                break
            row: list[str] = []
            cell_row: list[dict[str, Any] | None] = []
            for variable in result.variables:
                term, forms = solution[variable], None
                if variable.value not in computed:  # a computed value is not a file term
                    term, forms = restore_term(index, term)
                row.append(_term_to_str(term))
                cell = term_to_json(term) if term is not None else None
                if cell is not None and forms:
                    cell["lexical_forms"] = forms
                    ambiguous += 1
                cell_row.append(cell)
                if isinstance(term, ox.NamedNode):
                    entity_iris.add(term.value)
            if stated_rows is not None:
                key = _row_key(solution, result.variables)
                if stated_rows[key] > 0:
                    stated_rows[key] -= 1
                    row_inferred.append(False)
                else:
                    row_inferred.append(True)
            rows.append(row)
            cells.append(cell_row)
        answer: dict[str, Any] = {
            "kind": "select",
            "columns": columns,
            "rows": rows,
            "cells": cells,
            "row_count": len(rows),
            "truncated": truncated,
            "entity_iris": sorted(entity_iris),
            "scope": scope,
            "entailment": mode,
            "notices": notices_with(ambiguous),
        }
        if stated_rows is not None:
            answer["row_inferred"] = row_inferred
            answer["inferred_count"] = sum(row_inferred)
            removed = sum(count for count in stated_rows.values() if count > 0)
            if removed and not truncated:
                answer["notices"].append(
                    {
                        "code": "entailment_removed_rows",
                        "message": f"{removed} row(s) of the answer without inference are not in this answer.",
                    }
                )
        return answer

    if isinstance(result, ox.QueryBoolean):
        answer = {
            "kind": "ask",
            "value": bool(result),
            "scope": scope,
            "entailment": mode,
            "notices": notices_with(0),
        }
        if baseline is not None:
            answer["inferred"] = bool(result) and not bool(baseline)
        return answer

    # Remaining result kind: triples from CONSTRUCT / DESCRIBE. The cap and the
    # count apply AFTER restoration (merged triples come back).
    raw = list(result)
    inferred_raw: list[Any] = []
    if baseline is not None:
        stated_set = set(baseline)
        inferred_raw = [t for t in raw if t not in stated_set]
        raw = [t for t in raw if t in stated_set]
    triples, ambiguous = restore_triples(index, raw)
    inferred_triples, inferred_ambiguous = restore_triples(index, inferred_raw) if inferred_raw else ([], 0)
    truncated = False
    if max_construct_triples and len(triples) + len(inferred_triples) > max_construct_triples:
        # Stated first (PRP-0209 2.7): the cap counts both parts.
        truncated = True
        triples = triples[:max_construct_triples]
        inferred_triples = inferred_triples[: max_construct_triples - len(triples)]
    prefixes = _construct_prefixes(graph.document)
    text = ""
    fmt = "turtle"
    if graph.provenance is not None:
        # A dataset: keep where each triple came from (UDR-0182 D5). Template-built
        # triples (not in the file) go to the default block.
        fmt = "trig"
        default_graph = ox.DefaultGraph()
        quads = [
            ox.Quad(t.subject, t.predicate, t.object, g)
            for t in triples
            for g in graph.provenance.get(t) or [default_graph]
        ]
        # One block per graph: the writer opens a new block whenever the graph changes.
        order = {g: i for i, g in enumerate([default_graph, *graph.graphs])}
        quads.sort(key=lambda q: order.get(q.graph_name, len(order)))
        if quads:
            text = _serialize_answer(quads, ox.RdfFormat.TRIG, prefixes)
    elif triples:
        text = _serialize_answer(triples, ox.RdfFormat.TURTLE, prefixes)
    answer = {
        "kind": "construct",
        "format": fmt,
        "turtle": text,
        "triple_count": len(triples) + len(inferred_triples),
        "truncated": truncated,
        "scope": scope,
        "entailment": mode,
        "notices": notices_with(ambiguous + inferred_ambiguous),
    }
    if baseline is not None:
        # The inferred part in the answer's format: for a dataset each inferred triple is
        # written under the named graphs it was inferred in, else in the default block
        # (UDR-0191 D7).
        inferred_text = ""
        if inferred_triples and fmt == "trig":
            where = graph.inferred_graphs.get(mode, {})
            default_graph = ox.DefaultGraph()
            inferred_quads = [
                ox.Quad(t.subject, t.predicate, t.object, g)
                for t in inferred_triples
                for g in where.get(t) or [default_graph]
            ]
            order = {g: i for i, g in enumerate([default_graph, *graph.graphs])}
            inferred_quads.sort(key=lambda q: order.get(q.graph_name, len(order)))
            inferred_text = _serialize_answer(inferred_quads, ox.RdfFormat.TRIG, prefixes)
        elif inferred_triples:
            inferred_text = _serialize_answer(inferred_triples, ox.RdfFormat.TURTLE, prefixes)
        answer["inferred_turtle"] = inferred_text
        answer["inferred_count"] = len(inferred_triples)
    # The same triples as rows for a table (CTR-0171 v9): typed subject / predicate /
    # object, the named graph (None = the default graph) and whether the row is inferred.
    # A triple held in several graphs gives one row per graph. Capped like SELECT.
    where = graph.inferred_graphs.get(mode, {}) if baseline is not None else {}
    rows: list[dict[str, Any]] = []
    for inferred, part in ((False, triples), (True, inferred_triples)):
        for t in part:
            if graph.provenance is None:
                holders: list[Any] = [None]
            elif inferred:
                holders = where.get(t) or [None]
            else:
                holders = [g for g in graph.provenance.get(t) or [None] if not isinstance(g, ox.DefaultGraph)] or [None]
            for g in holders:
                rows.append(
                    {
                        "s": term_to_json(t.subject),
                        "p": term_to_json(t.predicate),
                        "o": term_to_json(t.object),
                        "graph": term_to_json(g) if g is not None else None,
                        "inferred": inferred,
                    }
                )
    answer["triples"] = rows[:row_bound]
    answer["triples_truncated"] = len(rows) > row_bound
    return answer


def graph_names(ontology_id: str) -> list[str]:
    """The named graphs of a stored dataset (N-Triples-style text), in file order; [] for a graph."""
    import pyoxigraph as ox

    entry = get_entry(ontology_id)
    if entry is None or not is_dataset(entry):
        return []
    names: dict[str, None] = {}
    data = read_ontology_bytes(ontology_id) or b""
    try:
        for quad in ox.parse(data, format=ox.RdfFormat.TRIG):
            if not isinstance(quad.graph_name, ox.DefaultGraph):
                names.setdefault(str(quad.graph_name), None)
    except (SyntaxError, ValueError):
        return []
    return list(names)


__all__ = [
    "FILE_SUFFIXES",
    "QueryGraph",
    "add_write_listener",
    "cache_stats",
    "cached_entries",
    "compute_entailment",
    "create_ontology",
    "delete_ontology",
    "display_finding",
    "ensure_entailment",
    "execute_query",
    "get_entry",
    "graph_names",
    "include_imports",
    "invalidate_query_cache",
    "is_dataset",
    "load_store",
    "max_rows",
    "notify_written",
    "ontology_dir",
    "parse_scope",
    "query_key",
    "read_catalog",
    "read_entry_and_bytes",
    "read_ontology_bytes",
    "rename_ontology",
    "resolve_query",
    "restore_deleted_ontology",
    "revision_of",
    "save_ontology_text",
    "scope_label",
    "write_catalog",
    "write_lock",
]
