"""owl:imports resolved from the catalog (CTR-0170 v6, PRP-0207, UDR-0189).

An import IRI is NEVER dereferenced (D1): nothing in this module opens a network
connection. It resolves, by the first rule that answers (D2), to

    1. the mapping table          ``ONTOLOGY_DIR/import-map.json`` (D3)  -> ``map``
    2. a built-in W3C vocabulary  rdf, rdfs, owl, xsd (nothing merged)   -> ``builtin``
    3. a catalog IRI              ``https://chatwalaau.com/ontology/{id}`` -> ``catalog``
    4. the ONLY catalog ontology declaring it as its ontology IRI        -> ``ontology_iri``
    5. the ONLY catalog ontology declaring it as its version IRI         -> ``version_iri``

Rules 4 and 5 compare IRIs exactly; two or more candidates are ``ambiguous`` and
merge nothing. The closure (D4) follows every ``owl:imports`` statement with an IRI
object, in any graph, breadth-first; an ontology already in the closure is not loaded
again (``cycle``), a self-import is ``self``, and the App Settings bounds
``ontology_imports_max_ontologies`` / ``ontology_imports_max_mb`` stop further loads
(``over_limit``) while keeping what was loaded.

The header index (ontology IRIs, version IRIs, imports of each file) is computed by
parsing a file once and kept in memory per (id, file, modification time, size): a
save replaces the file atomically, so the next lookup re-reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading
from typing import Any

from app.core.config import settings
from app.ontology import store

logger = logging.getLogger(__name__)

OWL = "http://www.w3.org/2002/07/owl#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
OWL_ONTOLOGY = f"{OWL}Ontology"
OWL_IMPORTS = f"{OWL}imports"
OWL_VERSION_IRI = f"{OWL}versionIRI"
CATALOG_IRI_PREFIX = "https://chatwalaau.com/ontology/"

# The W3C vocabularies an import may name without a catalog entry (D2 rule 2). Their
# terms are already known to the editor and the role table; nothing is merged.
BUILTIN_VOCABULARIES = {
    "http://www.w3.org/1999/02/22-rdf-syntax-ns": "rdf",
    "http://www.w3.org/2000/01/rdf-schema": "rdfs",
    "http://www.w3.org/2002/07/owl": "owl",
    "http://www.w3.org/2001/XMLSchema": "xsd",
}

MAP_FILE = "import-map.json"
MAP_SCHEMA_VERSION = 1
_MAP_LOCK = threading.RLock()

STATUSES = ("resolved", "builtin", "missing", "ambiguous", "cycle", "self", "over_limit")
MATCHES = ("map", "builtin", "catalog", "ontology_iri", "version_iri")


def catalog_iri(ontology_id: str) -> str:
    """The IRI every catalog ontology can be imported by (D2 rule 3)."""
    return f"{CATALOG_IRI_PREFIX}{ontology_id}"


def builtin_name(iri: str) -> str | None:
    """The built-in vocabulary an IRI names (``#`` / ``/`` folded for these only), else None."""
    return BUILTIN_VOCABULARIES.get(iri.rstrip("#/"))


# ---- Header index ------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    """What resolution needs from one file: its own IRIs and its imports."""

    ontology_iris: tuple[str, ...] = ()
    version_iris: tuple[str, ...] = ()
    # (import IRI, subject Term JSON, graph Term JSON or None), file order, unique.
    imports: tuple[tuple[str, dict[str, Any], dict[str, Any] | None], ...] = ()


_header_cache: dict[str, tuple[tuple[Any, ...], Header]] = {}
_header_lock = threading.Lock()


def header_of_bytes(data: bytes) -> Header:
    """Parse a stored file (Turtle or TriG) and read its header; an unparsable file has none."""
    import pyoxigraph as ox

    from app.ontology.vocabulary import term_to_json

    ontology_iris: dict[str, None] = {}
    version_iris: dict[str, None] = {}
    imports: dict[tuple[str, str, str], tuple[str, dict[str, Any], dict[str, Any] | None]] = {}
    if not data.strip():
        return Header()
    try:
        for quad in ox.parse(data, format=ox.RdfFormat.TRIG):
            predicate = quad.predicate.value
            obj = quad.object
            if not isinstance(obj, ox.NamedNode):
                continue
            if predicate == RDF_TYPE and obj.value == OWL_ONTOLOGY:
                if isinstance(quad.subject, ox.NamedNode):
                    ontology_iris.setdefault(quad.subject.value, None)
            elif predicate == OWL_VERSION_IRI:
                version_iris.setdefault(obj.value, None)
            elif predicate == OWL_IMPORTS:
                graph = None if isinstance(quad.graph_name, ox.DefaultGraph) else term_to_json(quad.graph_name)
                key = (obj.value, str(quad.subject), str(quad.graph_name))
                imports.setdefault(key, (obj.value, term_to_json(quad.subject), graph))
    except (SyntaxError, ValueError):
        logger.warning("ontology header: file could not be parsed", exc_info=True)
        return Header()
    return Header(tuple(ontology_iris), tuple(version_iris), tuple(imports.values()))


def header_of(entry: dict[str, str]) -> Header:
    """The header of a catalog entry, from memory while its file is unchanged."""
    path = store.ontology_dir() / entry["file"]
    try:
        stat = path.stat()
    except OSError:
        return Header()
    key = (entry["file"], stat.st_mtime_ns, stat.st_size)
    with _header_lock:
        hit = _header_cache.get(entry["id"])
        if hit is not None and hit[0] == key:
            return hit[1]
    try:
        data = path.read_bytes()
    except OSError:
        return Header()
    header = header_of_bytes(data)
    with _header_lock:
        _header_cache[entry["id"]] = (key, header)
    return header


def clear_header_cache() -> None:
    with _header_lock:
        _header_cache.clear()


# ---- Mapping table (D3) ---------------------------------------------------------


def _map_path() -> Any:
    return store.ontology_dir() / MAP_FILE


def read_import_map() -> dict[str, str]:
    """IRI -> ontology id. Tolerant: an unreadable or malformed table reads as empty."""
    with _MAP_LOCK:
        path = _map_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("ontology import map unreadable; treated as empty", exc_info=True)
            return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    out: dict[str, str] = {}
    for item in entries if isinstance(entries, list) else []:
        if isinstance(item, dict) and isinstance(item.get("iri"), str) and isinstance(item.get("ontology_id"), str):
            out.setdefault(item["iri"], item["ontology_id"])
    return out


def write_import_map(entries: list[dict[str, str]]) -> None:
    """Replace the table (the previous one kept as ``import-map.json.bak``, then an atomic write).

    Callers validate the entries. The single ``.bak`` never matches the ontology backup
    pattern ``<id>.<ext>.bak-<timestamp>`` that history and the trash read.
    """
    with _MAP_LOCK:
        path = _map_path()
        if path.is_file():
            (path.parent / f"{MAP_FILE}.bak").write_bytes(path.read_bytes())
        payload = {"schema_version": MAP_SCHEMA_VERSION, "entries": entries}
        store._atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


# ---- Resolution (D2) ------------------------------------------------------------


@dataclass
class Resolution:
    status: str  # resolved | builtin | missing | ambiguous
    match: str | None = None  # map | builtin | catalog | ontology_iri | version_iri
    ontology_id: str | None = None
    candidates: list[str] = field(default_factory=list)
    mapped_missing: bool = False


class Resolver:
    """Resolves import IRIs against one snapshot of the catalog, its headers and the map."""

    def __init__(self) -> None:
        self.entries = {e["id"]: e for e in store.read_catalog()}
        self.mapping = read_import_map()
        self._headers: dict[str, Header] = {}
        self._by_ontology_iri: dict[str, list[str]] | None = None
        self._by_version_iri: dict[str, list[str]] | None = None

    def header(self, ontology_id: str) -> Header:
        header = self._headers.get(ontology_id)
        if header is None:
            entry = self.entries.get(ontology_id)
            header = header_of(entry) if entry else Header()
            self._headers[ontology_id] = header
        return header

    def _index(self) -> None:
        by_iri: dict[str, list[str]] = {}
        by_version: dict[str, list[str]] = {}
        for ontology_id in self.entries:
            header = self.header(ontology_id)
            for iri in header.ontology_iris:
                by_iri.setdefault(iri, []).append(ontology_id)
            for iri in header.version_iris:
                by_version.setdefault(iri, []).append(ontology_id)
        self._by_ontology_iri, self._by_version_iri = by_iri, by_version

    def resolve(self, iri: str) -> Resolution:
        mapped = self.mapping.get(iri)
        if mapped is not None:
            if mapped in self.entries:
                return Resolution("resolved", "map", mapped)
            return Resolution("missing", "map", mapped_missing=True)
        if builtin_name(iri) is not None:
            return Resolution("builtin", "builtin")
        if iri.startswith(CATALOG_IRI_PREFIX):
            candidate = iri[len(CATALOG_IRI_PREFIX) :]
            if candidate in self.entries:
                return Resolution("resolved", "catalog", candidate)
        if self._by_ontology_iri is None:
            self._index()
        assert self._by_ontology_iri is not None and self._by_version_iri is not None
        for match, table in (("ontology_iri", self._by_ontology_iri), ("version_iri", self._by_version_iri)):
            found = sorted(set(table.get(iri, [])))
            if len(found) == 1:
                return Resolution("resolved", match, found[0])
            if len(found) > 1:
                return Resolution("ambiguous", match, candidates=found)
        return Resolution("missing")


# ---- Closure (D4) ---------------------------------------------------------------


@dataclass
class Loaded:
    """One imported ontology in the closure, with the bytes it was read from."""

    ontology_id: str
    iri: str  # the import IRI it was reached by (first time)
    revision: str
    data: bytes
    dataset: bool


@dataclass
class Closure:
    ontology_id: str
    items: list[dict[str, Any]] = field(default_factory=list)
    loaded: list[Loaded] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def signature(self) -> tuple[tuple[str, str], ...] | None:
        """(id, revision) of every loaded ontology in load order; None when nothing is loaded."""
        return tuple((item.ontology_id, item.revision) for item in self.loaded) or None


def compute_closure(ontology_id: str, *, resolver: Resolver | None = None) -> Closure:
    """The import closure of one catalog ontology (breadth-first, cycles cut, bounded)."""
    resolver = resolver or Resolver()
    closure = Closure(ontology_id)
    max_ontologies = max(1, int(settings.ontology_imports_max_ontologies))
    max_bytes = max(1, int(settings.ontology_imports_max_mb)) * 2**20
    total = 0
    in_closure = {ontology_id}
    # (importer id, importer's IRI for display, depth)
    queue: list[tuple[str, str | None, int]] = [(ontology_id, None, 0)]
    stopped = False
    while queue:
        importer, importer_iri, depth = queue.pop(0)
        for iri, subject, graph in resolver.header(importer).imports:
            item: dict[str, Any] = {
                "iri": iri,
                "subject": subject,
                "graph": graph,
                "parent": importer_iri,
                "parent_id": importer,
                "direct": depth == 0,
                "status": "missing",
                "match": None,
                "ontology": None,
                "candidates": [],
                "statements": None,
            }
            closure.items.append(item)
            found = resolver.resolve(iri)
            item["match"] = found.match
            item["candidates"] = [
                {"id": c, "name": resolver.entries[c]["name"]} for c in found.candidates if c in resolver.entries
            ]
            if found.status != "resolved" or found.ontology_id is None:
                item["status"] = found.status
                continue
            target = found.ontology_id
            item["ontology"] = {"id": target, "name": resolver.entries[target]["name"]}
            if target == ontology_id:
                item["status"] = "self" if importer == ontology_id else "cycle"
                continue
            if target in in_closure:
                item["status"] = "cycle"
                continue
            if stopped or len(closure.loaded) >= max_ontologies:
                item["status"] = "over_limit"
                stopped = True
                continue
            pair = store.read_entry_and_bytes(target)
            if pair is None:
                item["status"] = "missing"
                continue
            entry, data = pair
            if total + len(data) > max_bytes:
                item["status"] = "over_limit"
                stopped = True
                continue
            total += len(data)
            in_closure.add(target)
            item["status"] = "resolved"
            closure.loaded.append(Loaded(target, iri, store.revision_of(data), data, store.is_dataset(entry)))
            queue.append((target, iri, depth + 1))
    over = sum(1 for item in closure.items if item["status"] == "over_limit")
    if over:
        closure.notices.append(
            f"{over} import(s) were not loaded because the import closure reached its limit "
            f"({max_ontologies} ontologies, {max_bytes // 2**20} MiB in total)."
        )
    missing = [item["iri"] for item in closure.items if item["status"] in ("missing", "ambiguous")]
    if missing:
        closure.notices.append(
            "Not in the catalog (or ambiguous), so not included: " + ", ".join(f"<{iri}>" for iri in missing) + "."
        )
    return closure


def importers_of(ontology_id: str) -> list[dict[str, Any]]:
    """The catalog ontologies whose DIRECT imports resolve to ``ontology_id``."""
    resolver = Resolver()
    out: list[dict[str, Any]] = []
    for other_id, entry in resolver.entries.items():
        if other_id == ontology_id:
            continue
        for iri, _subject, _graph in resolver.header(other_id).imports:
            found = resolver.resolve(iri)
            if found.status == "resolved" and found.ontology_id == ontology_id:
                out.append({"id": other_id, "name": entry["name"], "iri": iri})
                break
    return out


def direct_import_names(ontology_id: str, resolver: Resolver) -> list[str]:
    """Names (or IRIs) of the direct imports, for the agent tool's catalog summary (D10)."""
    names: list[str] = []
    for iri, _subject, _graph in resolver.header(ontology_id).imports:
        found = resolver.resolve(iri)
        if found.status == "resolved" and found.ontology_id and found.ontology_id != ontology_id:
            names.append(resolver.entries[found.ontology_id]["name"])
        elif found.status == "builtin":
            names.append(builtin_name(iri) or iri)
        else:
            names.append(f"<{iri}> ({found.status})")
    return list(dict.fromkeys(names))


# ---- Imported vocabulary (D6) ------------------------------------------------------

_VOCABULARY_TYPES = {
    "http://www.w3.org/2002/07/owl#AnnotationProperty",
    "http://www.w3.org/2000/01/rdf-schema#Datatype",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
    "http://www.w3.org/2000/01/rdf-schema#Class",
    "http://www.w3.org/2002/07/owl#Class",
}


def vocabulary_of(closure: Closure) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Classes, properties, datatypes and annotation properties of the loaded ontologies.

    Each resource carries its statements (as subject), its role within its own file and
    its ``source`` (catalog id). Individuals are not included. Also returns the
    statement count per loaded ontology.
    """
    from app.ontology.vocabulary import RDF_TYPE as TYPE
    from app.ontology.vocabulary import turtle_to_projection

    out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for loaded in closure.loaded:
        try:
            projection = turtle_to_projection(loaded.data)
        except ValueError:
            counts[loaded.ontology_id] = 0
            continue
        counts[loaded.ontology_id] = int(projection.get("quad_count") or 0)
        for resource in projection["resources"]:
            if resource["term"]["type"] != "iri":
                continue
            types = {s["o"]["value"] for s in resource["statements"] if s["p"] == TYPE and s["o"]["type"] == "iri"}
            if resource["role"] == "other" and not (types & _VOCABULARY_TYPES):
                continue
            out.append({**resource, "source": loaded.ontology_id})
    return out, counts


__all__ = [
    "BUILTIN_VOCABULARIES",
    "CATALOG_IRI_PREFIX",
    "Closure",
    "Header",
    "Loaded",
    "Resolver",
    "builtin_name",
    "catalog_iri",
    "clear_header_cache",
    "compute_closure",
    "direct_import_names",
    "header_of",
    "header_of_bytes",
    "importers_of",
    "read_import_map",
    "vocabulary_of",
    "write_import_map",
]
