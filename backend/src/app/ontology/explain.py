"""Explain: why an inferred statement or an inconsistency holds (CTR-0170 v9, PRP-0211, UDR-0193 D14-D16).

owlrl records no derivation, and re-running the reasoner to find a minimal cause takes
dozens of runs. The rules themselves are fixed and public, and the stored result holds
every inferred statement (UDR-0193 D8), so one rule step backwards is a pattern match
on the stated + inferred statements:

- a STATED statement is a leaf ("stated in <graph>");
- a trivial or axiomatic statement (UDR-0191 D5, UDR-0192 D4: ``x a rdfs:Resource``,
  reflexive ``owl:sameAs``, inferred declarations, what the vocabulary alone gives) is a
  leaf "holds for every graph";
- otherwise a rule of ``RULES`` whose conclusion matches and whose premises ALL hold is
  applied, and each premise is explained in turn.

The search is depth-first, never uses a statement on its own path (no circular proof),
remembers what it proved, and stops at ``MAX_DEPTH`` levels or ``MAX_STATEMENTS``
statements (``truncated``). Every step is a rule instance whose premises were checked
to hold, so a proof is never a guess; when no proof is found the answer says so
(``found: false``). An inconsistency is explained from the rule its kind names
(``cax-dw``, ``prp-irp``, ...), with the finding's terms bound.

The rules are those of RDF 1.1 Semantics section 9 (RDFS) and of OWL 2 Profiles section
4.3, tables 4-9 (OWL 2 RL), as owlrl and the side rules apply them; datatype rules are
out of scope (literals are hidden from the reasoner, UDR-0191 D4).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"

MAX_DEPTH = 30
MAX_STATEMENTS = 200
MAX_STEPS = 20_000
MAX_OPTIONS_PER_RULE = 50

# Rule id -> what it says (shown on the rule chip).
RULE_NAMES: dict[str, str] = {
    "rdfs2": "the domain of a property types its subjects",
    "rdfs3": "the range of a property types its values",
    "rdfs5": "sub-property is transitive",
    "rdfs7": "a statement holds for every super-property",
    "rdfs9": "a member of a class is a member of its super-classes",
    "rdfs11": "sub-class is transitive",
    "rdfs12": "container membership properties are sub-properties of rdfs:member",
    "eq-sym": "owl:sameAs is symmetric",
    "eq-trans": "owl:sameAs is transitive",
    "eq-rep-s": "the same individual has the same statements (subject)",
    "eq-rep-p": "the same property has the same statements",
    "eq-rep-o": "the same individual has the same statements (value)",
    "prp-fp": "a functional property has one value",
    "prp-ifp": "an inverse-functional property identifies its subject",
    "prp-symp": "a symmetric property holds both ways",
    "prp-trp": "a transitive property chains",
    "prp-spo2": "a property chain implies its property",
    "prp-eqp1": "equivalent properties have the same statements",
    "prp-eqp2": "equivalent properties have the same statements",
    "prp-inv1": "an inverse property holds the other way",
    "prp-inv2": "an inverse property holds the other way",
    "prp-key": "members of a class with the same key are the same",
    "cls-int1": "a member of every class of an intersection is a member of it",
    "cls-int2": "a member of an intersection is a member of each class",
    "cls-uni": "a member of a class of a union is a member of the union",
    "cls-svf1": "a value of the given class makes a someValuesFrom member",
    "cls-svf2": "any value makes a someValuesFrom owl:Thing member",
    "cls-avf": "values of an allValuesFrom member are of the given class",
    "cls-hv1": "a hasValue member has the value",
    "cls-hv2": "having the value makes a hasValue member",
    "cls-maxc2": "max cardinality 1: two values are the same",
    "cls-maxqc3": "max qualified cardinality 1: two values are the same",
    "cls-maxqc4": "max qualified cardinality 1 (owl:Thing): two values are the same",
    "cls-oo": "the listed individuals are members of a oneOf class",
    "cax-eqc1": "equivalent classes have the same members",
    "cax-eqc2": "equivalent classes have the same members",
    "scm-eqc1": "equivalent classes are sub-classes of each other",
    "scm-eqc2": "mutual sub-classes are equivalent",
    "scm-eqp1": "equivalent properties are sub-properties of each other",
    "scm-eqp2": "mutual sub-properties are equivalent",
    "scm-dom1": "a domain implies its super-classes",
    "scm-dom2": "a sub-property has the domain of its super-property",
    "scm-rng1": "a range implies its super-classes",
    "scm-rng2": "a sub-property has the range of its super-property",
    "scm-hv": "hasValue restrictions on sub-properties are sub-classes",
    "scm-svf1": "someValuesFrom of a sub-class is a sub-class",
    "scm-svf2": "someValuesFrom on a sub-property is a sub-class",
    "scm-avf1": "allValuesFrom of a sub-class is a sub-class",
    "scm-avf2": "allValuesFrom on a sub-property is a super-class",
    "scm-int": "an intersection is a sub-class of each of its classes",
    "scm-uni": "each class of a union is a sub-class of it",
    # inconsistencies
    "cls-nothing2": "nothing is a member of owl:Nothing",
    "eq-diff1": "owl:sameAs and owl:differentFrom contradict",
    "eq-diff2": "owl:sameAs contradicts owl:AllDifferent",
    "cls-com": "a member of a class cannot be a member of its complement",
    "cax-dw": "disjoint classes have no common member",
    "cax-adc": "classes of owl:AllDisjointClasses have no common member",
    "prp-irp": "an irreflexive property never relates something to itself",
    "prp-asyp": "an asymmetric property never holds both ways",
    "prp-pdw": "disjoint properties never relate the same pair",
    "prp-adp": "properties of owl:AllDisjointProperties never relate the same pair",
    "prp-npa1": "a negative property assertion is violated",
    "prp-npa2": "a negative property assertion is violated",
    "cls-maxc1": "max cardinality 0: no value is allowed",
    "cls-maxqc1": "max qualified cardinality 0: no value of the class is allowed",
    "cls-maxqc2": "max qualified cardinality 0 (owl:Thing): no value is allowed",
    "literal-identity": "a functional property or key makes two different values the same",
}

RDFS_RULES = ("rdfs2", "rdfs3", "rdfs5", "rdfs7", "rdfs9", "rdfs11", "rdfs12")


class ExplainError(ValueError):
    """A request that cannot be explained (422 with ``code``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __reduce__(self) -> tuple[type, tuple[str, str]]:
        # It travels from a search worker back to the API process (UDR-0193 D3).
        return (ExplainError, (self.code, str(self)))


class _Truncated(Exception):
    pass


class _Context:
    """The statements a proof may use: stated + inferred of the merge, or of one named graph."""

    def __init__(self, graph: Any, mode: str, graph_name: Any) -> None:
        import pyoxigraph as ox

        from app.ontology import entailment

        self.ox = ox
        self.mode = mode
        self.graph_name = graph_name
        default_graph = ox.DefaultGraph()
        entailed = graph.entailed[mode]
        stated_store = graph.store
        if graph_name is None:
            self.facts = {q.triple for q in entailed.quads_for_pattern(None, None, None, default_graph)}
            self.stated = {q.triple for q in stated_store.quads_for_pattern(None, None, None, default_graph)}
        else:
            own = {q.triple for q in entailed.quads_for_pattern(None, None, None, graph_name)}
            merged = [q.triple for q in entailed.quads_for_pattern(None, None, None, default_graph)]
            schema = set(entailment._schema(merged, mode))
            self.facts = own | schema
            self.stated = {q.triple for q in stated_store.quads_for_pattern(None, None, None, graph_name)} | {
                q.triple for q in stated_store.quads_for_pattern(None, None, None, default_graph) if q.triple in schema
            }
        # The axiomatic statements (and what they alone give) hold in every graph; they
        # are trimmed from stored results, so they are added here for the rules to join on.
        self.facts |= entailment.axiomatic_closure(mode)
        self.provenance = graph.provenance
        self.described = {
            t.subject
            for t in self.stated
            if isinstance(t.subject, ox.NamedNode) and t.subject.value.startswith(entailment.BUILTIN_NAMESPACES)
        }
        self._out: dict[Any, dict[Any, list[Any]]] = {}
        self._inn: dict[Any, dict[Any, list[Any]]] = {}
        for t in self.facts:
            self._out.setdefault(t.predicate, {}).setdefault(t.subject, []).append(t.object)
            self._inn.setdefault(t.predicate, {}).setdefault(t.object, []).append(t.subject)
        self.first = {s: os[0] for s, os in self._out.get(self.iri(f"{RDF}first"), {}).items()}
        self.rest = {s: os[0] for s, os in self._out.get(self.iri(f"{RDF}rest"), {}).items()}

    def iri(self, value: str) -> Any:
        return self.ox.NamedNode(value)

    def triple(self, s: Any, p: Any, o: Any) -> Any:
        """A statement, or None when ``p`` cannot be a predicate (an anonymous property)."""
        if not isinstance(p, self.ox.NamedNode) or isinstance(s, self.ox.Literal):
            return None
        return self.ox.Triple(s, p, o)

    def trivial(self, t: Any) -> bool:
        from app.ontology import entailment

        return entailment.is_trivial(t, self.mode, self.described) or t in entailment.axiomatic_closure(self.mode)

    def holds(self, t: Any) -> bool:
        return t is not None and (t in self.facts or self.trivial(t))

    def objects(self, s: Any, p: Any) -> list[Any]:
        return list(self._out.get(p, {}).get(s, ()))

    def subjects(self, p: Any, o: Any) -> list[Any]:
        return list(self._inn.get(p, {}).get(o, ()))

    def pairs(self, p: Any) -> list[tuple[Any, Any]]:
        return [(s, o) for s, values in self._out.get(p, {}).items() for o in values]

    def members(self, head: Any) -> list[Any]:
        out: list[Any] = []
        seen: set[Any] = set()
        node = head
        while node in self.first and node not in seen:
            seen.add(node)
            out.append(self.first[node])
            node = self.rest.get(node)
        return out

    def values(self, u: Any, prop: Any) -> list[tuple[Any, list[Any]]]:
        """(v, premises) for ``u prop v`` -- also when ``prop`` is an anonymous inverse property
        (``[ owl:inverseOf q ]``, whose statements are never stored: ``v q u``)."""
        if isinstance(prop, self.ox.NamedNode):
            return [(v, [self.triple(u, prop, v)]) for v in self.objects(u, prop)]
        inverse = self.iri(f"{OWL}inverseOf")
        return [
            (v, [self.triple(prop, inverse, q), self.triple(v, q, u)])
            for q in self.objects(prop, inverse)
            if isinstance(q, self.ox.NamedNode)
            for v in self.subjects(q, u)
        ]

    def sources(self, v: Any, prop: Any) -> list[tuple[Any, list[Any]]]:
        """(u, premises) for ``u prop v`` (anonymous inverse properties as in ``values``)."""
        if isinstance(prop, self.ox.NamedNode):
            return [(u, [self.triple(u, prop, v)]) for u in self.subjects(prop, v)]
        inverse = self.iri(f"{OWL}inverseOf")
        return [
            (u, [self.triple(prop, inverse, q), self.triple(v, q, u)])
            for q in self.objects(prop, inverse)
            if isinstance(q, self.ox.NamedNode)
            for u in self.objects(v, q)
        ]

    def list_cells(self, head: Any) -> list[Any]:
        """The rdf:first / rdf:rest statements of a list (premises of list rules)."""
        cells: list[Any] = []
        seen: set[Any] = set()
        node = head
        first, rest = self.iri(f"{RDF}first"), self.iri(f"{RDF}rest")
        while node in self.first and node not in seen:
            seen.add(node)
            cells.append(self.triple(node, first, self.first[node]))
            if node in self.rest:
                cells.append(self.triple(node, rest, self.rest[node]))
            node = self.rest.get(node)
        return cells


def _candidates(ctx: _Context, t: Any) -> Iterator[tuple[str, list[Any]]]:
    """Rule instances (rule id, premises) that conclude ``t`` and whose premises all hold."""
    from app.ontology import entailment

    i = ctx.iri
    T = ctx.triple
    s, p, o = t.subject, t.predicate, t.object
    rdf_type, sco, spo = i(f"{RDF}type"), i(f"{RDFS}subClassOf"), i(f"{RDFS}subPropertyOf")
    domain, range_ = i(f"{RDFS}domain"), i(f"{RDFS}range")
    owl_rl = ctx.mode == entailment.OWL_RL
    same_as = i(f"{OWL}sameAs")

    def ok(premises: list[Any]) -> bool:
        return all(x is not None and ctx.holds(x) for x in premises)

    options: list[tuple[str, list[Any]]] = []

    def offer(rule: str, premises: list[Any]) -> None:
        if len([1 for r, _ in options if r == rule]) < MAX_OPTIONS_PER_RULE and ok(premises):
            options.append((rule, premises))

    # ---- RDFS (and the identical OWL 2 RL rules prp-dom, prp-rng, prp-spo1, cax-sco, scm-sco, scm-spo)
    if p == rdf_type:
        for prop, values in ctx._out.items():
            if s in values:
                for c in ctx.objects(prop, domain):
                    if c == o:
                        offer("rdfs2", [T(prop, domain, o), T(s, prop, values[s][0])])
        for prop in [q for q, c in ctx.pairs(range_) if c == o]:
            for x in ctx.subjects(prop, s):
                offer("rdfs3", [T(prop, range_, o), T(x, prop, s)])
                break
        for c in ctx.subjects(sco, o):
            if c != o and ctx.holds(T(s, rdf_type, c)):
                offer("rdfs9", [T(c, sco, o), T(s, rdf_type, c)])
    if p == sco:
        for d in ctx.objects(s, sco):
            if d not in (s, o) and ctx.holds(T(d, sco, o)):
                offer("rdfs11", [T(s, sco, d), T(d, sco, o)])
    if p == spo:
        for q in ctx.objects(s, spo):
            if q not in (s, o) and ctx.holds(T(q, spo, o)):
                offer("rdfs5", [T(s, spo, q), T(q, spo, o)])
        if o == i(f"{RDFS}member"):
            offer("rdfs12", [T(s, rdf_type, i(f"{RDFS}ContainerMembershipProperty"))])
    for sub in ctx.subjects(spo, p):
        if sub != p and ctx.holds(T(s, sub, o)):
            offer("rdfs7", [T(sub, spo, p), T(s, sub, o)])
    if not owl_rl:
        yield from options
        return

    # ---- OWL 2 RL: equality
    if p == same_as:
        offer("eq-sym", [T(o, same_as, s)])
        for y in ctx.objects(s, same_as):
            if y not in (s, o) and ctx.holds(T(y, same_as, o)):
                offer("eq-trans", [T(s, same_as, y), T(y, same_as, o)])
    for s2 in ctx.objects(s, same_as):
        if s2 != s and ctx.holds(T(s2, p, o)):
            offer("eq-rep-s", [T(s2, same_as, s), T(s2, p, o)])
    for p2 in ctx.objects(p, same_as):
        if p2 != p and ctx.holds(T(s, p2, o)):
            offer("eq-rep-p", [T(p2, same_as, p), T(s, p2, o)])
    for o2 in ctx.objects(o, same_as):
        if o2 != o and ctx.holds(T(s, p, o2)):
            offer("eq-rep-o", [T(o2, same_as, o), T(s, p, o2)])
    # ---- properties
    characteristics = set(ctx.objects(p, rdf_type))
    if p == same_as:
        for prop in ctx.subjects(rdf_type, i(f"{OWL}FunctionalProperty")):
            for x in ctx.subjects(prop, s):
                if ctx.holds(T(x, prop, o)):
                    offer("prp-fp", [T(prop, rdf_type, i(f"{OWL}FunctionalProperty")), T(x, prop, s), T(x, prop, o)])
        for prop in ctx.subjects(rdf_type, i(f"{OWL}InverseFunctionalProperty")):
            for y in ctx.objects(s, prop):
                if ctx.holds(T(o, prop, y)):
                    offer(
                        "prp-ifp",
                        [T(prop, rdf_type, i(f"{OWL}InverseFunctionalProperty")), T(s, prop, y), T(o, prop, y)],
                    )
        _key_and_cardinality(ctx, s, o, offer)
    if i(f"{OWL}SymmetricProperty") in characteristics:
        offer("prp-symp", [T(p, rdf_type, i(f"{OWL}SymmetricProperty")), T(o, p, s)])
    if i(f"{OWL}TransitiveProperty") in characteristics:
        for y in ctx.objects(s, p):
            if y not in (s, o) and ctx.holds(T(y, p, o)):
                offer("prp-trp", [T(p, rdf_type, i(f"{OWL}TransitiveProperty")), T(s, p, y), T(y, p, o)])
    for head in ctx.objects(p, i(f"{OWL}propertyChainAxiom")):
        chain = ctx.members(head)
        path = _chain_path(ctx, s, o, chain)
        if path is not None:
            offer("prp-spo2", [T(p, i(f"{OWL}propertyChainAxiom"), head), *ctx.list_cells(head), *path])
    equivalent = i(f"{OWL}equivalentProperty")
    for p1 in ctx.subjects(equivalent, p):
        if p1 != p:
            offer("prp-eqp1", [T(p1, equivalent, p), T(s, p1, o)])
    for p2 in ctx.objects(p, equivalent):
        if p2 != p:
            offer("prp-eqp2", [T(p, equivalent, p2), T(s, p2, o)])
    inverse = i(f"{OWL}inverseOf")
    for p1 in ctx.subjects(inverse, p):
        offer("prp-inv1", [T(p1, inverse, p), T(o, p1, s)])
    for p2 in ctx.objects(p, inverse):
        offer("prp-inv2", [T(p, inverse, p2), T(o, p2, s)])
    # ---- classes
    if p == rdf_type:
        _class_rules(ctx, s, o, offer)
    if p == sco:
        _schema_rules(ctx, s, o, offer)
    if p == equivalent and ctx.holds(T(o, spo, s)):
        offer("scm-eqp2", [T(s, spo, o), T(o, spo, s)])
    if p == spo:
        for q in (*ctx.objects(s, equivalent), *ctx.subjects(equivalent, s)):
            if q == o:
                offer("scm-eqp1", [T(s, equivalent, o)])
                offer("scm-eqp1", [T(o, equivalent, s)])
                break
    equivalent_class = i(f"{OWL}equivalentClass")
    if p == equivalent_class and ctx.holds(T(s, sco, o)) and ctx.holds(T(o, sco, s)):
        offer("scm-eqc2", [T(s, sco, o), T(o, sco, s)])
    if p in (domain, range_):
        rule1, rule2 = ("scm-dom1", "scm-dom2") if p == domain else ("scm-rng1", "scm-rng2")
        for c1 in ctx.objects(s, p):
            if c1 != o and ctx.holds(T(c1, sco, o)):
                offer(rule1, [T(s, p, c1), T(c1, sco, o)])
        for p2 in ctx.objects(s, spo):
            if p2 != s and ctx.holds(T(p2, p, o)):
                offer(rule2, [T(p2, p, o), T(s, spo, p2)])
    options.sort(key=lambda option: -sum(1 for x in option[1] if x in ctx.stated))
    yield from options


def _chain_path(ctx: _Context, start: Any, end: Any, chain: list[Any]) -> list[Any] | None:
    """Statements ``start p1 u2 ... un pn end`` along a property chain, or None."""
    if not chain:
        return None

    def walk(node: Any, position: int, path: list[Any], seen: set[Any]) -> list[Any] | None:
        prop = chain[position]
        for nxt in ctx.objects(node, prop):
            step = ctx.triple(node, prop, nxt)
            if position == len(chain) - 1:
                if nxt == end:
                    return [*path, step]
                continue
            if (nxt, position) in seen:
                continue
            seen.add((nxt, position))
            found = walk(nxt, position + 1, [*path, step], seen)
            if found is not None:
                return found
        return None

    return walk(start, 0, [], set())


def _key_and_cardinality(ctx: _Context, x: Any, y: Any, offer: Any) -> None:
    """prp-key, cls-maxc2, cls-maxqc3/4 for ``x owl:sameAs y``."""
    from app.ontology.entailment import _cardinality

    i, T = ctx.iri, ctx.triple
    rdf_type = i(f"{RDF}type")
    for cls, heads in ctx._out.get(i(f"{OWL}hasKey"), {}).items():
        if not ctx.holds(T(x, rdf_type, cls)) or not ctx.holds(T(y, rdf_type, cls)):
            continue
        for head in heads:
            premises = [
                T(cls, i(f"{OWL}hasKey"), head),
                *ctx.list_cells(head),
                T(x, rdf_type, cls),
                T(y, rdf_type, cls),
            ]
            complete = True
            for prop in ctx.members(head):
                shared = [z for z in ctx.objects(x, prop) if ctx.holds(T(y, prop, z))]
                if not shared:
                    complete = False
                    break
                premises += [T(x, prop, shared[0]), T(y, prop, shared[0])]
            if complete:
                offer("prp-key", premises)
    on_property, on_class = i(f"{OWL}onProperty"), i(f"{OWL}onClass")
    for predicate, rule in (
        (i(f"{OWL}maxCardinality"), "cls-maxc2"),
        (i(f"{OWL}maxQualifiedCardinality"), "cls-maxqc3"),
    ):
        for restriction, numbers in ctx._out.get(predicate, {}).items():
            if not any(_cardinality(n) == 1 for n in numbers):
                continue
            number = next(n for n in numbers if _cardinality(n) == 1)
            for prop in ctx.objects(restriction, on_property):
                for u in ctx.subjects(rdf_type, restriction):
                    if ctx.holds(T(u, prop, x)) and ctx.holds(T(u, prop, y)):
                        premises = [
                            T(restriction, predicate, number),
                            T(restriction, on_property, prop),
                            T(u, rdf_type, restriction),
                            T(u, prop, x),
                            T(u, prop, y),
                        ]
                        if rule == "cls-maxqc3":
                            for cls in ctx.objects(restriction, on_class):
                                if cls == i(f"{OWL}Thing"):
                                    offer("cls-maxqc4", [*premises, T(restriction, on_class, cls)])
                                elif ctx.holds(T(x, rdf_type, cls)) and ctx.holds(T(y, rdf_type, cls)):
                                    offer(
                                        rule,
                                        [
                                            *premises,
                                            T(restriction, on_class, cls),
                                            T(x, rdf_type, cls),
                                            T(y, rdf_type, cls),
                                        ],
                                    )
                        else:
                            offer(rule, premises)


def _class_rules(ctx: _Context, u: Any, c: Any, offer: Any) -> None:
    """The cls-* and cax-* rules concluding ``u rdf:type c``."""
    i, T = ctx.iri, ctx.triple
    rdf_type = i(f"{RDF}type")
    on_property = i(f"{OWL}onProperty")
    for head in ctx.objects(c, i(f"{OWL}intersectionOf")):
        members = ctx.members(head)
        if members and all(ctx.holds(T(u, rdf_type, m)) or ctx.trivial(T(u, rdf_type, m)) for m in members):
            offer(
                "cls-int1",
                [T(c, i(f"{OWL}intersectionOf"), head), *ctx.list_cells(head), *(T(u, rdf_type, m) for m in members)],
            )
    for whole, heads in ctx._out.get(i(f"{OWL}intersectionOf"), {}).items():
        for head in heads:
            if c in ctx.members(head) and ctx.holds(T(u, rdf_type, whole)):
                offer(
                    "cls-int2",
                    [T(whole, i(f"{OWL}intersectionOf"), head), *ctx.list_cells(head), T(u, rdf_type, whole)],
                )
    for head in ctx.objects(c, i(f"{OWL}unionOf")):
        for m in ctx.members(head):
            if ctx.holds(T(u, rdf_type, m)):
                offer("cls-uni", [T(c, i(f"{OWL}unionOf"), head), *ctx.list_cells(head), T(u, rdf_type, m)])
                break
    for y in ctx.objects(c, i(f"{OWL}someValuesFrom")):
        for prop in ctx.objects(c, on_property):
            for v, link in ctx.values(u, prop):
                if y == i(f"{OWL}Thing"):
                    offer("cls-svf2", [T(c, i(f"{OWL}someValuesFrom"), y), T(c, on_property, prop), *link])
                    break
                if ctx.holds(T(v, rdf_type, y)):
                    offer(
                        "cls-svf1",
                        [T(c, i(f"{OWL}someValuesFrom"), y), T(c, on_property, prop), *link, T(v, rdf_type, y)],
                    )
                    break
    for restriction in ctx.subjects(i(f"{OWL}allValuesFrom"), c):
        for prop in ctx.objects(restriction, on_property):
            for x, link in ctx.sources(u, prop):
                if ctx.holds(T(x, rdf_type, restriction)):
                    offer(
                        "cls-avf",
                        [
                            T(restriction, i(f"{OWL}allValuesFrom"), c),
                            T(restriction, on_property, prop),
                            T(x, rdf_type, restriction),
                            *link,
                        ],
                    )
    for y in ctx.objects(c, i(f"{OWL}hasValue")):
        for prop in ctx.objects(c, on_property):
            for v, link in ctx.values(u, prop):
                if v == y:
                    offer("cls-hv2", [T(c, i(f"{OWL}hasValue"), y), T(c, on_property, prop), *link])
    for head in ctx.objects(c, i(f"{OWL}oneOf")):
        if u in ctx.members(head):
            offer("cls-oo", [T(c, i(f"{OWL}oneOf"), head), *ctx.list_cells(head)])
    equivalent = i(f"{OWL}equivalentClass")
    for c1 in ctx.subjects(equivalent, c):
        if c1 != c and ctx.holds(T(u, rdf_type, c1)):
            offer("cax-eqc1", [T(c1, equivalent, c), T(u, rdf_type, c1)])
    for c2 in ctx.objects(c, equivalent):
        if c2 != c and ctx.holds(T(u, rdf_type, c2)):
            offer("cax-eqc2", [T(c, equivalent, c2), T(u, rdf_type, c2)])


def _schema_rules(ctx: _Context, c1: Any, c2: Any, offer: Any) -> None:
    """The scm-* rules concluding ``c1 rdfs:subClassOf c2``."""
    i, T = ctx.iri, ctx.triple
    sco, spo = i(f"{RDFS}subClassOf"), i(f"{RDFS}subPropertyOf")
    on_property = i(f"{OWL}onProperty")
    equivalent = i(f"{OWL}equivalentClass")
    if ctx.holds(T(c1, equivalent, c2)):
        offer("scm-eqc1", [T(c1, equivalent, c2)])
    if ctx.holds(T(c2, equivalent, c1)):
        offer("scm-eqc1", [T(c2, equivalent, c1)])
    for head in ctx.objects(c1, i(f"{OWL}intersectionOf")):
        if c2 in ctx.members(head):
            offer("scm-int", [T(c1, i(f"{OWL}intersectionOf"), head), *ctx.list_cells(head)])
    for head in ctx.objects(c2, i(f"{OWL}unionOf")):
        if c1 in ctx.members(head):
            offer("scm-uni", [T(c2, i(f"{OWL}unionOf"), head), *ctx.list_cells(head)])
    p1s, p2s = ctx.objects(c1, on_property), ctx.objects(c2, on_property)
    for predicate, rule_same, rule_sub in (
        (i(f"{OWL}hasValue"), None, "scm-hv"),
        (i(f"{OWL}someValuesFrom"), "scm-svf1", "scm-svf2"),
        (i(f"{OWL}allValuesFrom"), "scm-avf1", None),
    ):
        for y1 in ctx.objects(c1, predicate):
            for y2 in ctx.objects(c2, predicate):
                for p1 in p1s:
                    for p2 in p2s:
                        base = [
                            T(c1, predicate, y1),
                            T(c1, on_property, p1),
                            T(c2, predicate, y2),
                            T(c2, on_property, p2),
                        ]
                        if rule_same and p1 == p2 and ctx.holds(T(y1, sco, y2)):
                            offer(rule_same, [*base, T(y1, sco, y2)])
                        if rule_sub and y1 == y2 and ctx.holds(T(p1, spo, p2)):
                            offer(rule_sub, [*base, T(p1, spo, p2)])
    # scm-avf2: (c2 sco c1) <- c1 avf y on p1, c2 avf y on p2, p1 spo p2 -- here the conclusion is c1 sco c2
    for y in ctx.objects(c2, i(f"{OWL}allValuesFrom")):
        if T(c1, i(f"{OWL}allValuesFrom"), y) in ctx.facts:
            for p_super in ctx.objects(c1, on_property):
                for p_sub in ctx.objects(c2, on_property):
                    if ctx.holds(T(p_sub, spo, p_super)):
                        offer(
                            "scm-avf2",
                            [
                                T(c2, i(f"{OWL}allValuesFrom"), y),
                                T(c2, on_property, p_sub),
                                T(c1, i(f"{OWL}allValuesFrom"), y),
                                T(c1, on_property, p_super),
                                T(p_sub, spo, p_super),
                            ],
                        )


# ---- The search -----------------------------------------------------------------------


class _Prover:
    def __init__(self, ctx: _Context, deadline: float | None) -> None:
        self.ctx = ctx
        self.deadline = deadline
        self.memo: dict[Any, dict[str, Any]] = {}
        self.stack: set[Any] = set()
        self.steps = 0
        self.truncated = False

    def prove(self, t: Any, depth: int) -> dict[str, Any] | None:
        if t in self.memo:
            return self.memo[t]
        ctx = self.ctx
        if t in ctx.stated:
            node = {"kind": "stated", "statement": t, "premises": []}
            self.memo[t] = node
            return node
        if ctx.trivial(t):
            node = {"kind": "axiom", "statement": t, "premises": []}
            self.memo[t] = node
            return node
        if t not in ctx.facts or t in self.stack:
            return None
        self.steps += 1
        if depth >= MAX_DEPTH or len(self.memo) >= MAX_STATEMENTS or self.steps >= MAX_STEPS:
            raise _Truncated
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise _Truncated
        self.stack.add(t)
        try:
            for rule, premises in _candidates(ctx, t):
                if any(x in self.stack or x == t for x in premises):
                    continue
                proven: list[dict[str, Any]] = []
                for premise in premises:
                    node = self.prove(premise, depth + 1)
                    if node is None:
                        break
                    proven.append(node)
                else:
                    node = {"kind": "inferred", "statement": t, "rule": rule, "premises": proven}
                    self.memo[t] = node
                    return node
            return None
        finally:
            self.stack.discard(t)


def _inconsistency_premises(ctx: _Context, kind: str, terms: list[Any]) -> tuple[str, list[Any]] | None:
    """(rule, premises) of an inconsistency finding, its terms bound (the rule its kind names)."""
    i, T = ctx.iri, ctx.triple
    rdf_type = i(f"{RDF}type")
    nodes = [term for term in terms if not isinstance(term, str)]

    def first(*options: Any) -> Any:
        for option in options:
            if option in ctx.facts or ctx.trivial(option):
                return option
        return None

    if kind == "nothing_member" and nodes:
        return "cls-nothing2", [T(nodes[0], rdf_type, i(f"{OWL}Nothing"))]
    if kind == "same_and_different" and len(nodes) >= 2:
        x, y = nodes[0], nodes[1]
        same = first(T(x, i(f"{OWL}sameAs"), y), T(y, i(f"{OWL}sameAs"), x))
        different = first(T(x, i(f"{OWL}differentFrom"), y), T(y, i(f"{OWL}differentFrom"), x))
        if same is not None and different is not None:
            return "eq-diff1", [same, different]
        if same is not None:
            for group in ctx.subjects(rdf_type, i(f"{OWL}AllDifferent")):
                for predicate in (i(f"{OWL}members"), i(f"{OWL}distinctMembers")):
                    for head in ctx.objects(group, predicate):
                        members = ctx.members(head)
                        if x in members and y in members:
                            return "eq-diff2", [
                                same,
                                T(group, rdf_type, i(f"{OWL}AllDifferent")),
                                T(group, predicate, head),
                                *ctx.list_cells(head),
                            ]
        return None
    if kind in ("complement", "disjoint_classes") and len(nodes) >= 3:
        c1, c2, x = nodes[0], nodes[1], nodes[2]
        types = [T(x, rdf_type, c1), T(x, rdf_type, c2)]
        if kind == "complement":
            axiom = first(T(c1, i(f"{OWL}complementOf"), c2), T(c2, i(f"{OWL}complementOf"), c1))
            return ("cls-com", [axiom, *types]) if axiom is not None else None
        axiom = first(T(c1, i(f"{OWL}disjointWith"), c2), T(c2, i(f"{OWL}disjointWith"), c1))
        if axiom is not None:
            return "cax-dw", [axiom, *types]
        for group in ctx.subjects(rdf_type, i(f"{OWL}AllDisjointClasses")):
            for head in ctx.objects(group, i(f"{OWL}members")):
                members = ctx.members(head)
                if c1 in members and c2 in members:
                    return "cax-adc", [
                        T(group, rdf_type, i(f"{OWL}AllDisjointClasses")),
                        T(group, i(f"{OWL}members"), head),
                        *ctx.list_cells(head),
                        *types,
                    ]
        return None
    if kind == "irreflexive" and len(nodes) >= 2:
        x, p = nodes[0], nodes[1]
        return "prp-irp", [T(p, rdf_type, i(f"{OWL}IrreflexiveProperty")), T(x, p, x)]
    if kind == "asymmetric" and len(nodes) >= 3:
        p, x, y = nodes[0], nodes[1], nodes[2]
        return "prp-asyp", [T(p, rdf_type, i(f"{OWL}AsymmetricProperty")), T(x, p, y), T(y, p, x)]
    if kind == "disjoint_properties" and len(nodes) == 4:
        p1, p2, x, y = nodes
        axiom = first(T(p1, i(f"{OWL}propertyDisjointWith"), p2), T(p2, i(f"{OWL}propertyDisjointWith"), p1))
        return ("prp-pdw", [axiom, T(x, p1, y), T(x, p2, y)]) if axiom is not None else None
    if kind == "disjoint_properties" and len(nodes) >= 6:
        x, p1, y, _x, p2, _y = nodes[:6]
        for group in ctx.subjects(rdf_type, i(f"{OWL}AllDisjointProperties")):
            for head in ctx.objects(group, i(f"{OWL}members")):
                members = ctx.members(head)
                if p1 in members and p2 in members:
                    return "prp-adp", [
                        T(group, rdf_type, i(f"{OWL}AllDisjointProperties")),
                        T(group, i(f"{OWL}members"), head),
                        *ctx.list_cells(head),
                        T(x, p1, y),
                        T(x, p2, y),
                    ]
        return None
    if kind == "negative_assertion" and len(terms) >= 3:
        i1, p, i2 = terms[0], terms[1], terms[2]
        for npa in ctx.subjects(i(f"{OWL}sourceIndividual"), i1):
            if T(npa, i(f"{OWL}assertionProperty"), p) not in ctx.facts:
                continue
            for predicate, rule in ((i(f"{OWL}targetIndividual"), "prp-npa1"), (i(f"{OWL}targetValue"), "prp-npa2")):
                if T(npa, predicate, i2) in ctx.facts:
                    return rule, [
                        T(npa, i(f"{OWL}sourceIndividual"), i1),
                        T(npa, i(f"{OWL}assertionProperty"), p),
                        T(npa, predicate, i2),
                        T(i1, p, i2),
                    ]
        return None
    if kind == "max_cardinality" and len(nodes) >= 2:
        restriction, value = nodes[0], nodes[-1]
        qualified = len(nodes) >= 3
        predicate = i(f"{OWL}maxQualifiedCardinality") if qualified else i(f"{OWL}maxCardinality")
        numbers = ctx.objects(restriction, predicate)
        for prop in ctx.objects(restriction, i(f"{OWL}onProperty")):
            for u in ctx.subjects(rdf_type, restriction):
                if T(u, prop, value) in ctx.facts:
                    premises = [
                        *(T(restriction, predicate, n) for n in numbers[:1]),
                        T(restriction, i(f"{OWL}onProperty"), prop),
                        T(u, rdf_type, restriction),
                        T(u, prop, value),
                    ]
                    if qualified:
                        cls = nodes[1]
                        premises.append(T(restriction, i(f"{OWL}onClass"), cls))
                        if cls != i(f"{OWL}Thing"):
                            premises.append(T(value, rdf_type, cls))
                            return "cls-maxqc1", premises
                        return "cls-maxqc2", premises
                    return "cls-maxc1", premises
        return None
    if kind == "literal_identity" and len(terms) >= 2:
        v1, v2 = terms[0], terms[1]
        functional = i(f"{OWL}FunctionalProperty")
        for prop in ctx.subjects(rdf_type, functional):
            for x in ctx.subjects(prop, v1):
                if T(x, prop, v2) in ctx.facts:
                    return "literal-identity", [T(prop, rdf_type, functional), T(x, prop, v1), T(x, prop, v2)]
        return None
    return None


# ---- Entry points ---------------------------------------------------------------------


def _normalized(term: Any) -> Any:
    """A term as the query Store holds it (literal values are re-encoded by the Store)."""
    import pyoxigraph as ox

    if not isinstance(term, ox.Literal):
        return term
    probe = ox.Store()
    probe.add(ox.Quad(ox.NamedNode("urn:x-cw:s"), ox.NamedNode("urn:x-cw:p"), term, ox.DefaultGraph()))
    return next(iter(probe)).object


def _target(graph: Any, statement: Any) -> tuple[Any, Any]:
    """(triple, graph name or None) of the request's statement (Term JSON or a Turtle triple)."""
    import pyoxigraph as ox

    from app.ontology.vocabulary import ProjectionError, graph_from_json, term_from_json

    if isinstance(statement, str):
        prefixes = "".join(f"@prefix {p['prefix']}: <{p['iri']}> .\n" for p in graph.document.get("prefixes") or [])
        text = statement.strip()
        if not text.endswith("."):
            text += " ."
        try:
            parsed = [q.triple for q in ox.parse((prefixes + text).encode("utf-8"), format=ox.RdfFormat.TURTLE)]
        except (SyntaxError, ValueError) as exc:
            raise ExplainError("explain_invalid", f"Not one Turtle statement: {exc}") from exc
        if len(parsed) != 1:
            raise ExplainError("explain_invalid", f"Give exactly one statement (got {len(parsed)}).")
        t = parsed[0]
        return ox.Triple(t.subject, t.predicate, _normalized(t.object)), None
    if not isinstance(statement, dict):
        raise ExplainError("explain_invalid", "The statement must be {s, p, o, graph?} or one Turtle triple.")
    try:
        subject = term_from_json(statement.get("s"), "s")
        predicate = term_from_json(statement.get("p"), "p")
        obj = term_from_json(statement.get("o"), "o")
        graph_name = statement.get("graph")
        name = graph_from_json(graph_name, "graph") if graph_name is not None else None
    except ProjectionError as exc:
        raise ExplainError("explain_invalid", str(exc)) from exc
    if isinstance(name, ox.DefaultGraph):
        name = None
    return ox.Triple(subject, predicate, _normalized(obj)), name


def explain(
    graph: Any, mode: str, *, statement: Any = None, finding: Any = None, deadline: float | None = None
) -> dict[str, Any]:
    """Explain one statement or one stored finding (key) of ``graph`` in ``mode``.

    The entailed copy of ``mode`` must exist (``store.ensure_entailment``). Raises
    ``ExplainError`` (``explain_not_inferred``, ``explain_invalid``, ``explain_unknown_finding``).
    """
    from app.ontology import entailment
    from app.ontology import entailment_results as results

    if mode not in graph.entailed:
        raise ExplainError("explain_invalid", f"{entailment.MODE_LABELS.get(mode, mode)} is not available here.")
    target: dict[str, Any]
    if finding is not None:
        record = results.lookup(graph.key, mode) or {}
        item = next(
            (f for f in record.get("findings") or [] if results.finding_key(f) == finding),
            None,
        )
        if item is None:
            raise ExplainError("explain_unknown_finding", "No such inconsistency in the current version.")
        decoded = results.decode_finding(item)
        ctx = _Context(graph, mode, decoded.get("graph"))
        match = _inconsistency_premises(ctx, decoded["kind"], decoded["terms"])
        prover = _Prover(ctx, deadline)
        root: dict[str, Any] | None = None
        if match is not None:
            rule, premises = match
            proven: list[dict[str, Any]] = []
            try:
                for premise in premises:
                    node = prover.prove(premise, 1)
                    if node is None:
                        proven = []
                        break
                    proven.append(node)
            except _Truncated:
                prover.truncated = True
                proven = []
            if proven:
                root = {"kind": "inconsistency", "statement": None, "rule": rule, "premises": proven}
        target = {"finding": finding, "kind": decoded["kind"], "message": item.get("message") or decoded["template"]}
        return _answer(graph, mode, ctx, root, target, prover.truncated)
    triple, graph_name = _target(graph, statement)
    ctx = _Context(graph, mode, graph_name)
    if triple not in ctx.facts and triple not in ctx.stated and not ctx.trivial(triple):
        raise ExplainError(
            "explain_not_inferred",
            f"This statement is neither stated nor inferred under {entailment.MODE_LABELS[mode]}.",
        )
    prover = _Prover(ctx, deadline)
    try:
        root = prover.prove(triple, 0)
    except _Truncated:
        root = None
        prover.truncated = True
    return _answer(graph, mode, ctx, root, {"statement": triple}, prover.truncated)


def _answer(
    graph: Any, mode: str, ctx: _Context, root: dict[str, Any] | None, target: dict[str, Any], truncated: bool
) -> dict[str, Any]:
    """The proof as {target, found, root, nodes, stated, truncated, notices} (restored terms)."""
    import pyoxigraph as ox

    from app.ontology.lexical import restore_term
    from app.ontology.store import _construct_prefixes, _display
    from app.ontology.vocabulary import term_to_json

    prefixes = _construct_prefixes(graph.document)
    nodes: dict[str, dict[str, Any]] = {}
    ids: dict[int, str] = {}
    stated: list[dict[str, Any]] = []
    seen_stated: set[Any] = set()

    def restored(term: Any) -> Any:
        return restore_term(graph.index, term)[0]

    def text(t: Any) -> str:
        return " ".join(_display(restored(term), prefixes) for term in (t.subject, t.predicate, t.object))

    def statement_json(t: Any) -> dict[str, Any]:
        out = {
            "s": term_to_json(restored(t.subject)),
            "p": term_to_json(t.predicate),
            "o": term_to_json(restored(t.object)),
        }
        out["graph"] = None if ctx.graph_name is None else term_to_json(ctx.graph_name)
        return out

    def where(t: Any) -> list[Any]:
        if ctx.graph_name is not None:
            return [term_to_json(ctx.graph_name)]
        if graph.provenance is None:
            return [None]
        return [
            None if isinstance(g, ox.DefaultGraph) else term_to_json(g)
            for g in graph.provenance.get(t) or [ox.DefaultGraph()]
        ]

    def visit(node: dict[str, Any]) -> str:
        found = ids.get(id(node))
        if found is not None:
            return found
        node_id = f"n{len(ids)}"
        ids[id(node)] = node_id
        t = node.get("statement")
        entry: dict[str, Any] = {
            "id": node_id,
            "kind": node["kind"],
            "statement": statement_json(t) if t is not None else None,
            "text": text(t) if t is not None else None,
            "rule": node.get("rule"),
            "rule_name": RULE_NAMES.get(node.get("rule") or ""),
            "premises": [],
        }
        nodes[node_id] = entry
        entry["premises"] = [visit(child) for child in node["premises"]]
        if node["kind"] == "stated" and t is not None and t not in seen_stated:
            seen_stated.add(t)
            stated.extend({**statement_json(t), "graph": g, "text": text(t)} for g in where(t))
        return node_id

    root_id = visit(root) if root is not None else None
    target_json = dict(target)
    if "statement" in target_json and target_json["statement"] is not None:
        t = target_json["statement"]
        target_json = {"statement": statement_json(t), "text": text(t)}
    from app.ontology import entailment

    notices: list[dict[str, Any]] = []
    if root is None:
        notices.append(
            {
                "code": "explain_not_found" if not truncated else "explain_truncated",
                "message": (
                    "The search for a proof stopped at its bounds; the statement may need a longer chain."
                    if truncated
                    else "No chain of rules was found for this statement within the rules this product explains."
                ),
            }
        )
    return {
        "mode": mode,
        "mode_label": entailment.MODE_LABELS[mode],
        "target": target_json,
        "found": root is not None,
        "root": root_id,
        "nodes": nodes,
        "stated": stated,
        "truncated": truncated,
        "notices": notices,
    }


def render_text(result: dict[str, Any], *, ontology: str = "") -> str:
    """The agent tool's text form of an explanation (UDR-0193 D16)."""
    lines: list[str] = []
    target = result.get("target") or {}
    heading = target.get("text") or target.get("message") or "the statement"
    lines.append(f"Why: {heading} ({result.get('mode_label')}{', ' + ontology if ontology else ''})")
    if not result.get("found"):
        lines.extend(notice["message"] for notice in result.get("notices") or [])
        return "\n".join(lines)
    nodes = result["nodes"]
    shown: set[str] = set()

    def walk(node_id: str, depth: int) -> None:
        node = nodes[node_id]
        pad = "  " * depth
        label = node.get("text") or "inconsistency"
        if node["kind"] == "stated":
            lines.append(f"{pad}- {label}  (stated)")
            return
        if node["kind"] == "axiom":
            lines.append(f"{pad}- {label}  (holds for every graph)")
            return
        if node_id in shown:
            lines.append(f"{pad}- {label}  (see above)")
            return
        shown.add(node_id)
        lines.append(f"{pad}- {label}  [{node.get('rule')}: {node.get('rule_name') or ''}]")
        for child in node["premises"]:
            walk(child, depth + 1)

    walk(result["root"], 0)
    stated = result.get("stated") or []
    lines.append(f"Rests on {len(stated)} stated statement(s):")
    lines.append("```turtle")
    lines.extend(f"{item['text']} ." for item in stated)
    lines.append("```")
    if result.get("truncated"):
        lines.append("(The proof was cut at its bounds.)")
    return "\n".join(lines)


__all__ = [
    "MAX_DEPTH",
    "MAX_STATEMENTS",
    "RULE_NAMES",
    "ExplainError",
    "explain",
    "render_text",
]
