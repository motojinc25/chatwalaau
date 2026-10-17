"""Stored SHACL validation results, one per data and shapes version (CTR-0170 v10, PRP-0212, UDR-0194 D8).

Kept beside the stored entailment results, in the results folder (``ONTOLOGY_RESULTS_DIR``):

    <results dir>/<ontology id>/<key hash>-shacl.json      the record (counts, groups, state)
    <results dir>/<ontology id>/<key hash>-shacl.json.gz   every result kept (up to the bound)

The key is the data key -- the standard query key (id, revision, kind, scope ``all``,
imports signature) -- plus every shapes source with the revision of the file it is in,
the inference mode and the engine id, so a result answers for exactly the versions it
was computed from. States: ``ready``, ``too_large``, ``failed``, ``timeout``,
``shapes_error``, ``entailment_unavailable``. Only the current key of an ontology is
kept; deleting the ontology deletes its folder. A refusal goes stale when the bound or
the time limit that caused it is raised. This is a cache: deleting it is always safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
import gzip
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

RECORD_VERSION = 1
SUFFIX = "-shacl"
READY = "ready"
TOO_LARGE = "too_large"
FAILED = "failed"
TIMEOUT = "timeout"
SHAPES_ERROR = "shapes_error"
ENTAILMENT_UNAVAILABLE = "entailment_unavailable"
STATES = (READY, TOO_LARGE, FAILED, TIMEOUT, SHAPES_ERROR, ENTAILMENT_UNAVAILABLE)
# Violations a strict-mode refusal lists (UDR-0194 D13).
STRICT_LISTED = 20


# ---- Keys ---------------------------------------------------------------------------


def key_for(
    ontology_id: str,
    config: dict[str, Any],
    *,
    data: bytes | None = None,
    dataset: bool | None = None,
) -> dict[str, Any]:
    """The key of the ontology's current version (``data`` = a candidate text instead).

    Raises ``KeyError`` for an unknown ontology.
    """
    from app.ontology import entailment_results as results
    from app.ontology import shacl, store

    query_key = store.query_key(ontology_id, "all", None)
    fields = results.key_fields(query_key)
    if data is not None:
        fields["revision"] = store.revision_of(data)
        if dataset is not None:
            fields["dataset"] = bool(dataset)
    revision = fields["revision"]
    sources: list[list[Any]] = [["graph", iri, revision] for iri in config.get("shapes_graphs") or []]
    for shapes_id in config.get("shapes_ontologies") or []:
        shapes_bytes = store.read_ontology_bytes(shapes_id)
        sources.append(["ontology", shapes_id, None if shapes_bytes is None else store.revision_of(shapes_bytes)])
    return {
        **fields,
        "shapes": sources,
        "inference": config.get("inference") or "none",
        "engine": shacl.ENGINE_ID,
    }


def key_hash(key: dict[str, Any]) -> str:
    text = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _paths(key: dict[str, Any]) -> tuple[Path, Path]:
    from app.ontology import entailment_results as results

    base = results.folder(key["ontology_id"]) / f"{key_hash(key)}{SUFFIX}"
    return base.with_name(base.name + ".json"), base.with_name(base.name + ".json.gz")


# ---- Reading ------------------------------------------------------------------------


def read_record(key: dict[str, Any] | None) -> dict[str, Any] | None:
    if key is None:
        return None
    try:
        record_path, _ = _paths(key)
        if not record_path.is_file():
            return None
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("state") in STATES else None


def refusal_stale(record: dict[str, Any]) -> bool:
    """True when the refusal would not happen with the CURRENT App Settings."""
    from app.core.config import settings
    from app.ontology import entailment_results as results

    detail = record.get("detail") or {}
    state = record.get("state")
    try:
        if state == TOO_LARGE:
            return int(settings.ontology_shacl_max_triples) >= int(detail.get("statements") or 0)
        if state == TIMEOUT:
            return int(settings.ontology_shacl_build_timeout_seconds) > float(detail.get("limit_seconds") or 0)
        if state == ENTAILMENT_UNAVAILABLE:
            return results.refusal_stale(detail.get("entailment"), str(detail.get("mode") or ""))
    except (TypeError, ValueError):
        return False
    return False


def lookup(key: dict[str, Any] | None) -> dict[str, Any] | None:
    """The stored record of ``key`` (a stale refusal = None)."""
    record = read_record(key)
    if record is None:
        return None
    if record.get("state") != READY and refusal_stale(record):
        return None
    if record.get("state") == READY and not _paths(key)[1].is_file():  # type: ignore[arg-type]
        return None
    return record


def load_results(key: dict[str, Any]) -> list[dict[str, Any]]:
    """Every result kept for a ``ready`` record (sorted: severity, shape, path, focus)."""
    _, results_path = _paths(key)
    try:
        items = json.loads(gzip.decompress(results_path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError):
        return []
    return items if isinstance(items, list) else []


# ---- Writing ------------------------------------------------------------------------


def _atomic(path: Path, data: bytes) -> None:
    from app.ontology.entailment_results import _atomic_write

    _atomic_write(path, data)


def _record(key: dict[str, Any], state: str, **fields: Any) -> dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "mode": "shacl",
        **key,
        "key": key_hash(key),
        "state": state,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **fields,
    }


def _write(key: dict[str, Any], record: dict[str, Any], results: list[dict[str, Any]] | None) -> None:
    record_path, results_path = _paths(key)
    try:
        if results is not None:
            _atomic(results_path, gzip.compress(json.dumps(results, ensure_ascii=False).encode("utf-8"), 6))
        elif results_path.is_file():
            results_path.unlink()
        _atomic(record_path, json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"))
    except OSError:
        logger.warning("could not store the SHACL result of %s", key.get("ontology_id"), exc_info=True)


def save_timeout(key: dict[str, Any], *, limit_seconds: float) -> None:
    setting = "ontology_shacl_build_timeout_seconds"
    record = _record(
        key,
        TIMEOUT,
        message=f"SHACL validation did not finish within {limit_seconds:g} s (App Settings: {setting}).",
        detail={"reason": "build_timeout", "limit_seconds": limit_seconds, "setting": setting},
        elapsed_ms={"total": int(limit_seconds * 1000)},
    )
    _write(key, record, None)


def prune(ontology_id: str, keep: str | None) -> int:
    """Delete the SHACL results of the ontology except the key hash ``keep``."""
    from app.ontology import entailment_results as results

    try:
        base = results.folder(ontology_id)
    except ValueError:
        return 0
    if not base.is_dir():
        return 0
    removed = 0
    for path in [*base.glob(f"*{SUFFIX}.json"), *base.glob(f"*{SUFFIX}.json.gz")]:
        if keep is not None and path.name.startswith(f"{keep}{SUFFIX}."):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.debug("could not remove %s", path, exc_info=True)
    return removed


# ---- Computing ----------------------------------------------------------------------


def compute(
    ontology_id: str,
    config: dict[str, Any],
    key: dict[str, Any],
    *,
    data: bytes | None = None,
    entry: dict[str, Any] | None = None,
    stored_entailment: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """Validate and return (record, results); refusals become their state. Nothing stored."""
    from app.core.config import settings
    from app.ontology import shacl

    started = time.monotonic()
    sources: list[dict[str, Any]] = []
    try:
        collected = shacl.collect(ontology_id, config, data=data, entry=entry)
        sources = [source.as_json() for source in collected.sources]
        inference = config.get("inference") or "none"
        inferred = shacl.inferred_statements(collected, inference, stored=stored_entailment)
        outcome = shacl.validate(collected, inferred)
    except shacl.ValidationRefused as exc:
        return (
            _record(
                key,
                exc.state,
                sources=sources,
                message=str(exc),
                detail=dict(exc.detail),
                elapsed_ms={"total": int((time.monotonic() - started) * 1000)},
                unsupported=[],
            ),
            None,
        )
    except KeyError:
        raise
    except Exception as exc:
        logger.warning("SHACL validation of %s failed", ontology_id, exc_info=True)
        return (
            _record(
                key,
                FAILED,
                sources=sources,
                message=f"SHACL validation failed: {type(exc).__name__}: {exc}",
                detail={},
                elapsed_ms={"total": int((time.monotonic() - started) * 1000)},
                unsupported=[],
            ),
            None,
        )
    bound = max(1, int(settings.ontology_shacl_max_results))
    record = _record(
        key,
        READY,
        sources=sources,
        conforms=outcome.conforms,
        counts=outcome.counts,
        results_count=len(outcome.results),
        stored_results=min(bound, len(outcome.results)),
        truncated=len(outcome.results) > bound,
        groups=outcome.groups,
        focus_counts=outcome.focus_counts,
        unsupported=outcome.unsupported,
        data_statements=outcome.data_statements,
        inferred_statements=outcome.inferred_statements,
        skipped_statements=outcome.skipped,
        elapsed_ms={**outcome.elapsed_ms, "total": int((time.monotonic() - started) * 1000)},
        prefixes=collected.prefixes,
        message=None,
        detail={},
    )
    return record, outcome.results[:bound]


def build(ontology_id: str) -> dict[str, Any]:
    """Validate the ontology's CURRENT version and store it (kept when already stored)."""
    from app.ontology import store

    entry = store.get_entry(ontology_id)
    if entry is None:
        raise KeyError(ontology_id)
    config = store.shacl_config(entry)
    if config is None:
        prune(ontology_id, None)
        return {"state": "not_configured"}
    key = key_for(ontology_id, config)
    record = lookup(key)
    if record is None:
        record, results = compute(ontology_id, config, key)
        _write(key, record, results)
    prune(ontology_id, key_hash(key))
    return summary(record)


def build_from_job(params: dict[str, Any]) -> dict[str, Any]:
    """Child-process entry of the ``ontology-shacl`` job."""
    return build(params["ontology_id"])


def candidate(ontology_id: str, text: str, dataset: bool | None) -> dict[str, Any]:
    """Strict mode (UDR-0194 D13): validate a text BEFORE it is written.

    The inference, when the ontology has one, is computed from the candidate itself.
    The result is stored under the candidate's key (the version it becomes when saved).
    Returns ``{checked, state, message, counts, violations: [...]}``.
    """
    from app.ontology import store

    entry = store.get_entry(ontology_id)
    config = store.shacl_config(entry)
    if entry is None or config is None:
        return {"checked": False, "state": "not_configured", "reason": "not_configured"}
    data = text.encode("utf-8")
    key = key_for(ontology_id, config, data=data, dataset=dataset)
    record = lookup(key)
    results: list[dict[str, Any]] | None
    if record is None:
        candidate_entry = dict(entry)
        if dataset is not None:
            candidate_entry["file"] = f"{ontology_id}{store.TRIG_SUFFIX if dataset else store.TURTLE_SUFFIX}"
        record, results = compute(ontology_id, config, key, data=data, entry=candidate_entry, stored_entailment=False)
        _write(key, record, results)
    else:
        results = load_results(key) if record.get("state") == READY else None
    if record.get("state") != READY:
        return {
            "checked": False,
            "state": record.get("state"),
            "reason": record.get("state"),
            "message": record.get("message"),
        }
    violations = [item for item in results or [] if item["severity"] == "violation"]
    return {
        "checked": True,
        "state": READY,
        "counts": record.get("counts"),
        "violations": violations[:STRICT_LISTED],
        "violation_count": int((record.get("counts") or {}).get("violation", 0)),
    }


def summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": record.get("state"),
        "conforms": record.get("conforms"),
        "counts": record.get("counts"),
        "elapsed_ms": (record.get("elapsed_ms") or {}).get("total"),
        "key": record.get("key"),
        "message": record.get("message"),
    }


__all__ = [
    "ENTAILMENT_UNAVAILABLE",
    "FAILED",
    "READY",
    "SHAPES_ERROR",
    "STATES",
    "TIMEOUT",
    "TOO_LARGE",
    "build",
    "build_from_job",
    "candidate",
    "compute",
    "key_for",
    "key_hash",
    "load_results",
    "lookup",
    "prune",
    "read_record",
    "refusal_stale",
    "save_timeout",
    "summary",
]
