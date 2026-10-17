"""Query-time entailment for the ontology search lanes (CTR-0170 v8, PRP-0209 / PRP-0210,
UDR-0191 / UDR-0192).

Search takes an entailment mode (UDR-0191 D1): ``simple`` answers from the stated
statements only (the default everywhere); ``rdfs`` adds the statements the RDF and
RDFS entailment rules of RDF 1.1 / 1.2 Semantics derive from them; ``owl-rl`` adds
the OWL 2 RL/RDF rules on top (UDR-0192 D1), so it gives every ``rdfs`` answer too.

The closure is computed by owlrl through its ``oxigraph`` extra (D2), on a PRIVATE
working Store -- never on a cached query Store -- with every literal and every triple
term replaced by an opaque placeholder IRI and restored afterwards (D4): the engine
cannot change a lexical form, a language tag, a base direction or a triple term, and
nothing is inferred inside a triple term. Inferred statements whose subject is a
placeholder (generalized RDF) are dropped. Answers leave out the trivial statements
of D5 (``trim``; UDR-0192 D4 adds the OWL ones); the W3C gates check the untrimmed
closure.

In ``owl-rl`` mode owlrl reports inconsistencies as error statements; ``closure``
turns them into findings (UDR-0192 D5) with a kind from ``_ERROR_KINDS`` and the
restored terms, and reports an ``owl:sameAs`` between two different literals
(``literal_identity``), which owlrl cannot see without the literals.

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
import re
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
MODES: tuple[str, ...] = (SIMPLE, RDFS, OWL_RL)
RESERVED_MODES: tuple[str, ...] = ()
DEFAULT_MODE = SIMPLE

# The SPARQL 1.1 Entailment Regimes IRI of each mode (documentation and answers).
REGIME_IRIS = {
    SIMPLE: "http://www.w3.org/ns/entailment/Simple",
    RDFS: "http://www.w3.org/ns/entailment/RDFS",
    OWL_RL: "http://www.w3.org/ns/entailment/OWL-RL",
}

# How answers and messages name each mode.
MODE_LABELS = {SIMPLE: "no entailment", RDFS: "RDFS entailment", OWL_RL: "OWL 2 RL entailment"}

# The App Settings key bounding the input of each mode (UDR-0191 D8, UDR-0192 D7) and
# the output of owl-rl (UDR-0192 D8).
INPUT_SETTINGS = {RDFS: "ontology_entailment_max_triples", OWL_RL: "ontology_owl_rl_max_triples"}
RATIO_SETTING = "ontology_owl_rl_max_inferred_ratio"

PLACEHOLDER_PREFIX = "urn:x-chatwalaau:term:"

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL = "http://www.w3.org/2002/07/owl#"
_XSD = "http://www.w3.org/2001/XMLSchema#"
BUILTIN_NAMESPACES = (_RDF, _RDFS, _OWL, _XSD)
# owlrl writes its error messages with this vocabulary (never part of an answer).
_OWLRL_ERROR_NS = "http://www.daml.org/2002/03/agents/agent-ont#"

_TYPE = f"{_RDF}type"
_SAME_AS = f"{_OWL}sameAs"
_DIFFERENT_FROM = f"{_OWL}differentFrom"

# One closure at a time: owlrl is pure Python and CPU-bound, so running several in
# parallel only multiplies the memory of the working Stores.
_ENGINE_LOCK = threading.Lock()

# PRP-0211 (UDR-0193 D11): ``rdfs`` uses the direct engine (``rdfs_engine``), proven
# equal to owlrl's ``RDFS_Semantics``; "owlrl" switches back (the tests use both).
RDFS_ENGINE = "direct"

# The engine of each mode, as stored results name it (UDR-0193 D8): a change of an
# engine or of its rules MUST change its id, so no stored result is read again.
ENGINE_IDS = {RDFS: "rdfs-direct-1", OWL_RL: "owlrl-7.6.2/side-rules-1"}


def engine_id(mode: str) -> str:
    """The id of the engine ``mode`` uses now."""
    if mode == RDFS and RDFS_ENGINE != "direct":
        return "owlrl-7.6.2/rdfs-1"
    return ENGINE_IDS[mode]


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


class EntailmentPending(EntailmentError):
    """The stored result of this mode is still being computed (PRP-0211, UDR-0193 D9)."""

    code = "entailment_pending"


class EntailmentFailed(EntailmentError):
    """The engine raised on this input (a defect of the engine, reported, never a 500)."""

    code = "entailment_failed"


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


def max_input_triples() -> int:
    """The ``ontology_entailment_max_triples`` App Settings bound of ``rdfs`` (UDR-0191 D8)."""
    from app.core.config import settings

    return max(1, int(settings.ontology_entailment_max_triples))


def max_owl_rl_input_triples() -> int:
    """The ``ontology_owl_rl_max_triples`` App Settings bound of ``owl-rl`` (UDR-0192 D7)."""
    from app.core.config import settings

    return max(1, int(settings.ontology_owl_rl_max_triples))


def max_inferred_ratio() -> int:
    """The ``ontology_owl_rl_max_inferred_ratio`` App Settings bound K (UDR-0192 D8)."""
    from app.core.config import settings

    return max(1, int(settings.ontology_owl_rl_max_inferred_ratio))


def input_bound(mode: str) -> int:
    """The largest input ``mode`` accepts."""
    return max_owl_rl_input_triples() if mode == OWL_RL else max_input_triples()


# ---- Trimming (UDR-0191 D5, UDR-0192 D4) --------------------------------------------

# Inferred typings owl-rl answers leave out: owl:Thing (cls-thing and the full binding of
# owl:Thing to rdfs:Resource) and inferred declarations (scm-cls, scm-op, scm-dp, prp-ap;
# operator Q5). A stated declaration is stated, so it is never trimmed.
_OWL_TRIVIAL_TYPES = frozenset(
    (
        f"{_OWL}Thing",
        f"{_OWL}Class",
        f"{_RDFS}Class",
        f"{_OWL}ObjectProperty",
        f"{_OWL}DatatypeProperty",
        f"{_OWL}AnnotationProperty",
    )
)
_REFLEXIVE_OWL = frozenset((_SAME_AS, f"{_OWL}equivalentClass", f"{_OWL}equivalentProperty"))


_AXIOMATIC: dict[str, frozenset[Any]] = {}


def axiomatic_closure(mode: str) -> frozenset[Any]:
    """What ``mode`` infers from NO statement: the axiomatic triples and their consequences.

    Computed once per mode and process (the closure of the empty graph, untrimmed); these
    hold in every graph, so answers leave them out (UDR-0191 D5 after A3).
    """
    held = _AXIOMATIC.get(mode)
    if held is None:
        held = frozenset(closure([], mode=mode, trim=False, check_limit=False))
        _AXIOMATIC[mode] = held
    return held


def is_trivial(triple: Any, mode: str = RDFS, described: frozenset[Any] | set[Any] = frozenset()) -> bool:
    """True for an inferred statement the answers leave out (PRP-0209 2.4, PRP-0210 2.4).

    rdfs4a / rdfs4b (``rdf:type rdfs:Resource``), rdfs6 / rdfs10 (reflexive
    sub-property / sub-class), rdfs8 / rdfs13 (``rdfs:subClassOf rdfs:Resource`` /
    ``rdfs:Literal``), rdfs1 (``rdf:type rdfs:Datatype``), the RDF rule for predicates
    (``rdf:type rdf:Property``), and every statement about an rdf:, rdfs:, owl: or xsd:
    term (the axiomatic triples and what merely USING the vocabulary gives, such as
    ``owl:Class a rdfs:Class``) -- unless the ontology itself states something about that
    term (``described``: the built-in subjects of the input) and the statement does not
    follow from the axiomatic triples alone (``axiomatic_closure``). So ``g:schema`` stating
    ``rdf:langString a rdfs:Datatype`` and ``rdfs:Datatype rdfs:subClassOf rdfs:Class``
    answers ``rdf:langString a rdfs:Class`` (UDR-0191 A3).
    ``owl-rl`` adds: reflexive ``owl:sameAs`` (eq-ref),
    ``owl:equivalentClass`` and ``owl:equivalentProperty``; ``rdf:type owl:Thing``;
    ``rdfs:subClassOf owl:Thing``; inferred declarations (``owl:Class``, ``rdfs:Class``,
    ``owl:ObjectProperty``, ``owl:DatatypeProperty``, ``owl:AnnotationProperty``).
    ``owl:Nothing rdfs:subClassOf ?c`` has an owl: subject.
    """
    import pyoxigraph as ox

    subject, predicate, obj = triple.subject, triple.predicate.value, triple.object
    if (
        isinstance(subject, ox.NamedNode)
        and subject.value.startswith(BUILTIN_NAMESPACES)
        and (subject not in described or triple in axiomatic_closure(mode))
    ):
        return True
    target = obj.value if isinstance(obj, ox.NamedNode) else None
    if mode == OWL_RL:
        if predicate in _REFLEXIVE_OWL and subject == obj:
            return True
        if predicate == _TYPE and target in _OWL_TRIVIAL_TYPES:
            return True
        if predicate == f"{_RDFS}subClassOf" and target == f"{_OWL}Thing":
            return True
    if predicate == _TYPE:
        return target in (f"{_RDFS}Resource", f"{_RDFS}Datatype", f"{_RDF}Property")
    if predicate in (f"{_RDFS}subClassOf", f"{_RDFS}subPropertyOf"):
        if subject == obj:
            return True
        return predicate == f"{_RDFS}subClassOf" and target in (f"{_RDFS}Resource", f"{_RDFS}Literal")
    return False


# ---- Inconsistency findings (UDR-0192 D5) --------------------------------------------

# owlrl 7.6.2's error texts (``OWLRL.py``), each with the kind it reports. The table is
# tied to the pinned version: an invariant checks every template still occurs in owlrl.
_ERROR_KINDS: tuple[tuple[str, str], ...] = (
    ("nothing_member", "%s is defined of type 'Nothing'"),
    ("same_and_different", "'sameAs' and 'differentFrom' cannot be used on the same subject-object pair: (%s, %s)"),
    ("same_and_different", "'sameAs' and 'AllDifferent' cannot be used on the same subject-object pair: (%s, %s)"),
    ("complement", "Violation of complementarity for classes %s and %s on element %s"),
    ("disjoint_classes", "Disjoint classes %s and %s have a common individual %s"),
    ("irreflexive", "Irreflexive property used on %s with %s"),
    ("disjoint_properties", "Erroneous usage of disjoint properties %s and %s on %s and %s"),
    (
        "disjoint_properties",
        "Disjoint properties in an 'AllDisjointProperties' are not really disjoint: (%s, %s,%s) and (%s,%s,%s)",
    ),
    ("all_values_datatype", "Violation of type restriction for allValuesFrom in %s for datatype %s on value %s"),
    ("asymmetric", "Erroneous usage of asymmetric property %s on %s and %s"),
    ("max_cardinality", "Erroneous usage of maximum cardinality with %s and %s"),
    ("max_cardinality", "Erroneous usage of maximum qualified cardinality with %s, %s and %s"),
    ("negative_assertion", "Negative (object) property assertion violated for: (%s, %s, %s)"),
    ("negative_assertion", "Negative (datatype) property assertion violated for: (%s, %s, %s)"),
    ("other", "Lexical value of the literal '%s' does not match its datatype (%s)"),
)
FINDING_KINDS: tuple[str, ...] = (
    "same_and_different",
    "irreflexive",
    "asymmetric",
    "disjoint_properties",
    "negative_assertion",
    "nothing_member",
    "complement",
    "max_cardinality",
    "disjoint_classes",
    "all_values_datatype",
    "literal_identity",
    "other",
)
LITERAL_IDENTITY_TEMPLATE = "Two different values %s and %s are made the same (a functional property or a key)"
_ERROR_PATTERNS = tuple(
    (kind, template, re.compile("^" + re.escape(template).replace("%s", "(.+?)") + "$"))
    for kind, template in _ERROR_KINDS
)
_PLACEHOLDER_RE = re.compile(re.escape(PLACEHOLDER_PREFIX) + r"[0-9a-f]+:\d+")


def _finding(text: str, resolve: Any) -> dict[str, Any]:
    """One owlrl error text as ``{kind, template, terms}`` (terms restored by ``resolve``)."""
    for kind, template, pattern in _ERROR_PATTERNS:
        match = pattern.match(text)
        if match:
            return {"kind": kind, "template": template, "terms": [resolve(g.strip()) for g in match.groups()]}
    # An unknown text: keep it, with its placeholders restored.
    tokens = _PLACEHOLDER_RE.findall(text)
    return {
        "kind": "other",
        "template": _PLACEHOLDER_RE.sub("%s", text.replace("%", "%%")),
        "terms": [resolve(token) for token in tokens],
    }


# ---- The closure (UDR-0191 D2, D4; UDR-0192 D2, D3) ------------------------------


class _Placeholders:
    """Opaque IRIs for one run, one per distinct hidden term.

    Every literal and triple term is hidden (D4). So is every blank node that rules can
    put in the predicate position (``blank_nodes``): an anonymous super-property such as
    ``[ owl:inverseOf ex:p ]``, which the Oxigraph backend cannot hold as a predicate.
    As a placeholder IRI the run goes on, and statements with it as predicate are
    dropped afterwards (generalized RDF).
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

    def show_text(self, token: str) -> Any:
        """The original term of a placeholder IRI written in a message, else None."""
        return self._back.get(token)


# Properties whose blank-node values rules can turn into predicates (UDR-0192 D3).
_PROPERTY_LINKS = frozenset(
    f"{ns}{name}"
    for ns, name in (
        (_RDFS, "subPropertyOf"),
        (_OWL, "equivalentProperty"),
        (_OWL, "inverseOf"),
        (_OWL, "propertyDisjointWith"),
    )
)


def _list_members(head: Any, first: dict[Any, Any], rest: dict[Any, Any]) -> list[Any]:
    """The members of the RDF list starting at ``head`` (cycles and broken lists end it)."""
    out: list[Any] = []
    seen: set[Any] = set()
    node = head
    while node in first and node not in seen:
        seen.add(node)
        out.append(first[node])
        node = rest.get(node)
    return out


def _property_blank_nodes(stated: list[Any], mode: str) -> set[Any]:
    """The blank nodes to hide so no rule can make a blank-node predicate."""
    import pyoxigraph as ox

    sub_property = f"{_RDFS}subPropertyOf"
    if mode != OWL_RL:  # rdfs: rdfs7 only (UDR-0191 D4)
        return {t.object for t in stated if t.predicate.value == sub_property and isinstance(t.object, ox.BlankNode)}
    found: set[Any] = set()
    first = {t.subject: t.object for t in stated if t.predicate.value == f"{_RDF}first"}
    rest = {t.subject: t.object for t in stated if t.predicate.value == f"{_RDF}rest"}
    all_disjoint = ox.NamedNode(f"{_OWL}AllDisjointProperties")
    disjoint_sets = {t.subject for t in stated if t.predicate.value == _TYPE and t.object == all_disjoint}
    for t in stated:
        predicate = t.predicate.value
        if predicate in _PROPERTY_LINKS:
            found.update((t.subject, t.object))
        elif predicate == f"{_OWL}onProperty":
            found.add(t.object)
        elif predicate == f"{_OWL}propertyChainAxiom":
            found.add(t.subject)
            found.update(_list_members(t.object, first, rest))
        elif predicate == f"{_OWL}members" and t.subject in disjoint_sets:
            found.update(_list_members(t.object, first, rest))
    return {term for term in found if isinstance(term, ox.BlankNode)}


_MAX_CARDINALITY = frozenset((f"{_OWL}maxCardinality", f"{_OWL}maxQualifiedCardinality"))
_SIDE_RULE_ROUNDS = 5
_INTEGER_TYPES = frozenset(
    f"{_XSD}{name}"
    for name in (
        "integer",
        "nonNegativeInteger",
        "positiveInteger",
        "nonPositiveInteger",
        "negativeInteger",
        "long",
        "int",
        "short",
        "byte",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
    )
)


def _cardinality(term: Any) -> int | None:
    """The number of a cardinality restriction; None when it is not an integer literal."""
    import pyoxigraph as ox

    if not isinstance(term, ox.Literal) or term.datatype.value not in _INTEGER_TYPES:
        return None
    try:
        return int(term.value.strip())
    except ValueError:
        return None


def _expand(work: Any, mode: str, axioms: bool) -> None:
    """One owlrl run on the private ``work`` Store, IN PLACE (UDR-0191 D2, UDR-0192 D2).

    With a separate destination graph owlrl's look-ups read only the input graph, so a
    rule needing an inferred statement besides the one it visits (cls-com, cax-dw,
    prp-* look-ups) never fired; in place, every rule sees every statement. The caller
    takes the inferred statements as the difference with the input.
    """
    if mode == RDFS and RDFS_ENGINE == "direct":
        # PRP-0211 (UDR-0193 D11): owlrl's RDFS result, computed directly (proven equal).
        from app.ontology import rdfs_engine

        rdfs_engine.expand(work, axioms=axioms)
        return
    owlrl_expand(work, mode, axioms)


def owlrl_expand(work: Any, mode: str, axioms: bool) -> None:
    """One owlrl run (``RDFS_Semantics`` or ``RDFS_OWLRL_Semantics``) on ``work``, in place."""
    with _ENGINE_LOCK:
        import owlrl

        semantics = owlrl.RDFS_OWLRL_Semantics if mode == OWL_RL else owlrl.RDFS_Semantics
        # improved_datatypes=False: no literal reaches the engine, and the improved
        # conversions are switched by a process-wide toggle.
        try:
            owlrl.DeductiveClosure(
                semantics, improved_datatypes=False, axiomatic_triples=axioms, datatype_axioms=False
            ).expand(work)
        except Exception as exc:  # an engine defect on this input -- never a 500 (UDR-0192 D2)
            logger.warning("owlrl failed during an %s closure", mode, exc_info=True)
            raise EntailmentFailed(
                f"The reasoner could not compute {MODE_LABELS[mode]} for this ontology ({type(exc).__name__}).",
                mode=mode,
            ) from exc


def _side_rules(
    work: Any,
    numbers: dict[Any, tuple[str, int | None]],
    keys: dict[Any, list[list[Any]]],
    placeholders: _Placeholders,
) -> tuple[set[Any], list[dict[str, Any]]]:
    """cls-maxc1/2, cls-maxqc1-4 and prp-key over the statements of one engine run.

    Returns the owl:sameAs quads the rules give (default graph, placeholder terms) and
    the inconsistencies (max cardinality 0 with a value), as findings.
    """
    import pyoxigraph as ox

    graphs = (ox.DefaultGraph(),)

    def objects(subject: Any, predicate: Any) -> list[Any]:
        return list(
            dict.fromkeys(q.object for g in graphs for q in work.quads_for_pattern(subject, predicate, None, g))
        )

    def holds(subject: Any, predicate: Any, obj: Any) -> bool:
        return any(next(iter(work.quads_for_pattern(subject, predicate, obj, g)), None) is not None for g in graphs)

    rdf_type = ox.NamedNode(_TYPE)
    on_property = ox.NamedNode(f"{_OWL}onProperty")
    on_class = ox.NamedNode(f"{_OWL}onClass")
    thing = ox.NamedNode(f"{_OWL}Thing")
    same_as = ox.NamedNode(_SAME_AS)
    out: set[Any] = set()
    found: list[dict[str, Any]] = []
    for restriction, (predicate, number) in numbers.items():
        if number not in (0, 1):
            continue
        qualified = predicate == f"{_OWL}maxQualifiedCardinality"
        classes = objects(restriction, on_class) if qualified else [thing]
        members = list(
            dict.fromkeys(q.subject for g in graphs for q in work.quads_for_pattern(None, rdf_type, restriction, g))
        )
        for prop in objects(restriction, on_property):
            for cls in classes:
                for member in members:
                    values = [
                        y for y in objects(member, prop) if not qualified or cls == thing or holds(y, rdf_type, cls)
                    ]
                    if number == 0:
                        for value in values:
                            terms = [restriction, cls, value] if qualified else [restriction, value]
                            found.append(
                                {
                                    "kind": "max_cardinality",
                                    "template": (
                                        "Erroneous usage of maximum qualified cardinality with %s, %s and %s"
                                        if qualified
                                        else "Erroneous usage of maximum cardinality with %s and %s"
                                    ),
                                    "terms": [placeholders.show(term) for term in terms],
                                }
                            )
                        continue
                    for first in values:
                        for second in values:
                            if first != second:
                                out.add(ox.Quad(first, same_as, second, ox.DefaultGraph()))
    # prp-key: members of a class with the same values for EVERY key property are one.
    for cls, key_lists in keys.items():
        members = list(dict.fromkeys(q.subject for g in graphs for q in work.quads_for_pattern(None, rdf_type, cls, g)))
        for properties in key_lists:
            groups: dict[tuple[Any, ...], list[Any]] = {}
            for member in members:
                combos: list[tuple[Any, ...]] = [()]
                for prop in properties:
                    combos = [(*combo, value) for combo in combos for value in objects(member, prop)]
                for combo in combos:
                    groups.setdefault(combo, []).append(member)
            for group in groups.values():
                for first in group:
                    for second in group:
                        if first != second:
                            out.add(ox.Quad(first, same_as, second, ox.DefaultGraph()))
    return out, found


def closure(
    triples: Iterable[Any],
    *,
    mode: str = RDFS,
    trim: bool = True,
    axioms: bool = True,
    check_limit: bool = True,
    findings: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """The statements ``mode`` infers from ``triples`` that are not among them.

    ``triples`` are the stated statements (pyoxigraph ``Triple``). The result is a list
    of ``Triple`` in the original terms (literals and triple terms restored exactly),
    without the trivial statements of D5 when ``trim``. ``axioms`` adds the axiomatic
    triples before chaining (they are trimmed from answers). Raises
    ``EntailmentTooLarge`` above the mode's App Settings bound (``check_limit``; the
    per-graph runs of ``scoped_closure`` are bounded by the check of the whole dataset).
    In ``owl-rl`` mode the inconsistencies found are appended to ``findings`` as
    ``{kind, template, terms}`` (UDR-0192 D5).
    """
    if mode not in (RDFS, OWL_RL):
        raise EntailmentUnavailable(f"The entailment mode {mode!r} has no closure.", mode=mode)
    import pyoxigraph as ox

    stated = list(triples)
    limit = input_bound(mode)
    if check_limit and len(stated) > limit:
        raise EntailmentTooLarge(
            f"This ontology has {len(stated):,} statements; {MODE_LABELS[mode]} accepts at most {limit:,} "
            f"(App Settings: {INPUT_SETTINGS[mode]}).",
            statements=len(stated),
            limit=limit,
            reason="input",
            setting=INPUT_SETTINGS[mode],
        )
    hidden_blank_nodes = _property_blank_nodes(stated, mode)
    placeholders = _Placeholders(hidden_blank_nodes)
    default_graph = ox.DefaultGraph()
    # owl-rl: two rule groups run beside the engine (UDR-0192 D2), between engine runs,
    # until no owl:sameAs is new:
    # - cls-maxc1/2, cls-maxqc1-4: owlrl reads the NUMBER of a max (qualified) cardinality
    #   restriction, which a placeholder hides, and a real number in its Oxigraph graph
    #   makes it fail (dt-type2 puts the literal in the subject position);
    # - prp-key: owlrl 7.6.2 compares all key properties but the last one (an off-by-one),
    #   so a one-property key made every member of the class the same individual.
    # Those statements are kept from the engine; ``_side_rules`` applies them.
    numbers: dict[Any, tuple[str, int | None]] = {}
    keys: dict[Any, list[Any]] = {}
    engine_input: list[Any] = []
    if mode == OWL_RL:
        first = {t.subject: t.object for t in stated if t.predicate.value == f"{_RDF}first"}
        rest = {t.subject: t.object for t in stated if t.predicate.value == f"{_RDF}rest"}
    for t in stated:
        subject = placeholders.hide(t.subject)
        if mode == OWL_RL and t.predicate.value in _MAX_CARDINALITY:
            numbers[subject] = (t.predicate.value, _cardinality(t.object))
            continue
        if mode == OWL_RL and t.predicate.value == f"{_OWL}hasKey":
            members = [placeholders.hide(m) for m in _list_members(t.object, first, rest)]
            if members:
                keys.setdefault(subject, []).append(members)
            continue
        engine_input.append(ox.Quad(subject, t.predicate, placeholders.hide(t.object), default_graph))
    extra: set[Any] = set()  # owl:sameAs from the side rules (placeholder terms)
    side_findings: list[dict[str, Any]] = []
    for _round in range(_SIDE_RULE_ROUNDS):
        work = ox.Store()
        work.extend(engine_input)
        work.extend(extra)
        _expand(work, mode, axioms)
        if not numbers and not keys:
            break
        same_as, side_findings = _side_rules(work, numbers, keys, placeholders)
        if same_as <= extra:
            break
        extra |= same_as
    if findings is not None:
        findings.extend(side_findings)
    blank_ids = {term.value for t in stated for term in (t.subject, t.object) if isinstance(term, ox.BlankNode)}

    def resolve(token: str) -> Any:
        original = placeholders.show_text(token)
        if original is not None:
            return original
        if token in blank_ids:
            return ox.BlankNode(token)
        if ":" in token and " " not in token:
            try:
                return ox.NamedNode(token)
            except ValueError:
                pass
        return token

    stated_set = set(stated)
    # Built-in terms the ontology itself states something about (UDR-0191 A3).
    described = {
        t.subject
        for t in stated
        if isinstance(t.subject, ox.NamedNode) and t.subject.value.startswith(BUILTIN_NAMESPACES)
    }
    inferred: list[Any] = []
    seen: set[Any] = set()
    identified: set[frozenset[Any]] = set()
    errors = 0
    given = set(engine_input)
    for quad in work.quads_for_pattern(None, None, None, default_graph):
        if quad in given:
            continue
        subject, predicate, obj = quad.subject, quad.predicate, quad.object
        if predicate.value.startswith(_OWLRL_ERROR_NS) or isinstance(obj, ox.Literal):
            errors += 1  # owlrl's own error statements -- never part of an answer
            if findings is not None and predicate.value == f"{_OWLRL_ERROR_NS}error" and isinstance(obj, ox.Literal):
                findings.append(_finding(obj.value, resolve))
            continue
        if isinstance(obj, ox.NamedNode) and obj.value.startswith(_OWLRL_ERROR_NS):
            continue  # [] a err:ErrorMessage
        subject, predicate, obj = placeholders.show(subject), placeholders.show(predicate), placeholders.show(obj)
        if mode == OWL_RL and isinstance(predicate, ox.NamedNode) and predicate.value in (_SAME_AS, _DIFFERENT_FROM):
            hidden = (ox.Literal, ox.Triple)
            if isinstance(subject, hidden) or isinstance(obj, hidden):
                # Identity of literals is generalized RDF (UDR-0192 D3); two DIFFERENT
                # literals made the same is an inconsistency owlrl cannot see (A-6).
                pair = frozenset((subject, obj))
                if predicate.value == _SAME_AS and subject != obj and findings is not None and pair not in identified:
                    identified.add(pair)
                    first_term, second_term = sorted(pair, key=str)
                    findings.append(
                        {
                            "kind": "literal_identity",
                            "template": LITERAL_IDENTITY_TEMPLATE,
                            "terms": [first_term, second_term],
                        }
                    )
                continue
        if not isinstance(subject, ox.NamedNode | ox.BlankNode) or not isinstance(predicate, ox.NamedNode):
            continue  # a literal or triple term as subject, a blank node as predicate: generalized RDF (D4)
        triple = ox.Triple(subject, predicate, obj)
        if triple in stated_set or triple in seen:
            continue
        if trim and is_trivial(triple, mode, described):
            continue
        seen.add(triple)
        inferred.append(triple)
    if errors:
        logger.debug("owlrl reported %d error statement(s) during an %s closure", errors, mode)
    return inferred


# ---- Per-graph statements (UDR-0191 D7 after A1, UDR-0192 D6) --------------------------

# The statements an RDFS rule takes its schema from: the hierarchies, domains and ranges,
# and container membership properties (rdfs12).
_SCHEMA_PREDICATES = frozenset(f"{_RDFS}{name}" for name in ("subClassOf", "subPropertyOf", "domain", "range"))

# owl-rl: rdfs: / owl: predicates that are about individuals or annotations, not schema.
_NOT_SCHEMA_PREDICATES = frozenset(
    (
        _SAME_AS,
        _DIFFERENT_FROM,
        f"{_OWL}members",
        f"{_OWL}distinctMembers",
        f"{_OWL}sourceIndividual",
        f"{_OWL}assertionProperty",
        f"{_OWL}targetIndividual",
        f"{_OWL}targetValue",
        f"{_RDFS}label",
        f"{_RDFS}comment",
        f"{_RDFS}seeAlso",
        f"{_RDFS}isDefinedBy",
    )
)
_NOT_SCHEMA_TYPES = frozenset(
    (
        f"{_OWL}NamedIndividual",
        f"{_OWL}AllDifferent",
        f"{_OWL}NegativePropertyAssertion",
        f"{_OWL}Thing",
        f"{_RDFS}Resource",
    )
)


def _is_schema(triple: Any, mode: str = RDFS) -> bool:
    predicate = triple.predicate.value
    target = getattr(triple.object, "value", None)
    if mode == OWL_RL:
        if predicate == _TYPE:
            return isinstance(target, str) and target.startswith((_RDFS, _OWL)) and target not in _NOT_SCHEMA_TYPES
        if predicate.startswith((_RDFS, _OWL)):
            return predicate not in _NOT_SCHEMA_PREDICATES
        return False
    if predicate in _SCHEMA_PREDICATES:
        return True
    return predicate == _TYPE and target == f"{_RDFS}ContainerMembershipProperty"


def _schema(statements: list[Any], mode: str) -> list[Any]:
    """The schema statements of ``statements``, with the list cells they reach (owl-rl)."""
    import pyoxigraph as ox

    schema = [t for t in statements if _is_schema(t, mode)]
    if mode != OWL_RL:
        return list(dict.fromkeys(schema))
    cells: dict[Any, list[Any]] = {}
    for t in statements:
        if t.predicate.value in (f"{_RDF}first", f"{_RDF}rest"):
            cells.setdefault(t.subject, []).append(t)
    todo = [t.object for t in schema if isinstance(t.object, ox.BlankNode)]
    reached: set[Any] = set()
    while todo:
        node = todo.pop()
        if node in reached or node not in cells:
            continue
        reached.add(node)
        for cell in cells[node]:
            schema.append(cell)
            if isinstance(cell.object, ox.BlankNode):
                todo.append(cell.object)
    return list(dict.fromkeys(schema))


def scoped_closure(
    merged: list[Any],
    graphs: dict[Any, list[Any]],
    *,
    mode: str = RDFS,
    findings: list[dict[str, Any]] | None = None,
) -> tuple[list[Any], dict[Any, list[Any]]]:
    """The inferred statements of the merge, and of each named graph (D7).

    ``merged`` is the default graph of the query Store (the scope merge, imports
    included); ``graphs`` maps each of the ontology's named graphs to its statements.
    A statement inferred for graph G follows from G's statements together with the
    schema of the whole dataset (the merge's schema statements, stated and inferred),
    and is not already in G. A statement that follows from the schema alone (for
    example ``ex:Book a rdfs:Class``) belongs to G only when G's own statements give
    it. Only the merge is checked against the input bound. ``findings`` (owl-rl)
    collects the inconsistencies of every run, each with ``graph`` (None = the merge).
    """

    def collect(graph: Any) -> list[dict[str, Any]] | None:
        if findings is None:
            return None
        sink: list[dict[str, Any]] = []
        sinks.append((graph, sink))
        return sink

    sinks: list[tuple[Any, list[dict[str, Any]]]] = []
    merge_inferred = closure(merged, mode=mode, findings=collect(None))
    per_graph: dict[Any, list[Any]] = {}
    if graphs:
        schema = _schema([*merged, *merge_inferred], mode)
        from_schema = {*schema, *closure(schema, mode=mode, check_limit=False)}
        for name, stated in graphs.items():
            own = set(stated)
            alone = closure(stated, mode=mode, check_limit=False, findings=collect(name))
            alone_set = set(alone)
            with_schema = closure([*stated, *schema], mode=mode, check_limit=False, findings=collect(name))
            candidates = dict.fromkeys([*with_schema, *alone])
            per_graph[name] = [t for t in candidates if t not in own and (t not in from_schema or t in alone_set)]
    if findings is not None:
        for graph, sink in sinks:
            findings.extend({**item, "graph": graph} for item in sink)
    return merge_inferred, per_graph


__all__ = [
    "DEFAULT_MODE",
    "ENGINE_IDS",
    "FINDING_KINDS",
    "INPUT_SETTINGS",
    "MODES",
    "MODE_LABELS",
    "OWL_RL",
    "RATIO_SETTING",
    "RDFS",
    "RDFS_ENGINE",
    "REGIME_IRIS",
    "RESERVED_MODES",
    "SIMPLE",
    "EntailmentError",
    "EntailmentFailed",
    "EntailmentPending",
    "EntailmentTooLarge",
    "EntailmentUnavailable",
    "axiomatic_closure",
    "closure",
    "engine_id",
    "input_bound",
    "is_trivial",
    "max_inferred_ratio",
    "max_input_triples",
    "max_owl_rl_input_triples",
    "owlrl_expand",
    "parse_mode",
    "scoped_closure",
]
