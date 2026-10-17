"""Stored entailment results, one per ontology version (CTR-0170 v9, PRP-0211, UDR-0193 D8).

The inferred statements of an entailment mode are computed after every write (by the
``ontology-entailment`` Pipeline Job, ``entailment_jobs``) or at the first search that
needs them, and kept beside the ontology files -- never in them:

    <results dir>/<ontology id>/<key hash>-<mode>.nq.gz   inferred statements
    <results dir>/<ontology id>/<key hash>-<mode>.json    the record

The results dir is ``ONTOLOGY_RESULTS_DIR`` (PRP-0212, UDR-0194 D4; default
``<ontology_dir>/.entailment``); the SHACL validation results (``shacl_results``) live in
the same per-ontology folders.

The key is the query-cache key -- (id, revision, kind, scope, imports signature),
UDR-0183 D1 / UDR-0189 D5 -- so a result answers for exactly the file versions it was
computed from. The record names the engine (``entailment.engine_id``): a result of
another engine is never read. The N-Quads hold the query Store's own terms: the
default graph holds the merge's inferred statements, named graph G those inferred in G
(UDR-0191 D7); blank-node labels are the Store's, which ``store._StableBlanks`` makes
the same in every process.

States: ``ready`` (with the statements), ``too_large`` (input bound or output bound K),
``failed`` (the engine failed), ``timeout`` (the build ran out of time). Only the
current revision and import signature of each ontology are kept (``prune``); deleting
an ontology deletes its folder. This is a cache: deleting the folder is always safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
import gzip
import hashlib
import json
import logging
import shutil
import time
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from pathlib import Path

    from app.ontology.store import EntailmentComputation, QueryGraph

logger = logging.getLogger(__name__)

RECORD_VERSION = 1
RESULTS_DIRNAME = ".entailment"
READY = "ready"
TOO_LARGE = "too_large"
FAILED = "failed"
TIMEOUT = "timeout"
STATES = (READY, TOO_LARGE, FAILED, TIMEOUT)
# Findings kept in a record (the Checks pane lists them, UDR-0193 D13).
MAX_STORED_FINDINGS = 1000


_refused_dirs: set[str] = set()


def results_root() -> Path:
    """The results folder: ``ONTOLOGY_RESULTS_DIR`` (PRP-0212, UDR-0194 D4).

    Relative = under ``ONTOLOGY_DIR`` (default ``.entailment``, the PRP-0211 folder);
    absolute = as is. A value that is the ontology folder itself or one of its ancestors
    is refused with a warning and the default is used: pruning deletes files in the
    results folder, so it must never hold the ontology files.
    """
    from pathlib import Path

    from app.core.config import settings
    from app.ontology.store import ontology_dir

    base = ontology_dir()
    raw = str(getattr(settings, "ontology_results_dir", "") or "").strip() or RESULTS_DIRNAME
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved, home = candidate.resolve(), base.resolve()
    except OSError:
        return base / RESULTS_DIRNAME
    if resolved == home or resolved in home.parents:
        if raw not in _refused_dirs:
            _refused_dirs.add(raw)
            logger.warning(
                "ONTOLOGY_RESULTS_DIR=%r is the ontology folder or one of its parents; using %r instead",
                raw,
                RESULTS_DIRNAME,
            )
        return base / RESULTS_DIRNAME
    return candidate


def folder(ontology_id: str) -> Path:
    from app.ontology.store import _ID_RE

    if not _ID_RE.match(ontology_id):
        raise ValueError(f"Invalid ontology id: {ontology_id!r}")
    return results_root() / ontology_id


def scope_text(scope: Any) -> str:
    """A scope as stored: ``all``, ``default`` or the graph name in N-Triples."""
    return scope if isinstance(scope, str) else str(scope)


def key_fields(key: tuple[Any, ...]) -> dict[str, Any]:
    """The parts of a query-cache key as JSON."""
    imports = [list(pair) for pair in key[4]] if len(key) > 4 and key[4] else None
    return {
        "ontology_id": key[0],
        "revision": key[1],
        "dataset": bool(key[2]),
        "scope": scope_text(key[3]),
        "imports": imports,
    }


def key_hash(key: tuple[Any, ...]) -> str:
    text = json.dumps(key_fields(key), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _paths(key: tuple[Any, ...], mode: str) -> tuple[Path, Path]:
    base = folder(key[0]) / f"{key_hash(key)}-{mode}"
    return base.with_suffix(".nq.gz"), base.with_suffix(".json")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def read_record(key: tuple[Any, ...] | None, mode: str) -> dict[str, Any] | None:
    """The stored record of ``key`` and ``mode`` (any engine), or None."""
    if key is None:
        return None
    try:
        _, record_path = _paths(key, mode)
        if not record_path.is_file():
            return None
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("state") in STATES else None


def refusal_stale(detail: dict[str, Any] | None, mode: str) -> bool:
    """True when a recorded refusal would not happen with the CURRENT App Settings.

    A raised input bound, output bound K or build time limit makes the refusal of a
    smaller value stale: the result is computed again instead of being refused.
    """
    from app.core.config import settings
    from app.ontology import entailment

    detail = detail or {}
    reason = detail.get("reason")
    try:
        if reason == "input":
            return entailment.input_bound(mode) >= int(detail.get("statements") or 0)
        if reason == "inferred_ratio":
            return float(detail.get("ratio") or 0) <= entailment.max_inferred_ratio()
        if reason == "build_timeout":
            return int(settings.ontology_entailment_build_timeout_seconds) > float(detail.get("limit_seconds") or 0)
    except (TypeError, ValueError):
        return False
    return False


def lookup(key: tuple[Any, ...] | None, mode: str) -> dict[str, Any] | None:
    """The stored record of ``key`` and ``mode`` made by the CURRENT engine, or None.

    A refusal the current App Settings would no longer make is ignored (``refusal_stale``).
    """
    from app.ontology import entailment

    record = read_record(key, mode)
    if record is None or record.get("engine") != entailment.engine_id(mode):
        return None
    if record.get("state") in (TOO_LARGE, TIMEOUT) and refusal_stale(record.get("detail"), mode):
        return None
    if record.get("state") == READY:
        data_path, _ = _paths(key, mode)  # type: ignore[arg-type]
        if not data_path.is_file():
            return None
    return record


# ---- Terms --------------------------------------------------------------------------


def encode_term(term: Any) -> dict[str, str]:
    """A finding's term as JSON: an RDF term in N-Triples, or a plain text."""
    if isinstance(term, str):
        return {"text": term}
    return {"nt": str(term)}


def parse_term(text: str) -> Any:
    """An RDF term written in N-Triples (an object; a graph name parses the same way)."""
    import pyoxigraph as ox

    quad = next(iter(ox.parse(f"<urn:x-cw:s> <urn:x-cw:p> {text} .".encode(), format=ox.RdfFormat.N_TRIPLES)))
    return quad.object


def decode_term(item: dict[str, str]) -> Any:
    if "nt" in item:
        try:
            return parse_term(item["nt"])
        except (SyntaxError, ValueError, StopIteration):
            return item["nt"]
    return item.get("text", "")


def encode_finding(finding: dict[str, Any]) -> dict[str, Any]:
    graph = finding.get("graph")
    return {
        "kind": finding["kind"],
        "template": finding["template"],
        "graph": None if graph is None else str(graph),
        "terms": [encode_term(term) for term in finding["terms"]],
    }


def decode_finding(item: dict[str, Any]) -> dict[str, Any]:
    graph = item.get("graph")
    return {
        "kind": item["kind"],
        "template": item["template"],
        "graph": None if graph is None else decode_term({"nt": graph}),
        "terms": [decode_term(term) for term in item.get("terms") or []],
    }


def finding_key(item: dict[str, Any]) -> str:
    """A finding's stable key (kind + terms): unchanged while its cause is unchanged."""
    text = json.dumps([item["kind"], [term.get("nt", term.get("text")) for term in item.get("terms") or []]])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


# ---- Writing ------------------------------------------------------------------------


def _record(key: tuple[Any, ...], mode: str, state: str, **fields: Any) -> dict[str, Any]:
    from app.ontology import entailment

    return {
        "version": RECORD_VERSION,
        **key_fields(key),
        "mode": mode,
        "engine": entailment.engine_id(mode),
        "state": state,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **fields,
    }


def _write_record(key: tuple[Any, ...], mode: str, record: dict[str, Any]) -> None:
    _, record_path = _paths(key, mode)
    _atomic_write(record_path, json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"))


def save_ready(
    key: tuple[Any, ...] | None,
    mode: str,
    result: EntailmentComputation,
    *,
    elapsed_ms: int,
    graph: QueryGraph | None = None,
) -> None:
    """Store a computed result (best effort: a write failure is logged, never raised).

    With ``graph`` each finding also keeps how answers show it (message, terms, graph),
    so the Checks pane can list the findings without loading the ontology.
    """
    if key is None:
        return
    import pyoxigraph as ox

    try:
        default_graph = ox.DefaultGraph()
        quads = [ox.Quad(t.subject, t.predicate, t.object, default_graph) for t in result.merge_inferred]
        for name, inferred in result.per_graph.items():
            quads.extend(ox.Quad(t.subject, t.predicate, t.object, name) for t in inferred)
        data = ox.serialize(quads, format=ox.RdfFormat.N_QUADS) if quads else b""
        data_path, _ = _paths(key, mode)
        _atomic_write(data_path, gzip.compress(data, compresslevel=6))
        findings = []
        for finding in result.findings:
            item = encode_finding(finding)
            if graph is not None:
                from app.ontology.store import display_finding

                shown = display_finding(graph, finding)
                item.update(message=shown["message"], shown=shown["terms"], graph_shown=shown["graph"])
            findings.append(item)
        record = _record(
            key,
            mode,
            READY,
            stated=result.stated,
            inferred=result.count,
            per_graph={str(name): len(inferred) for name, inferred in result.per_graph.items()},
            elapsed_ms=elapsed_ms,
            findings=findings[:MAX_STORED_FINDINGS],
            findings_count=len(findings),
            same_as_pairs=result.same_as,
            message=None,
        )
        _write_record(key, mode, record)
    except OSError:
        logger.warning("could not store the %s result of %s", mode, key[0], exc_info=True)


def save_refusal(
    key: tuple[Any, ...] | None, mode: str, exc: Any, *, elapsed_ms: int, state: str | None = None
) -> None:
    """Store a refusal (``too_large`` / ``failed`` / ``timeout``) so it is not computed again."""
    if key is None:
        return
    from app.ontology import entailment

    if state is None:
        state = FAILED if isinstance(exc, entailment.EntailmentFailed) else TOO_LARGE
    detail = dict(getattr(exc, "detail", {}) or {})
    try:
        data_path, _ = _paths(key, mode)
        if data_path.is_file():
            data_path.unlink()
        record = _record(
            key,
            mode,
            state,
            stated=detail.get("statements"),
            inferred=detail.get("inferred"),
            elapsed_ms=elapsed_ms,
            findings=[],
            findings_count=0,
            same_as_pairs=detail.get("same_as_pairs", 0),
            message=str(exc),
            detail={k: v for k, v in detail.items() if k not in ("code", "message")},
        )
        _write_record(key, mode, record)
    except OSError:
        logger.warning("could not store the %s refusal of %s", mode, key[0], exc_info=True)


def save_timeout(key: tuple[Any, ...], mode: str, *, limit_seconds: float) -> None:
    """Record that the build of ``key`` / ``mode`` ran out of time."""
    from app.ontology import entailment

    setting = "ontology_entailment_build_timeout_seconds"
    exc = entailment.EntailmentTooLarge(
        f"{entailment.MODE_LABELS[mode]} of this ontology did not finish within {limit_seconds:g} s "
        f"(App Settings: {setting}).",
        reason="build_timeout",
        limit_seconds=limit_seconds,
        setting=setting,
    )
    save_refusal(key, mode, exc, elapsed_ms=int(limit_seconds * 1000), state=TIMEOUT)


# ---- Reading ------------------------------------------------------------------------


def refusal(record: dict[str, Any]) -> Any:
    """The error a stored refusal stands for (raised to the search as before)."""
    from app.ontology import entailment

    message = str(record.get("message") or "The entailment of this ontology was refused.")
    detail = dict(record.get("detail") or {})
    if record.get("state") == FAILED:
        return entailment.EntailmentFailed(message, **detail)
    return entailment.EntailmentTooLarge(message, **detail)


def load(key: tuple[Any, ...] | None, mode: str, record: dict[str, Any]) -> EntailmentComputation:
    """The stored inferred statements and findings of a ``ready`` record."""
    import pyoxigraph as ox

    from app.ontology.store import EntailmentComputation

    data_path, _ = _paths(key, mode)  # type: ignore[arg-type]
    raw = gzip.decompress(data_path.read_bytes())
    merge_inferred: list[Any] = []
    per_graph: dict[Any, list[Any]] = {}
    if raw:
        for quad in ox.parse(raw, format=ox.RdfFormat.N_QUADS):
            if isinstance(quad.graph_name, ox.DefaultGraph):
                merge_inferred.append(quad.triple)
            else:
                per_graph.setdefault(quad.graph_name, []).append(quad.triple)
    return EntailmentComputation(
        merge_inferred=merge_inferred,
        per_graph=per_graph,
        findings=[decode_finding(item) for item in record.get("findings") or []],
        same_as=int(record.get("same_as_pairs") or 0),
        count=int(record.get("inferred") or len(merge_inferred)),
        stated=int(record.get("stated") or 0),
    )


def records(ontology_id: str) -> list[dict[str, Any]]:
    """Every stored record of one ontology (any key, mode or engine)."""
    try:
        base = folder(ontology_id)
    except ValueError:
        return []
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            out.append({**record, "_file": path.name})
    return out


# ---- Retention ----------------------------------------------------------------------


def prune(ontology_id: str, revision: str, signature: list[list[str]] | None) -> int:
    """Delete the results of other revisions or import signatures; returns files removed.

    A result computed without imports (signature None) is kept for the current revision;
    one with imports only when its signature is the current one.
    """
    removed = 0
    for record in records(ontology_id):
        imports = record.get("imports")
        if record.get("revision") == revision and (imports is None or imports == signature):
            continue
        stem = record["_file"].removesuffix(".json")
        for name in (f"{stem}.json", f"{stem}.nq.gz", f"{stem}.json.gz"):
            path = folder(ontology_id) / name
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                logger.debug("could not remove %s", path, exc_info=True)
    return removed


def remove(ontology_id: str) -> None:
    """Delete every stored result of one ontology (it was deleted)."""
    try:
        base = folder(ontology_id)
    except ValueError:
        return
    shutil.rmtree(base, ignore_errors=True)


def current_signature(ontology_id: str) -> list[list[str]] | None:
    """The import signature the ontology's closure has now (None without imports)."""
    from app.ontology.imports import compute_closure

    loaded = compute_closure(ontology_id).loaded
    return [[item.ontology_id, item.revision] for item in loaded] or None


# ---- Building (runs in a child process of the Pipeline Job, UDR-0193 D7) -------------


def build(ontology_id: str, scope: Any, imports: bool | None, mode: str) -> dict[str, Any]:
    """Compute and store ``mode`` for the ontology's CURRENT version; returns a summary.

    A result already stored by the current engine is kept as it is. Refusals are stored
    as their state. Afterwards the results of older versions are pruned.
    """
    from app.ontology import entailment, store

    graph = store.load_store(ontology_id, scope, imports)
    key = graph.key
    assert key is not None
    record = lookup(key, mode)
    if record is None:
        started = time.monotonic()
        try:
            result = store.compute_entailment(graph, mode)
        except (entailment.EntailmentTooLarge, entailment.EntailmentFailed) as exc:
            save_refusal(key, mode, exc, elapsed_ms=int((time.monotonic() - started) * 1000))
        else:
            save_ready(key, mode, result, elapsed_ms=int((time.monotonic() - started) * 1000), graph=graph)
        record = read_record(key, mode) or {}
    try:
        prune(ontology_id, key[1], current_signature(ontology_id))
    except Exception:
        logger.debug("pruning the stored results of %s failed", ontology_id, exc_info=True)
    return {
        "mode": mode,
        "state": record.get("state"),
        "inferred": record.get("inferred"),
        "stated": record.get("stated"),
        "elapsed_ms": record.get("elapsed_ms"),
        "findings_count": record.get("findings_count"),
        "key": key_hash(key),
    }


def build_from_job(params: dict[str, Any]) -> dict[str, Any]:
    """Child-process entry of the ``ontology-entailment`` job: one mode (``params["mode"]``)."""
    from app.ontology import store

    scope = store.parse_scope(params.get("scope"))
    return build(params["ontology_id"], scope, params.get("imports"), params["mode"])


__all__ = [
    "FAILED",
    "READY",
    "RESULTS_DIRNAME",
    "STATES",
    "TIMEOUT",
    "TOO_LARGE",
    "build",
    "build_from_job",
    "current_signature",
    "decode_finding",
    "encode_finding",
    "finding_key",
    "folder",
    "key_fields",
    "key_hash",
    "load",
    "lookup",
    "parse_term",
    "prune",
    "read_record",
    "records",
    "refusal",
    "refusal_stale",
    "remove",
    "save_ready",
    "save_refusal",
    "save_timeout",
    "scope_text",
]
