"""Direct RDFS closure engine (CTR-0170 v9, PRP-0211, UDR-0193 D11).

Computes EXACTLY what owlrl 7.6.2's ``RDFS_Semantics`` adds to a graph (with axiomatic
triples, without datatype axioms), about ten times faster: every statement is joined
once with the statements already known (semi-naive evaluation over hash indexes)
instead of re-running every rule over every statement until a round adds nothing.

owlrl's rule set, reproduced as it is:

- the RDF / RDFS axiomatic triples (owlrl's own list) and, for the container
  membership properties ``rdf:_1 .. rdf:_n`` (``n`` counted as owlrl counts it), their
  four axiomatic statements;
- rdfs4a / rdfs4b (``x rdf:type rdfs:Resource``) for the subjects and objects of the
  statements present in owlrl's FIRST cycle only (the input plus the axiomatic triples);
- rdf1, rdfs2, rdfs3, rdfs5, rdfs6, rdfs7, rdfs8, rdfs9, rdfs10, rdfs11, rdfs12, rdfs13.

The input never holds a literal or a triple term (``entailment.closure`` hides them
behind placeholder IRIs, UDR-0191 D4), so owlrl's literal rules have nothing to do and
are not reproduced. The equality with owlrl is proven by invariant on every fixture,
on generated graphs and on the W3C RDFS gate (UDR-0193 D11).
"""

from __future__ import annotations

from typing import Any

ENGINE_ID = "rdfs-direct-1"

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"


def _axiomatic_triples() -> list[tuple[str, str, str]]:
    """owlrl's RDFS axiomatic triples, as IRI strings (owlrl stays the reference)."""
    from owlrl.AxiomaticTriples import RDFS_Axiomatic_Triples

    return [(str(s), str(p), str(o)) for s, p, o in RDFS_Axiomatic_Triples]


def expand(work: Any, *, axioms: bool = True) -> None:
    """Add the RDFS closure of ``work``'s default graph to ``work`` (in place)."""
    import pyoxigraph as ox

    default_graph = ox.DefaultGraph()
    ids: dict[Any, int] = {}
    terms: list[Any] = []

    def term_id(term: Any) -> int:
        found = ids.get(term)
        if found is None:
            found = len(terms)
            ids[term] = found
            terms.append(term)
        return found

    def iri(value: str) -> int:
        return term_id(ox.NamedNode(value))

    input_quads = list(work.quads_for_pattern(None, None, None, default_graph))
    stated = [(term_id(q.subject), term_id(q.predicate), term_id(q.object)) for q in input_quads]

    rdf_type = iri(f"{_RDF}type")
    rdf_property = iri(f"{_RDF}Property")
    domain = iri(f"{_RDFS}domain")
    range_ = iri(f"{_RDFS}range")
    sub_property = iri(f"{_RDFS}subPropertyOf")
    sub_class = iri(f"{_RDFS}subClassOf")
    rdfs_class = iri(f"{_RDFS}Class")
    resource = iri(f"{_RDFS}Resource")
    literal = iri(f"{_RDFS}Literal")
    datatype = iri(f"{_RDFS}Datatype")
    member = iri(f"{_RDFS}member")
    membership = iri(f"{_RDFS}ContainerMembershipProperty")

    predicate_ids: set[int] = set()

    def _is_iri(p: int) -> bool:
        # A blank node never becomes a predicate (the caller hides those, UDR-0192 D3).
        if isinstance(terms[p], ox.NamedNode):
            predicate_ids.add(p)
            return True
        return False

    facts: set[tuple[int, int, int]] = set()
    # out[p][s] -> objects, inn[p][o] -> subjects
    out: dict[int, dict[int, set[int]]] = {}
    inn: dict[int, dict[int, set[int]]] = {}
    todo: list[tuple[int, int, int]] = []

    def add(s: int, p: int, o: int) -> None:
        fact = (s, p, o)
        if fact in facts or (p not in predicate_ids and not _is_iri(p)):
            return
        facts.add(fact)
        out.setdefault(p, {}).setdefault(s, set()).add(o)
        inn.setdefault(p, {}).setdefault(o, set()).add(s)
        todo.append(fact)

    def objects(s: int, p: int) -> list[int]:
        return list(out.get(p, {}).get(s, ()))

    def subjects(p: int, o: int) -> list[int]:
        return list(inn.get(p, {}).get(o, ()))

    def pairs(p: int) -> list[tuple[int, int]]:
        return [(s, o) for s, values in list(out.get(p, {}).items()) for o in list(values)]

    for fact in stated:
        add(*fact)
    if axioms:
        for s, p, o in _axiomatic_triples():
            add(iri(s), iri(p), iri(o))
        # owlrl's count, as it is: ``n`` advances once per STATEMENT using ``rdf:_n``,
        # and the search stops at the first ``rdf:_n`` used by none.
        uses: dict[str, int] = {}
        for q in input_quads:
            uses[q.predicate.value] = uses.get(q.predicate.value, 0) + 1
        highest, n = 0, 1
        while uses.get(f"{_RDF}_{n}", 0):
            count = uses[f"{_RDF}_{n}"]
            highest = n + count - 1
            n += count
        for n in range(1, highest + 1):
            item = iri(f"{_RDF}_{n}")
            add(item, rdf_type, rdf_property)
            add(item, domain, resource)
            add(item, range_, resource)
            add(item, rdf_type, membership)
    # rdfs4a / rdfs4b on owlrl's first cycle: the statements known before any rule ran.
    first_cycle = list(facts)
    resource_like = (ox.NamedNode, ox.BlankNode)
    for s, _p, o in first_cycle:
        if isinstance(terms[s], resource_like):
            add(s, rdf_type, resource)
        if isinstance(terms[o], resource_like):
            add(o, rdf_type, resource)

    while todo:
        s, p, o = todo.pop()
        add(p, rdf_type, rdf_property)  # rdf1
        # rdfs2 / rdfs3 / rdfs7 with this statement as the data statement
        for cls in objects(p, domain):
            add(s, rdf_type, cls)
        for cls in objects(p, range_):
            add(o, rdf_type, cls)
        for super_property in objects(p, sub_property):
            add(s, super_property, o)
        if p == domain:
            for x, _y in pairs(s):
                add(x, rdf_type, o)
        elif p == range_:
            for _x, y in pairs(s):
                add(y, rdf_type, o)
        elif p == sub_property:
            for x in objects(o, sub_property):  # rdfs5
                add(s, sub_property, x)
            for z in subjects(sub_property, s):
                add(z, sub_property, o)
            for x, y in pairs(s):  # rdfs7
                add(x, o, y)
        elif p == sub_class:
            for x in subjects(rdf_type, s):  # rdfs9
                add(x, rdf_type, o)
            for x in objects(o, sub_class):  # rdfs11
                add(s, sub_class, x)
            for z in subjects(sub_class, s):
                add(z, sub_class, o)
        elif p == rdf_type:
            for super_class in objects(o, sub_class):  # rdfs9
                add(s, rdf_type, super_class)
            if o == rdf_property:  # rdfs6
                add(s, sub_property, s)
            elif o == rdfs_class:  # rdfs8, rdfs10
                add(s, sub_class, resource)
                add(s, sub_class, s)
            elif o == membership:  # rdfs12
                add(s, sub_property, member)
            elif o == datatype:  # rdfs13
                add(s, sub_class, literal)

    given = set(stated)
    new = [fact for fact in facts if fact not in given]
    work.extend(ox.Quad(terms[s], terms[p], terms[o], default_graph) for s, p, o in new)


__all__ = ["ENGINE_ID", "expand"]
