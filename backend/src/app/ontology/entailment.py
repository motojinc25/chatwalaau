"""Query-time entailment for the ontology search lanes (CTR-0170 v7, PRP-0209, UDR-0191).

Search takes an entailment mode (D1): ``simple`` answers from the stated statements
only (the default everywhere); ``rdfs`` adds the statements the RDF and RDFS
entailment rules of RDF 1.1 / 1.2 Semantics derive from them. ``owl-rl`` is reserved
for OWL 2 RL on the same engine and is refused until it exists.

The closure is computed by owlrl through its ``oxigraph`` extra (D2), on a PRIVATE
working Store -- never on a cached query Store -- with every literal and every triple
term replaced by an opaque placeholder IRI and restored afterwards (D4): the engine
cannot change a lexical form, a language tag, a base direction or a triple term, and
nothing is inferred inside a triple term. Inferred statements whose subject is a
placeholder (generalized RDF) are dropped. Answers leave out the trivial statements
of D5 (``trim``); the W3C gate checks the untrimmed closure.

For a dataset, ``scoped_closure`` also gives each named graph the statements that
follow from that graph's statements together with the schema of the whole dataset (D7):
``lib:book1 a ex:Book`` in ``g:catalog`` with ``ex:Book rdfs:subClassOf
ex:Publication`` in ``g:schema`` puts ``lib:book1 a ex:Publication`` in ``g:catalog``.
The caller (``store.ensure_entailment``) builds an entailed copy of the query Store from
the result (D3); nothing here writes a file. owlrl and rdflib are imported on the first
closure only.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

SIMPLE = "simple"
RDFS = "rdfs"
OWL_RL = "owl-rl"

# D1: the implemented modes, and the reserved ones (refused, never treated as another).
MODES: tuple[str, ...] = (SIMPLE, RDFS)
RESERVED_MODES: tuple[str, ...] = (OWL_RL,)
DEFAULT_MODE = SIMPLE

# The SPARQL 1.1 Entailment Regimes IRI of each mode (documentation and answers).
REGIME_IRIS = {
    SIMPLE: "http://www.w3.org/ns/entailment/Simple",
    RDFS: "http://www.w3.org/ns/entailment/RDFS",
    OWL_RL: "http://www.w3.org/ns/entailment/OWL-RL",
}

INFERRED_GRAPH_PREFIX = "urn:x-chatwalaau:entailment:"
PLACEHOLDER_PREFIX = "urn:x-chatwalaau:term:"

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL = "http://www.w3.org/2002/07/owl#"
_XSD = "http://www.w3.org/2001/XMLSchema#"
BUILTIN_NAMESPACES = (_RDF, _RDFS, _OWL, _XSD)
# owlrl writes its error messages with this vocabulary (never part of an answer).
_OWLRL_ERROR_NS = "http://www.daml.org/2002/03/agents/agent-ont#"

# One closure at a time: owlrl is pure Python and CPU-bound, so running several in
# parallel only multiplies the memory of the working Stores.
_ENGINE_LOCK = threading.Lock()


class EntailmentError(ValueError):
    """A refused entailment request; ``code`` is the machine-readable reason."""

    code = "entailment_invalid"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = {"code": self.code, "message": message, **detail}


class EntailmentUnavailable(EntailmentError):
    code = "entailment_unavailable"


class EntailmentTooLarge(EntailmentError):
    code = "entailment_too_large"


def parse_mode(value: Any) -> str:
    """The mode of a request: ``None`` / empty = ``simple``; a reserved or unknown one raises."""
    if value is None:
        return DEFAULT_MODE
    mode = str(value).strip().lower()
    if not mode:
        return DEFAULT_MODE
    if mode in MODES:
        return mode
    if mode in RESERVED_MODES:
        raise EntailmentUnavailable(f"The entailment mode {mode!r} is not available yet.", mode=mode)
    raise EntailmentError(
        f"Unknown entailment mode {mode!r}; use one of: {', '.join(MODES)}.", mode=mode, modes=list(MODES)
    )


def inferred_graph_name(mode: str) -> Any:
    """The named graph the engine writes its inferred statements to in its private working Store."""
    import pyoxigraph as ox

    return ox.NamedNode(f"{INFERRED_GRAPH_PREFIX}{mode}")


def max_input_triples() -> int:
    """The ``ontology_entailment_max_triples`` App Settings bound (D8)."""
    from app.core.config import settings

    return max(1, int(settings.ontology_entailment_max_triples))


# ---- Trimming (D5) -----------------------------------------------------------------


def is_trivial(triple: Any) -> bool:
    """True for an inferred statement the answers leave out (PRP-0209 2.4, operator Q2).

    rdfs4a / rdfs4b (``rdf:type rdfs:Resource``), rdfs6 / rdfs10 (reflexive
    sub-property / sub-class), rdfs8 / rdfs13 (``rdfs:subClassOf rdfs:Resource`` /
    ``rdfs:Literal``), rdfs1 (``rdf:type rdfs:Datatype``), the RDF rule for predicates
    (``rdf:type rdf:Property``), and every statement about an rdf:, rdfs:, owl: or xsd:
    term (the axiomatic triples).
    """
    import pyoxigraph as ox

    subject, predicate, obj = triple.subject, triple.predicate.value, triple.object
    if isinstance(subject, ox.NamedNode) and subject.value.startswith(BUILTIN_NAMESPACES):
        return True
    target = obj.value if isinstance(obj, ox.NamedNode) else None
    if predicate == f"{_RDF}type":
        return target in (f"{_RDFS}Resource", f"{_RDFS}Datatype", f"{_RDF}Property")
    if predicate in (f"{_RDFS}subClassOf", f"{_RDFS}subPropertyOf"):
        if subject == obj:
            return True
        return predicate == f"{_RDFS}subClassOf" and target in (f"{_RDFS}Resource", f"{_RDFS}Literal")
    return False


# ---- The closure (D2, D4) --------------------------------------------------------


class _Placeholders:
    """Opaque IRIs for one run, one per distinct hidden term.

    Every literal and triple term is hidden (D4). So is every blank node that is the
    object of ``rdfs:subPropertyOf`` (an anonymous super-property such as ``[ owl:inverseOf
    ex:p ]``): rule rdfs7 would otherwise put it in the predicate position, which the
    Oxigraph backend cannot hold; as a placeholder IRI the run goes on, and statements
    with it as predicate are dropped afterwards (generalized RDF).
    """

    def __init__(self, blank_nodes: set[Any] | None = None) -> None:
        import pyoxigraph as ox

        self._ox = ox
        self._prefix = f"{PLACEHOLDER_PREFIX}{uuid.uuid4().hex}:"
        self._blank_nodes = blank_nodes or set()
        self._to: dict[Any, Any] = {}
        self._back: dict[str, Any] = {}

    def hide(self, term: Any) -> Any:
        if isinstance(term, self._ox.Literal | self._ox.Triple) or term in self._blank_nodes:
            node = self._to.get(term)
            if node is None:
                node = self._ox.NamedNode(f"{self._prefix}{len(self._to)}")
                self._to[term] = node
                self._back[node.value] = term
            return node
        return term

    def show(self, term: Any) -> Any:
        if isinstance(term, self._ox.NamedNode):
            return self._back.get(term.value, term)
        return term


def closure(
    triples: Iterable[Any], *, mode: str = RDFS, trim: bool = True, axioms: bool = True, check_limit: bool = True
) -> list[Any]:
    """The statements ``mode`` infers from ``triples`` that are not among them.

    ``triples`` are the stated statements (pyoxigraph ``Triple``). The result is a list
    of ``Triple`` in the original terms (literals and triple terms restored exactly),
    without the trivial statements of D5 when ``trim``. ``axioms`` adds the RDF / RDFS
    axiomatic triples before chaining (the RDFS regime; they are trimmed from answers).
    Raises ``EntailmentTooLarge`` above the App Settings bound (``check_limit``; the
    per-graph runs of ``scoped_closure`` are bounded by the check of the whole dataset).
    """
    if mode != RDFS:
        raise EntailmentUnavailable(f"The entailment mode {mode!r} is not available yet.", mode=mode)
    import pyoxigraph as ox

    stated = list(triples)
    limit = max_input_triples()
    if check_limit and len(stated) > limit:
        raise EntailmentTooLarge(
            f"This ontology has {len(stated):,} statements; RDFS entailment accepts at most {limit:,} "
            "(App Settings: ontology_entailment_max_triples).",
            statements=len(stated),
            limit=limit,
        )
    sub_property = ox.NamedNode(f"{_RDFS}subPropertyOf")
    placeholders = _Placeholders(
        {t.object for t in stated if t.predicate == sub_property and isinstance(t.object, ox.BlankNode)}
    )
    work = ox.Store()
    default_graph = ox.DefaultGraph()
    work.extend(
        ox.Quad(placeholders.hide(t.subject), t.predicate, placeholders.hide(t.object), default_graph) for t in stated
    )
    destination = inferred_graph_name(mode)
    with _ENGINE_LOCK:
        import owlrl

        # improved_datatypes=False: no literal reaches the engine, and the improved
        # conversions are switched by a process-wide toggle.
        owlrl.DeductiveClosure(
            owlrl.RDFS_Semantics, improved_datatypes=False, axiomatic_triples=axioms, datatype_axioms=False
        ).expand(work, destination=destination.value)
    stated_set = set(stated)
    inferred: list[Any] = []
    seen: set[Any] = set()
    errors = 0
    for quad in work.quads_for_pattern(None, None, None, destination):
        subject, predicate, obj = quad.subject, quad.predicate, quad.object
        if predicate.value.startswith(_OWLRL_ERROR_NS) or isinstance(obj, ox.Literal):
            errors += 1  # owlrl's own error statements -- never part of an answer
            continue
        subject, predicate, obj = placeholders.show(subject), placeholders.show(predicate), placeholders.show(obj)
        if not isinstance(subject, ox.NamedNode | ox.BlankNode) or not isinstance(predicate, ox.NamedNode):
            continue  # a literal or triple term as subject, a blank node as predicate: generalized RDF (D4)
        triple = ox.Triple(subject, predicate, obj)
        if triple in stated_set or triple in seen:
            continue
        if trim and is_trivial(triple):
            continue
        seen.add(triple)
        inferred.append(triple)
    if errors:
        logger.debug("owlrl reported %d error statement(s) during an %s closure", errors, mode)
    return inferred


# ---- Per-graph statements (D7) -------------------------------------------------------

# The statements an RDFS rule takes its schema from: the hierarchies, domains and ranges,
# and container membership properties (rdfs12).
_SCHEMA_PREDICATES = frozenset(f"{_RDFS}{name}" for name in ("subClassOf", "subPropertyOf", "domain", "range"))


def _is_schema(triple: Any) -> bool:
    predicate = triple.predicate.value
    if predicate in _SCHEMA_PREDICATES:
        return True
    return predicate == f"{_RDF}type" and getattr(triple.object, "value", None) == (
        f"{_RDFS}ContainerMembershipProperty"
    )


def scoped_closure(
    merged: list[Any], graphs: dict[Any, list[Any]], *, mode: str = RDFS
) -> tuple[list[Any], dict[Any, list[Any]]]:
    """The inferred statements of the merge, and of each named graph (D7).

    ``merged`` is the default graph of the query Store (the scope merge, imports
    included); ``graphs`` maps each of the ontology's named graphs to its statements.
    A statement inferred for graph G follows from G's statements together with the
    schema of the whole dataset (the merge's schema statements, stated and inferred),
    and is not already in G. A statement that follows from the schema alone (for
    example ``ex:Book a rdfs:Class``) belongs to G only when G's own statements give
    it. Only the merge is checked against the input bound.
    """
    merge_inferred = closure(merged, mode=mode)
    if not graphs:
        return merge_inferred, {}
    schema = list(dict.fromkeys(t for t in (*merged, *merge_inferred) if _is_schema(t)))
    from_schema = {*schema, *closure(schema, mode=mode, check_limit=False)}
    per_graph: dict[Any, list[Any]] = {}
    for name, stated in graphs.items():
        own = set(stated)
        alone = closure(stated, mode=mode, check_limit=False)
        alone_set = set(alone)
        with_schema = closure([*stated, *schema], mode=mode, check_limit=False)
        candidates = dict.fromkeys([*with_schema, *alone])
        per_graph[name] = [t for t in candidates if t not in own and (t not in from_schema or t in alone_set)]
    return merge_inferred, per_graph


__all__ = [
    "DEFAULT_MODE",
    "INFERRED_GRAPH_PREFIX",
    "MODES",
    "OWL_RL",
    "RDFS",
    "REGIME_IRIS",
    "RESERVED_MODES",
    "SIMPLE",
    "EntailmentError",
    "EntailmentTooLarge",
    "EntailmentUnavailable",
    "closure",
    "inferred_graph_name",
    "is_trivial",
    "max_input_triples",
    "parse_mode",
    "scoped_closure",
]
