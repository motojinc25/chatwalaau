"""Ontology store and catalog (CTR-0170, PRP-0105, UDR-0084 D3/D7/D10).

File layout under ``ONTOLOGY_DIR`` (created on demand):

    catalog.json              -- the catalog SSOT: [{id, name, description, file,
                                 created_at, updated_at}]
    <id>.ttl                  -- ONE self-contained Turtle file per ontology (SSOT)
    <id>.ttl.bak-<timestamp>  -- automatic backup on every save / pre-delete

The catalog reader is tolerant and self-healing (per-entry normalization +
write-back; an unparseable file is backed up to ``catalog.corrupt-<ts>.json``
and the catalog restarts empty -- the CTR-0015 folder-index precedent). Every
save writes a timestamped backup then a temp file + atomic ``os.replace``
(the CTR-0166 / UDR-0080 D3 convention); delete is backup-then-remove.

Query execution is READ-ONLY by construction: the ONE executor runs
``pyoxigraph.Store.query()`` (SELECT / CONSTRUCT / ASK / DESCRIBE), which is
structurally incapable of mutating; SPARQL UPDATE strings fail its parser.
Filenames are catalog-derived single segments, so no path outside ONTOLOGY_DIR
is ever resolved (the CTR-0022 confinement precedent).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)

_CATALOG_NAME = "catalog.json"
_ID_RE = re.compile(r"^ont_[0-9a-f]{12}$")

# Result-shape caps for the read-only executor (payload bound, not a security
# boundary -- CTR-0083 gates the callers).
SELECT_MAX_ROWS = 1000


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
    tmp.write_text(content, encoding="utf-8")
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
    if Path(file_name).name != file_name or not file_name.endswith(".ttl"):
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
    _atomic_write_text(_catalog_path(), json.dumps({"ontologies": entries}, ensure_ascii=False, indent=2))


def get_entry(ontology_id: str) -> dict[str, str] | None:
    for entry in read_catalog():
        if entry["id"] == ontology_id:
            return entry
    return None


def _touch_entry(ontology_id: str) -> None:
    entries = read_catalog()
    for entry in entries:
        if entry["id"] == ontology_id:
            entry["updated_at"] = _now()
    write_catalog(entries)


# ---- Ontology file lifecycle -------------------------------------------------


def _file_path(entry: dict[str, str]) -> Path:
    return ontology_dir() / entry["file"]


def create_ontology(name: str, description: str, *, initial_turtle: str = "") -> dict[str, str]:
    """Create a new catalog entry + its Turtle file (empty unless imported)."""
    entry_id = f"ont_{uuid.uuid4().hex[:12]}"
    entry = {
        "id": entry_id,
        "name": (name or "").strip() or entry_id,
        "description": (description or "").strip(),
        "file": f"{entry_id}.ttl",
        "created_at": _now(),
        "updated_at": _now(),
    }
    _atomic_write_text(_file_path(entry), initial_turtle)
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
    entry = get_entry(ontology_id)
    if entry is None:
        return False
    path = _file_path(entry)
    _backup(path)
    if path.is_file():
        path.unlink()
    write_catalog([e for e in read_catalog() if e["id"] != ontology_id])
    logger.info("ontology deleted: %s (backup kept)", ontology_id)
    return True


def read_ontology_bytes(ontology_id: str) -> bytes | None:
    entry = get_entry(ontology_id)
    if entry is None:
        return None
    path = _file_path(entry)
    return path.read_bytes() if path.is_file() else b""


def save_ontology_text(ontology_id: str, turtle: str) -> str | None:
    """Guarded save: size cap -> backup -> temp + atomic replace. Returns backup name."""
    entry = get_entry(ontology_id)
    if entry is None:
        raise KeyError(ontology_id)
    encoded = turtle.encode("utf-8")
    if len(encoded) > settings.ontology_max_file_bytes:
        raise ValueError(f"Ontology is {len(encoded)} bytes but the limit is {settings.ontology_max_file_bytes} bytes")
    path = _file_path(entry)
    backup_name = _backup(path)
    _atomic_write_text(path, turtle)
    _touch_entry(ontology_id)
    return backup_name


# ---- pyoxigraph load + read-only query executor (UDR-0084 D3/D7) -------------


def load_store(ontology_id: str) -> Any:
    """Parse the ontology's Turtle file into an IN-MEMORY pyoxigraph Store."""
    import pyoxigraph as ox

    data = read_ontology_bytes(ontology_id)
    if data is None:
        raise KeyError(ontology_id)
    store = ox.Store()
    if data.strip():
        store.load(data, format=ox.RdfFormat.TURTLE)
    return store


def _term_to_str(term: Any) -> str:
    """A display string for a binding term (IRI value, literal value, or _:id)."""
    value = getattr(term, "value", None)
    if value is not None:
        return str(value)
    return str(term) if term is not None else ""


def execute_query(store: Any, sparql: str, *, max_construct_triples: int = 0) -> dict[str, Any]:
    """Run a READ-ONLY SPARQL query and shape the result for CTR-0171 / CTR-0172.

    - SELECT   -> {kind, columns, rows, row_count, truncated, entity_iris}
                  (entity_iris = the IRIs bound anywhere in the results, so the
                  UI can drive the strong/dim canvas highlight -- RESULT-1)
    - CONSTRUCT/DESCRIBE -> {kind, turtle, triple_count, truncated}
    - ASK      -> {kind, value}

    ``Store.query()`` cannot mutate; a SPARQL UPDATE string fails its parser.
    Raises ``ValueError`` with the parser message on an invalid query.
    """
    import pyoxigraph as ox

    try:
        result = store.query(sparql)
    except Exception as exc:  # SyntaxError and friends from the SPARQL parser
        raise ValueError(f"SPARQL error: {exc}") from exc

    if isinstance(result, ox.QuerySolutions):
        columns = [str(v).lstrip("?") for v in result.variables]
        rows: list[list[str]] = []
        entity_iris: set[str] = set()
        truncated = False
        for solution in result:
            if len(rows) >= SELECT_MAX_ROWS:
                truncated = True
                break
            row: list[str] = []
            for variable in result.variables:
                term = solution[variable]
                row.append(_term_to_str(term))
                if isinstance(term, ox.NamedNode):
                    entity_iris.add(term.value)
            rows.append(row)
        return {
            "kind": "select",
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "entity_iris": sorted(entity_iris),
        }

    if isinstance(result, ox.QueryBoolean):
        return {"kind": "ask", "value": bool(result)}

    # Remaining result kind: triples from CONSTRUCT / DESCRIBE.
    triples = list(result)
    truncated = False
    if max_construct_triples and len(triples) > max_construct_triples:
        triples = triples[:max_construct_triples]
        truncated = True
    turtle = ""
    if triples:
        from app.ontology.vocabulary import TURTLE_PREFIXES

        turtle = ox.serialize(triples, format=ox.RdfFormat.TURTLE, prefixes=TURTLE_PREFIXES).decode("utf-8")
    return {
        "kind": "construct",
        "turtle": turtle,
        "triple_count": len(triples),
        "truncated": truncated,
    }


__all__ = [
    "SELECT_MAX_ROWS",
    "create_ontology",
    "delete_ontology",
    "execute_query",
    "get_entry",
    "load_store",
    "ontology_dir",
    "read_catalog",
    "read_ontology_bytes",
    "rename_ontology",
    "save_ontology_text",
    "write_catalog",
]
