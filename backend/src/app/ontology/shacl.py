"""SHACL Core validation of ontology data with pyshacl (CTR-0170 v10, PRP-0212, UDR-0194).

What is validated (UDR-0194 D1-D3, D6, D7):

- **Data**: the ontology's statements in every graph EXCEPT its declared shapes graphs
  (catalog ``shacl.shapes_graphs``), plus its import closure (as searches see it) minus
  the imported ontologies' own declared shapes graphs. Built from the PARSER's terms --
  the written lexical forms, ill-typed literals kept -- never from the query Store,
  which re-encodes typed literals (``"01"^^xsd:integer`` -> ``"1"``); blank nodes get
  the same labels the query Store gives them (``store._StableBlanks``), so focus nodes
  and stored inferred statements name the same nodes.
- **Inference** (``none`` / ``rdfs`` / ``owl-rl``, one per ontology): the inferred
  statements are added from the STORED entailment of the standard key when the data is
  that key's merge (no shapes graph was left out); otherwise the closure of the data is
  computed here. pyshacl's own ``inference`` is never used.
- **Shapes**: the declared shapes graphs and the whole files of the declared shapes
  ontologies. SHACL-SPARQL, SHACL-AF and SHACL-JS constructs are REMOVED before pyshacl
  runs and reported (``unsupported``): SPARQL may hold ``SERVICE`` calls, and nothing
  here may open a network connection (UDR-0189 D1). The shapes are then checked against
  SHACL-for-SHACL; errors are reported instead of validating.

pyshacl runs with ``inference="none"``, ``advanced=False``, ``js=False``,
``do_owl_imports=False``: SHACL Core only, nothing fetched. Results are encoded with
terms as N-Triples and as shown with the ontology's prefixes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

SH = "http://www.w3.org/ns/shacl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XSD = "http://www.w3.org/2001/XMLSchema#"
XSD_STRING = f"{XSD}string"
RDF_TYPE = f"{RDF}type"

PYSHACL_VERSION = "0.40.1"
ENGINE_ID = f"pyshacl-{PYSHACL_VERSION}/core-1"
INFERENCE_MODES = ("none", "rdfs", "owl-rl")

SEVERITIES = ("violation", "warning", "info")
_SEVERITY_IRIS = {f"{SH}Violation": "violation", f"{SH}Warning": "warning", f"{SH}Info": "info"}
SEVERITY_RANK = {"violation": 0, "warning": 1, "info": 2}

# Constructs outside SHACL Core (UDR-0194 D7): statements with these predicates, and every
# statement of a subject typed with these classes, are removed from the shapes.
REMOVED_PREDICATES = frozenset(
    f"{SH}{name}"
    for name in (
        "sparql",
        "select",
        "ask",
        "construct",
        "update",
        "rule",
        "target",
        "condition",
        "validator",
        "nodeValidator",
        "propertyValidator",
        "js",
        "jsFunctionName",
        "jsLibrary",
        "jsLibraryURL",
        "shapesGraph",
        "entailment",
        "values",
    )
)
REMOVED_TYPES = frozenset(
    f"{SH}{name}"
    for name in (
        "SPARQLConstraint",
        "SPARQLTarget",
        "SPARQLTargetType",
        "SPARQLAskValidator",
        "SPARQLSelectValidator",
        "SPARQLFunction",
        "SPARQLRule",
        "SPARQLValuesDeriver",
        "TripleRule",
        "Function",
        "ConstraintComponent",
        "TargetType",
        "JSConstraint",
        "JSFunction",
        "JSLibrary",
        "JSRule",
        "JSTarget",
        "JSTargetType",
        "JSValidator",
    )
)

# Groups and focus nodes listed per group in a record (the Checks pane, UDR-0194 D9).
MAX_GROUP_FOCUS = 50
MAX_GROUPS = 2_000
# Nodes whose result counts a record keeps (the Data view's chips, UDR-0194 D12).
MAX_FOCUS_COUNTS = 5_000
MAX_SHAPE_ERRORS = 50
_BUILTIN_PREFIXES = {"rdf": RDF, "rdfs": RDFS, "owl": OWL, "xsd": XSD, "sh": SH}


class ValidationRefused(Exception):
    """Validation did not run: ``state`` names why (too_large, shapes_error, ...)."""

    def __init__(self, state: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.state = state
        self.detail = detail

    def __reduce__(self) -> Any:
        return (_refused, (self.state, str(self), self.detail))


def _refused(state: str, message: str, detail: dict[str, Any]) -> ValidationRefused:
    return ValidationRefused(state, message, **detail)


# ---- Sources ----------------------------------------------------------------------


@dataclass
class ShapesSource:
    kind: str  # "graph" | "ontology"
    ref: str  # graph IRI | ontology id
    revision: str | None = None
    missing: bool = False
    name: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "revision": self.revision,
            "missing": self.missing,
            "name": self.name,
        }


@dataclass
class Collected:
    """The data and shapes of one validation, in pyoxigraph terms."""

    ontology_id: str
    revision: str
    dataset: bool
    data: dict[Any, None]  # data triples, file order, unique
    shapes: list[Any]  # shapes triples
    sources: list[ShapesSource]
    signature: list[list[str]] | None  # imports (id, revision), as the query key has it
    prefixes: dict[str, str]
    own_terms: set[Any] = field(default_factory=set)
    left_out: int = 0  # data statements left out because they are in a shapes graph
    skipped: int = 0  # statements pyshacl cannot take (triple terms)


def collect(
    ontology_id: str,
    config: dict[str, Any],
    *,
    data: bytes | None = None,
    entry: dict[str, Any] | None = None,
) -> Collected:
    """Read the data and the shapes of ``ontology_id`` (``data`` = a candidate text, strict mode).

    Raises ``KeyError`` for an unknown ontology.
    """
    import pyoxigraph as ox

    from app.ontology import store
    from app.ontology.imports import compute_closure
    from app.ontology.vocabulary import document_of

    if data is None or entry is None:
        pair = store.read_entry_and_bytes(ontology_id)
        if pair is None:
            raise KeyError(ontology_id)
        entry = pair[0] if entry is None else entry
        data = pair[1] if data is None else data
    imported = compute_closure(ontology_id).loaded if store.include_imports(None) else []
    prefix = store._blank_prefix(data, imported)
    shapes_graphs = [ox.NamedNode(iri) for iri in config.get("shapes_graphs") or []]
    shapes_set = set(shapes_graphs)
    collected = Collected(
        ontology_id=ontology_id,
        revision=store.revision_of(data),
        dataset=store.is_dataset(entry),
        data={},
        shapes=[],
        sources=[],
        signature=[[item.ontology_id, item.revision] for item in imported] or None,
        prefixes={},
    )
    present: set[Any] = set()
    prefixes: dict[str, str] = {}
    if data.strip():
        parser = ox.parse(data, format=ox.RdfFormat.TRIG)
        stable = store._StableBlanks(data, f"{prefix}p")
        for parsed in parser:
            quad = stable.quad(parsed)
            graph = quad.graph_name
            if graph in shapes_set:
                present.add(graph)
                collected.shapes.append(quad.triple)
                continue
            triple = quad.triple
            if triple not in collected.data:
                collected.data[triple] = None
                collected.own_terms.add(triple.subject)
                collected.own_terms.add(triple.object)
        for item in document_of(parser, data)["prefixes"]:
            prefixes.setdefault(item["prefix"], item["iri"])
    for graph in shapes_graphs:
        collected.sources.append(ShapesSource("graph", graph.value, collected.revision, missing=graph not in present))
    collected.left_out = len(collected.shapes)
    # Imports: relabelled exactly as the query Store does (store._merge_imports).
    for k, item in enumerate(imported):
        imported_config = store.shacl_config(store.get_entry(item.ontology_id)) or {}
        excluded = {ox.NamedNode(iri) for iri in imported_config.get("shapes_graphs") or []}
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

        stable = store._StableBlanks(item.data, f"{prefix}{k}y")
        try:
            parser = ox.parse(item.data, format=ox.RdfFormat.TRIG)
            for parsed in parser:
                if parsed.graph_name in excluded:
                    collected.left_out += 1
                    continue
                quad = stable.quad(parsed)
                subject, obj = quad.subject, quad.object
                if not isinstance(subject, ox.NamedNode):
                    subject = term(subject)
                if isinstance(obj, ox.BlankNode | ox.Triple):
                    obj = term(obj)
                collected.data.setdefault(ox.Triple(subject, quad.predicate, obj), None)
            for name, iri in (parser.prefixes or {}).items():
                prefixes.setdefault(name, iri)
        except (SyntaxError, ValueError):
            logger.warning("imported ontology %s could not be parsed for validation", item.ontology_id)
    # Shapes ontologies: their whole files, blank nodes kept apart per file.
    for k, shapes_id in enumerate(config.get("shapes_ontologies") or []):
        pair = store.read_entry_and_bytes(shapes_id)
        if pair is None:
            collected.sources.append(ShapesSource("ontology", shapes_id, None, missing=True))
            continue
        shapes_entry, shapes_data = pair
        collected.sources.append(
            ShapesSource("ontology", shapes_id, store.revision_of(shapes_data), name=shapes_entry.get("name"))
        )
        labels: dict[str, Any] = {}

        def blank(t: Any, k: int = k, labels: dict[str, Any] = labels) -> Any:
            if isinstance(t, ox.BlankNode):
                node = labels.get(t.value)
                if node is None:
                    node = labels[t.value] = ox.BlankNode(f"cwshapes{k}x{len(labels)}")
                return node
            return t

        try:
            parser = ox.parse(shapes_data, format=ox.RdfFormat.TRIG)
            for quad in parser:
                if isinstance(quad.subject, ox.Triple) or isinstance(quad.object, ox.Triple):
                    continue
                collected.shapes.append(ox.Triple(blank(quad.subject), quad.predicate, blank(quad.object)))
            for name, iri in (parser.prefixes or {}).items():
                prefixes.setdefault(name, iri)
        except (SyntaxError, ValueError):
            logger.warning("shapes ontology %s could not be parsed", shapes_id)
    for name, iri in _BUILTIN_PREFIXES.items():
        if name not in prefixes and iri not in prefixes.values():
            prefixes[name] = iri
    collected.prefixes = prefixes
    return collected


# ---- Inference ----------------------------------------------------------------------


def inferred_statements(collected: Collected, mode: str, *, stored: bool = True) -> list[Any]:
    """The inferred statements to add for ``mode`` (UDR-0194 D6).

    The stored entailment of the standard key is used when ``stored`` and nothing was
    left out of the data (the data is then exactly that key's merge); its typed
    literals, re-encoded by the query Store, are mapped back to the file's forms when
    one form matches. Otherwise the closure of the data is computed here. A refused
    entailment raises ``ValidationRefused("entailment_unavailable")``.
    """
    from app.ontology import entailment, store
    from app.ontology import entailment_results as results

    if mode == "none":
        return []
    try:
        if stored and collected.left_out == 0:
            key = store.query_key(collected.ontology_id, "all", None)
            if key[1] == collected.revision:
                record = results.lookup(key, mode)
                if record is None:
                    results.build(collected.ontology_id, "all", None, mode)
                    record = results.lookup(key, mode)
                if record is not None and record.get("state") == results.READY:
                    return _restore_literals(collected, results.load(key, mode, record).merge_inferred)
                if record is not None:
                    raise results.refusal(record)
        statements = list(collected.data)
        inferred = entailment.closure(statements, mode=mode)
        if mode == entailment.OWL_RL:
            ratio_limit = entailment.max_inferred_ratio()
            if len(inferred) > ratio_limit * max(1, len(statements)):
                raise entailment.EntailmentTooLarge(
                    f"OWL 2 RL entailment inferred {len(inferred):,} statements from {len(statements):,}; at most "
                    f"{ratio_limit:,} per statement are accepted (App Settings: {entailment.RATIO_SETTING}).",
                    statements=len(statements),
                    inferred=len(inferred),
                    reason="inferred_ratio",
                    setting=entailment.RATIO_SETTING,
                )
        return inferred
    except entailment.EntailmentError as exc:
        detail = {k: v for k, v in (getattr(exc, "detail", {}) or {}).items() if k not in ("code", "message")}
        raise ValidationRefused(
            "entailment_unavailable",
            f"{entailment.MODE_LABELS.get(mode, mode)} was not available for validation: {exc}",
            mode=mode,
            entailment=detail,
        ) from exc


def _restore_literals(collected: Collected, inferred: list[Any]) -> list[Any]:
    import pyoxigraph as ox

    from app.ontology.lexical import is_restorable, normalize

    originals = list({t.object for t in collected.data if is_restorable(t.object) and isinstance(t.object, ox.Literal)})
    if not originals:
        return inferred
    forms: dict[Any, list[Any]] = {}
    for original, normalized in normalize(originals).items():
        forms.setdefault(normalized, []).append(original)
    out = []
    for t in inferred:
        candidates = forms.get(t.object) if isinstance(t.object, ox.Literal) else None
        if candidates and len(candidates) == 1 and candidates[0] != t.object:
            out.append(ox.Triple(t.subject, t.predicate, candidates[0]))
        else:
            out.append(t)
    return out


# ---- Shapes preparation (UDR-0194 D7) ------------------------------------------------


def prepare_shapes(triples: list[Any], prefixes: dict[str, str]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Remove SHACL-SPARQL / SHACL-AF / SHACL-JS constructs; returns (kept, unsupported)."""
    import pyoxigraph as ox

    typed = {
        t.subject for t in triples if t.predicate.value == RDF_TYPE and getattr(t.object, "value", "") in REMOVED_TYPES
    }
    removed: list[Any] = []
    kept: list[Any] = []
    roots: list[tuple[str, Any]] = []
    for t in triples:
        if t.predicate.value in REMOVED_PREDICATES:
            removed.append(t)
            roots.append((f"sh:{t.predicate.value[len(SH) :]}", t.subject))
        elif t.subject in typed:
            removed.append(t)
            if t.predicate.value == RDF_TYPE and getattr(t.object, "value", "") in REMOVED_TYPES:
                roots.append((f"sh:{t.object.value[len(SH) :]}", t.subject))
        else:
            kept.append(t)
    # Report each construct once, where it is attached (not its own inner statements).
    inner = {t.object for t in removed if isinstance(t.object, ox.BlankNode)}
    reported: dict[tuple[str, str], int] = {}
    for name, subject in roots:
        if subject in inner:
            continue
        place = (name, show(subject, prefixes))
        reported[place] = reported.get(place, 0) + 1
    # Blank nodes reached only through removed statements go with them (their subtrees).
    while True:
        referenced = {t.object for t in kept if isinstance(t.object, ox.BlankNode)}
        orphans = {t.object for t in removed if isinstance(t.object, ox.BlankNode) and t.object not in referenced}
        dropping = [t for t in kept if t.subject in orphans]
        if not dropping:
            break
        drop = set(dropping)
        kept = [t for t in kept if t not in drop]
        removed.extend(dropping)
    unsupported = [
        {"construct": name, "shape": subject, "count": count} for (name, subject), count in sorted(reported.items())
    ]
    return kept, unsupported


# ---- Terms --------------------------------------------------------------------------


_BARE_LITERALS = {
    f"{XSD}integer": re.compile(r"[+-]?\d+"),
    f"{XSD}decimal": re.compile(r"[+-]?\d*\.\d+"),
    f"{XSD}boolean": re.compile(r"true|false"),
}


def show(term: Any, prefixes: dict[str, str]) -> str:
    """A term as Checks and the agent show it: ``prefix:local``, ``"v"^^xsd:t``, ``_:b``."""
    import pyoxigraph as ox

    from app.ontology.store import _display

    if isinstance(term, ox.Literal):
        value = json.dumps(term.value, ensure_ascii=False)
        if term.language:
            return f"{value}@{term.language}"
        if term.datatype.value == XSD_STRING:
            return value
        bare = _BARE_LITERALS.get(term.datatype.value)
        if bare is not None and bare.fullmatch(term.value):
            return term.value  # written as Turtle writes it: 1, 0.5, true
        return f"{value}^^{_display(term.datatype, prefixes)}"
    return _display(term, prefixes)


def _ox_from_rdflib(term: Any) -> Any:
    import pyoxigraph as ox
    import rdflib

    if isinstance(term, rdflib.URIRef):
        return ox.NamedNode(str(term))
    if isinstance(term, rdflib.BNode):
        return ox.BlankNode(str(term))
    if isinstance(term, rdflib.Literal):
        if term.language:
            return ox.Literal(str(term), language=term.language)
        if term.datatype is not None:
            return ox.Literal(str(term), datatype=ox.NamedNode(str(term.datatype)))
        return ox.Literal(str(term))
    raise ValueError(f"Unsupported term {term!r}")


class _ToRdflib:
    """pyoxigraph terms -> rdflib terms, lexical forms kept exactly (``normalize=False``)."""

    def __init__(self) -> None:
        import rdflib

        self.rdflib = rdflib
        self.cache: dict[Any, Any] = {}

    def __call__(self, term: Any) -> Any:
        hit = self.cache.get(term)
        if hit is not None:
            return hit
        import pyoxigraph as ox

        rdflib = self.rdflib
        if isinstance(term, ox.NamedNode):
            out = rdflib.URIRef(term.value)
        elif isinstance(term, ox.BlankNode):
            out = rdflib.BNode(term.value)
        elif isinstance(term, ox.Literal):
            if term.language:
                try:
                    out = rdflib.Literal(term.value, lang=term.language, normalize=False)
                except ValueError:
                    out = rdflib.Literal(term.value, normalize=False)
            elif term.datatype.value == XSD_STRING:
                out = rdflib.Literal(term.value, normalize=False)
            else:
                out = rdflib.Literal(term.value, datatype=rdflib.URIRef(term.datatype.value), normalize=False)
        else:
            raise TypeError("triple term")
        self.cache[term] = out
        return out


def _graph(triples: Any, convert: _ToRdflib) -> tuple[Any, int]:
    """An rdflib graph of ``triples``; returns (graph, statements skipped: triple terms)."""
    import rdflib

    graph = rdflib.Graph()
    add = graph.add
    skipped = 0
    for t in triples:
        try:
            add((convert(t.subject), convert(t.predicate), convert(t.object)))
        except TypeError:
            skipped += 1
    return graph, skipped


@contextmanager
def _quiet_rdflib() -> Iterator[None]:
    """rdflib logs a warning per ill-typed literal; the validation reports them instead."""
    term_logger = logging.getLogger("rdflib.term")
    level = term_logger.level
    term_logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        term_logger.setLevel(level)


# ---- Validation ---------------------------------------------------------------------


def _path_text(node: Any, graph: Any, prefixes: dict[str, str], depth: int = 0) -> str:
    import rdflib
    from rdflib.collection import Collection

    sh = rdflib.Namespace(SH)
    if depth > 20:
        return "..."
    if isinstance(node, rdflib.URIRef):
        return show(_ox_from_rdflib(node), prefixes)
    if (node, rdflib.RDF.first, None) in graph:
        return "/".join(_path_text(item, graph, prefixes, depth + 1) for item in Collection(graph, node))
    for predicate, template in (
        (sh.inversePath, "^{}"),
        (sh.zeroOrMorePath, "{}*"),
        (sh.oneOrMorePath, "{}+"),
        (sh.zeroOrOnePath, "{}?"),
    ):
        inner = graph.value(node, predicate)
        if inner is not None:
            text = _path_text(inner, graph, prefixes, depth + 1)
            if not isinstance(inner, rdflib.URIRef):
                text = f"({text})"
            return template.format(text)
    alternatives = graph.value(node, sh.alternativePath)
    if alternatives is not None:
        return (
            "("
            + "|".join(_path_text(item, graph, prefixes, depth + 1) for item in Collection(graph, alternatives))
            + ")"
        )
    return str(node)


def _component_name(iri: str) -> str:
    name = iri[len(SH) :] if iri.startswith(SH) else iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    return name.removesuffix("ConstraintComponent")


def _constraint_text(component: str, shape: Any, shapes: Any, prefixes: dict[str, str]) -> str | None:
    """The constraint's parameter as written in the shape, e.g. ``sh:minCount 1``."""
    import rdflib

    if not component:
        return None
    parameter = component[0].lower() + component[1:]
    if component == "Class":
        parameter = "class"
    elif component == "QualifiedMinCount" or component == "QualifiedMaxCount":
        parameter = "qualified" + component[len("Qualified") :]
    elif component == "Closed":
        return "sh:closed true"
    value = shapes.value(shape, rdflib.URIRef(f"{SH}{parameter}"))
    if value is None:
        return f"sh:{parameter}"
    if (value, rdflib.RDF.first, None) in shapes:
        from rdflib.collection import Collection

        members = [show(_ox_from_rdflib(item), prefixes) for item in list(Collection(shapes, value))[:10]]
        return f"sh:{parameter} ({' '.join(members)})"
    if isinstance(value, rdflib.BNode):
        return f"sh:{parameter} [...]"
    return f"sh:{parameter} {show(_ox_from_rdflib(value), prefixes)}"


def _message(messages: list[dict[str, str | None]]) -> str:
    for wanted in ("en", None):
        for item in messages:
            if (item.get("lang") or None) == wanted or (
                wanted == "en" and str(item.get("lang") or "").startswith("en")
            ):
                return str(item["text"])
    return str(messages[0]["text"]) if messages else ""


def _shape_errors(report: Any, prefixes: dict[str, str]) -> list[str]:
    import rdflib

    sh = rdflib.Namespace(SH)
    out = []
    for result in report.subjects(rdflib.RDF.type, sh.ValidationResult):
        focus = report.value(result, sh.focusNode)
        message = report.value(result, sh.resultMessage)
        path = report.value(result, sh.resultPath)
        where = show(_ox_from_rdflib(focus), prefixes) if focus is not None else "?"
        text = f"{where}: {message}" if message is not None else where
        if isinstance(path, rdflib.URIRef):
            text += f" ({show(_ox_from_rdflib(path), prefixes)})"
        out.append(text)
    return sorted(out)[:MAX_SHAPE_ERRORS]


@dataclass
class Outcome:
    conforms: bool
    counts: dict[str, int]
    results: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    unsupported: list[dict[str, Any]]
    data_statements: int
    inferred_statements: int
    elapsed_ms: dict[str, int]
    skipped: int
    focus_counts: dict[str, dict[str, int]] = field(default_factory=dict)


def _result_key(parts: list[str | None]) -> str:
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()[:24]


def validate(collected: Collected, inferred: list[Any]) -> Outcome:
    """Run pyshacl (Core only) on the collected data and shapes (UDR-0194 D7).

    Raises ``ValidationRefused`` with ``too_large`` (above ``ontology_shacl_max_triples``)
    or ``shapes_error`` (the shapes are not valid SHACL, or pyshacl cannot load them).
    """
    import pyshacl
    from pyshacl.entrypoints import meta_validate
    from pyshacl.errors import ReportableRuntimeError, ShapeLoadError, ValidationFailure
    import rdflib

    from app.core.config import settings
    from app.ontology.vocabulary import term_to_json

    started = time.monotonic()
    data_count = len(collected.data) + len(inferred)
    limit = max(1, int(settings.ontology_shacl_max_triples))
    if data_count > limit:
        raise ValidationRefused(
            "too_large",
            f"The data has {data_count:,} statements (stated and inferred); SHACL validation accepts at most "
            f"{limit:,} (App Settings: ontology_shacl_max_triples).",
            statements=data_count,
            limit=limit,
            reason="input",
            setting="ontology_shacl_max_triples",
        )
    prefixes = collected.prefixes
    shapes_triples, unsupported = prepare_shapes(collected.shapes, prefixes)
    convert = _ToRdflib()
    sh = rdflib.Namespace(SH)
    with _quiet_rdflib():
        shapes_graph, _ = _graph(shapes_triples, convert)
        data_graph, skipped = _graph([*collected.data, *inferred], convert)
        converted = time.monotonic()
        try:
            meta_conforms, meta_report, _ = meta_validate(
                shapes_graph, inference="none", advanced=False, js=False, do_owl_imports=False
            )
        except (ReportableRuntimeError, ShapeLoadError, ValidationFailure, ValueError, TypeError) as exc:
            raise ValidationRefused("shapes_error", f"The shapes could not be read: {exc}", errors=[str(exc)]) from exc
        if not meta_conforms:
            errors = _shape_errors(meta_report, prefixes)
            raise ValidationRefused(
                "shapes_error",
                f"{len(errors)} shape statement(s) are not valid SHACL.",
                errors=errors,
            )
        try:
            conforms, report, _ = pyshacl.validate(
                data_graph,
                shacl_graph=shapes_graph,
                ont_graph=None,
                inference="none",
                abort_on_first=False,
                allow_infos=False,
                allow_warnings=False,
                meta_shacl=False,
                advanced=False,
                js=False,
                debug=False,
                do_owl_imports=False,
                iterate_rules=False,
            )
        except (ReportableRuntimeError, ShapeLoadError, ValidationFailure) as exc:
            raise ValidationRefused(
                "shapes_error", f"The shapes could not be applied: {exc}", errors=[str(exc)]
            ) from exc
        validated = time.monotonic()
        parents: dict[Any, Any] = {}
        for node_shape, property_shape in shapes_graph.subject_objects(sh.property):
            parents.setdefault(property_shape, node_shape)
        imported_focus: dict[Any, bool] = {}
        results: list[dict[str, Any]] = []
        # Results nested under another result (sh:detail, e.g. of sh:node) are part of it.
        nested = set(report.objects(None, sh.detail))
        for node in report.subjects(rdflib.RDF.type, sh.ValidationResult):
            if node in nested:
                continue
            severity_iri = str(report.value(node, sh.resultSeverity) or f"{SH}Violation")
            # A severity of the shapes' own (SHACL allows any IRI) counts as a violation.
            severity = _SEVERITY_IRIS.get(severity_iri, "violation")
            focus_r = report.value(node, sh.focusNode)
            if focus_r is None:
                continue
            focus = _ox_from_rdflib(focus_r)
            source = report.value(node, sh.sourceShape)
            component_iri = str(report.value(node, sh.sourceConstraintComponent) or "")
            component = _component_name(component_iri)
            path_node = report.value(node, sh.resultPath)
            path = None
            if path_node is not None:
                path = _path_text(
                    path_node, shapes_graph if (path_node, None, None) in shapes_graph else report, prefixes
                )
            value_r = report.value(node, sh.value)
            value = _ox_from_rdflib(value_r) if value_r is not None else None
            node_shape = parents.get(source, source) if source is not None else None
            shape = _ox_from_rdflib(node_shape) if node_shape is not None else None
            messages = [
                {"text": str(m), "lang": getattr(m, "language", None)} for m in report.objects(node, sh.resultMessage)
            ]
            if focus not in imported_focus:
                imported_focus[focus] = focus not in collected.own_terms
            shape_nt = str(shape) if shape is not None else None
            value_nt = str(value) if value is not None else None
            results.append(
                {
                    "key": _result_key([component, shape_nt, str(focus), path, value_nt]),
                    "severity": severity,
                    "severity_iri": severity_iri,
                    "focus": {"nt": str(focus), "shown": show(focus, prefixes), "term": term_to_json(focus)},
                    "imported": imported_focus[focus],
                    "path": path,
                    "value": None if value is None else {"nt": value_nt, "shown": show(value, prefixes)},
                    "shape": None if shape is None else {"nt": shape_nt, "shown": show(shape, prefixes)},
                    "property_shape": str(_ox_from_rdflib(source))
                    if source is not None and source in parents
                    else None,
                    "component": component,
                    "constraint": _constraint_text(component, source, shapes_graph, prefixes)
                    if source is not None
                    else None,
                    "messages": messages,
                    "message": _message(messages) or f"{component} constraint not met",
                }
            )
    results.sort(
        key=lambda r: (
            SEVERITY_RANK[r["severity"]],
            r["shape"]["shown"] if r["shape"] else "",
            r["path"] or "",
            r["component"],
            r["focus"]["shown"],
            r["value"]["shown"] if r["value"] else "",
        )
    )
    counts = dict.fromkeys(SEVERITIES, 0)
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in results:
        counts[item["severity"]] += 1
        group_key = (item["severity"], item["shape"]["nt"] if item["shape"] else None, item["path"], item["component"])
        group = groups.get(group_key)
        if group is None:
            group = groups[group_key] = {
                "key": _result_key([str(part) for part in group_key]),
                "severity": item["severity"],
                "shape": item["shape"],
                "path": item["path"],
                "component": item["component"],
                "constraint": item["constraint"],
                "message": item["message"],
                "count": 0,
                "focus": [],
                "focus_count": 0,
                "_seen": set(),
            }
        group["count"] += 1
        if item["focus"]["nt"] not in group["_seen"]:
            group["_seen"].add(item["focus"]["nt"])
            group["focus_count"] += 1
            if len(group["focus"]) < MAX_GROUP_FOCUS:
                group["focus"].append({**item["focus"], "imported": item["imported"]})
    for group in groups.values():
        group.pop("_seen")
    focus_counts: dict[str, dict[str, int]] = {}
    for item in results:
        counted = focus_counts.get(item["focus"]["nt"])
        if counted is None:
            if len(focus_counts) >= MAX_FOCUS_COUNTS:
                continue
            counted = focus_counts[item["focus"]["nt"]] = dict.fromkeys(SEVERITIES, 0)
        counted[item["severity"]] += 1
    finished = time.monotonic()
    return Outcome(
        conforms=bool(conforms) and not results,
        counts=counts,
        results=results,
        groups=list(groups.values())[:MAX_GROUPS],
        unsupported=unsupported,
        data_statements=len(collected.data),
        inferred_statements=len(inferred),
        elapsed_ms={
            "convert": int((converted - started) * 1000),
            "validate": int((validated - converted) * 1000),
            "total": int((finished - started) * 1000),
        },
        skipped=skipped,
        focus_counts=focus_counts,
    )


__all__ = [
    "ENGINE_ID",
    "INFERENCE_MODES",
    "PYSHACL_VERSION",
    "REMOVED_PREDICATES",
    "REMOVED_TYPES",
    "SEVERITIES",
    "Collected",
    "Outcome",
    "ShapesSource",
    "ValidationRefused",
    "collect",
    "inferred_statements",
    "prepare_shapes",
    "show",
    "validate",
]
