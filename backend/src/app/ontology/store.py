"""Ontology store and catalog (CTR-0170, PRP-0105, UDR-0084 D3/D7/D10).

File layout under ``ONTOLOGY_DIR`` (created on demand):

    catalog.json              -- the catalog SSOT: [{id, name, description, file,
                                 created_at, updated_at}]
    <id>.ttl                  -- ONE self-contained file per ontology (SSOT): Turtle,
    <id>.trig                    or TriG once it has a named graph (UDR-0182 D3)
    <id>.ttl.bak-<timestamp>  -- automatic backup on every save / pre-delete

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

Loaded query Stores are cached per (ontology, revision, scope) within the
``ontology_query_cache_mb`` App Settings budget; the revision is the SHA-256 of
the bytes on disk, so a cached entry can never answer for another file version
(CTR-0170 v4, UDR-0183 D1-D3). The callers run this module's work in worker
threads (D4): catalog read-modify-writes hold ``_CATALOG_LOCK`` and every
"check revision -> read -> apply -> write" holds ``write_lock(id)``.
"""

from __future__ import annotations

from collections import OrderedDict
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
SELECT_MAX_ROWS = 1000

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
    return {
        "id": entry_id,
        "name": str(raw.get("name") or "").strip() or entry_id,
        "description": str(raw.get("description") or "").strip(),
        "file": file_name,
        "created_at": str(raw.get("created_at") or "").strip() or _now(),
        "updated_at": str(raw.get("updated_at") or "").strip() or _now(),
    }


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
) -> dict[str, str]:
    """Create a new catalog entry + its file.

    ``initial_turtle`` is the file content, or a factory called with the new id
    (a new ontology's header binds ``:`` to its own minting namespace). ``dataset``
    (or a callable asked after the factory ran) stores the content as TriG.
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
        entries.append(entry)
        write_catalog(entries)
    logger.info("ontology created: %s (%s)", entry_id, entry["name"])
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
    """Backup-then-remove the Turtle file and drop the catalog entry (UDR-0084 D10)."""
    with write_lock(ontology_id):
        entry = get_entry(ontology_id)
        if entry is None:
            return False
        path = _file_path(entry)
        _backup(path)
        if path.is_file():
            path.unlink()
        with _CATALOG_LOCK:
            write_catalog([e for e in read_catalog() if e["id"] != ontology_id])
    invalidate_query_cache(ontology_id)
    logger.info("ontology deleted: %s (backup kept)", ontology_id)
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


def save_ontology_text(ontology_id: str, turtle: str, *, dataset: bool | None = None) -> str | None:
    """Guarded save: size cap -> backup -> temp + atomic replace. Returns backup name.

    ``dataset`` switches the stored file between ``<id>.ttl`` (Turtle) and
    ``<id>.trig`` (TriG) when it differs from the current one (UDR-0182 D3): the new
    file is written first, then the catalog points at it, and the previous file is
    kept only as its backup. ``None`` keeps the current file.
    """
    with write_lock(ontology_id):
        backup_name = _save_locked(ontology_id, turtle, dataset)
    invalidate_query_cache(ontology_id)
    return backup_name


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


def _build_query_graph(entry: dict[str, str], data: bytes, scope: Any) -> QueryGraph:
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

        def quads() -> Any:
            nonlocal count, named_quads
            for quad in parser:
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


def cache_key(ontology_id: str, revision: str, dataset: bool, scope: Any) -> tuple[Any, ...]:
    """(id, revision, kind, scope); a graph file's "all" and "default" share one entry (D1)."""
    if not dataset and scope == "default":
        scope = "all"
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


def load_store(ontology_id: str, scope: Any = "all") -> QueryGraph:
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
    pair = read_entry_and_bytes(ontology_id)
    if pair is None:
        raise KeyError(ontology_id)
    entry, data = pair
    scope = parse_scope(scope)
    budget = cache_budget_bytes()
    if budget <= 0:
        if _cache:
            invalidate_query_cache()
        return _build_query_graph(entry, data, scope)
    dataset = is_dataset(entry)
    key = cache_key(ontology_id, revision_of(data), dataset, scope)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            cache_stats["hits"] += 1
            return hit.graph
        slot = _loading.setdefault(key, threading.Lock())
    with slot:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None:  # another request loaded it while this one waited
                _cache.move_to_end(key)
                cache_stats["hits"] += 1
                return hit.graph
            cache_stats["misses"] += 1
        try:
            graph = _build_query_graph(entry, data, scope)
        except BaseException:
            with _cache_lock:
                _loading.pop(key, None)
            raise
        cost = cache_cost(len(data), dataset, scope)
        with _cache_lock:
            # Insert and release the key in one step, so no request starts a second load.
            _loading.pop(key, None)
            budget = cache_budget_bytes()  # the setting may have changed during the load
            if cost <= budget:
                _cache[key] = _CacheEntry(graph, cost)
                _cache_total += cost
            _evict_to(budget)
        return graph


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


def execute_query(graph: QueryGraph, sparql: str, *, max_construct_triples: int = 0) -> dict[str, Any]:
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

    Every response also carries ``scope``. Results are restored to the file's
    lexical forms and blank-node labels (UDR-0181 D2); ``notices`` state what
    could not be (D3). ``Store.query()`` cannot mutate; a SPARQL UPDATE string
    fails its parser. Raises ``ValueError`` with the parser message on an invalid query.
    """
    import pyoxigraph as ox

    from app.ontology.vocabulary import term_to_json

    index = graph.index
    scope = graph.scope if isinstance(graph.scope, str) else term_to_json(graph.scope)
    try:
        result = graph.store.query(sparql)
    except Exception as exc:  # SyntaxError and friends from the SPARQL parser
        raise ValueError(f"SPARQL error: {exc}") from exc

    if isinstance(result, ox.QuerySolutions):
        columns = [str(v).lstrip("?") for v in result.variables]
        rows: list[list[str]] = []
        cells: list[list[dict[str, Any] | None]] = []
        entity_iris: set[str] = set()
        truncated = False
        ambiguous = 0
        computed = computed_variables(sparql)
        for solution in result:
            if len(rows) >= SELECT_MAX_ROWS:
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
            rows.append(row)
            cells.append(cell_row)
        return {
            "kind": "select",
            "columns": columns,
            "rows": rows,
            "cells": cells,
            "row_count": len(rows),
            "truncated": truncated,
            "entity_iris": sorted(entity_iris),
            "scope": scope,
            "notices": lexical_notices(index, sparql, ambiguous),
        }

    if isinstance(result, ox.QueryBoolean):
        return {"kind": "ask", "value": bool(result), "scope": scope, "notices": lexical_notices(index, sparql, 0)}

    # Remaining result kind: triples from CONSTRUCT / DESCRIBE. The cap and the
    # count apply AFTER restoration (merged triples come back).
    triples, ambiguous = restore_triples(index, list(result))
    truncated = False
    if max_construct_triples and len(triples) > max_construct_triples:
        triples = triples[:max_construct_triples]
        truncated = True
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
    return {
        "kind": "construct",
        "format": fmt,
        "turtle": text,
        "triple_count": len(triples),
        "truncated": truncated,
        "scope": scope,
        "notices": lexical_notices(index, sparql, ambiguous),
    }


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
    "SELECT_MAX_ROWS",
    "QueryGraph",
    "cache_stats",
    "cached_entries",
    "create_ontology",
    "delete_ontology",
    "execute_query",
    "get_entry",
    "graph_names",
    "invalidate_query_cache",
    "is_dataset",
    "load_store",
    "ontology_dir",
    "parse_scope",
    "read_catalog",
    "read_entry_and_bytes",
    "read_ontology_bytes",
    "rename_ontology",
    "revision_of",
    "save_ontology_text",
    "scope_label",
    "write_catalog",
    "write_lock",
]
