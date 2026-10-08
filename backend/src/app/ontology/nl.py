"""Natural language -> SPARQL conversion (CTR-0171 /nl-query + CTR-0172, UDR-0084 D8).

ONE non-streaming completion built through the registry chokepoint
(``app.agui.agent_registry._build_chat_client``, CTR-0102) -- the Auto Session
Title / User Memory Extraction precedent -- so provider dispatch, prompt caching,
and DEMO_MODE are honored by construction.

The conversion prompt is SCHEMA-AWARE: the target ontology's prefixes, classes,
datatype properties, and object properties (with direction + cardinality) are
included so the model grounds its query in the actual vocabulary. The generated
SPARQL is ALWAYS surfaced to the caller and executed on the read-only lane
(CTR-0170 ``execute_query``), so a wrong translation is wrong -- never harmful.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.ontology.vocabulary import (
    CW,
    CW_CARDINALITY,
    OWL_CLASS,
    OWL_DATATYPE_PROPERTY,
    OWL_OBJECT_PROPERTY,
    OWL_ON_DATATYPE,
    RDF_PROPERTY,
    RDF_TYPE,
    RDFS_CLASS,
    RDFS_DATATYPE,
    RDFS_DOMAIN,
    RDFS_LABEL,
    RDFS_RANGE,
    RDFS_SUB_CLASS_OF,
    RDFS_SUB_PROPERTY_OF,
    classify_role,
    local_name,
    term_key,
    term_to_json,
)
from app.usage.ledger import append_helper_usage

logger = logging.getLogger(__name__)

# Bound the schema summary so a huge ontology cannot balloon the prompt.
_SCHEMA_CHAR_CAP = 6000
_QUESTION_CHAR_CAP = 2000
# Labels listed per class / property in the schema summary (UDR-0181 D5).
_LABELS_PER_TERM = 5

_NL_SYSTEM_PROMPT = (
    "You translate a natural-language question about an RDF ontology into ONE SPARQL 1.1 "
    "{form} query. Output ONLY the SPARQL query -- no prose, no markdown fence, no "
    "explanation. Ground every IRI in the provided schema; never invent terms. The data "
    "is a CONCEPT model: classes (owl:Class or rdfs:Class), datatype properties, and object "
    "properties (OWL-typed or rdf:Property) with rdfs:domain/rdfs:range and a cw:cardinality "
    "annotation, plus an rdfs:subClassOf class hierarchy. Queries see the asserted "
    "statements only (no inference): to include instances of subclasses, match "
    "rdf:type/rdfs:subClassOf* instead of rdf:type. Include the PREFIX declarations your "
    "query uses."
)

_NL_USER_TEMPLATE = "Ontology schema:\n{schema}\n\nQuestion:\n{question}\n\nSPARQL {form} query:"

# The first query-form keyword after the prolog decides the form.
_FORM_RE = re.compile(r"\b(SELECT|CONSTRUCT|ASK|DESCRIBE)\b", re.IGNORECASE)
# Cheap early rejection of update forms for a clear error message; the read-only
# executor (Store.query) would reject them anyway (UDR-0084 D7).
_UPDATE_RE = re.compile(r"\b(INSERT|DELETE|DROP|CLEAR|LOAD|CREATE|MOVE|COPY|ADD)\b", re.IGNORECASE)


def _label_order(literal: Any) -> tuple[int, str]:
    """Untagged first, then English, then by tag (the editor's pickLiteralIndex order)."""
    language = getattr(literal, "language", None) or ""
    if not language:
        return (0, "")
    if language == "en" or language.startswith("en-"):
        return (1, language)
    return (2, language)


_SCHEMA_TYPES = (OWL_CLASS, RDFS_CLASS, OWL_OBJECT_PROPERTY, OWL_DATATYPE_PROPERTY, RDF_PROPERTY)


def _roles(store: Any) -> dict[Any, str]:
    """Classes and properties in the Store, by the editor's rules (UDR-0185 D1 / D7).

    ``property`` marks an rdf:Property without a range (the editor's ``other``).
    """
    import pyoxigraph as ox

    named = ox.NamedNode
    declared = [
        *store.quads_for_pattern(None, named(OWL_ON_DATATYPE), None),
        *store.quads_for_pattern(None, named(RDF_TYPE), named(RDFS_DATATYPE)),
    ]
    datatypes = {term_key(term_to_json(q.subject)) for q in declared}
    subjects = {
        q.subject
        for type_iri in _SCHEMA_TYPES
        for q in store.quads_for_pattern(None, named(RDF_TYPE), named(type_iri))
        if isinstance(q.subject, ox.NamedNode)
    }
    roles: dict[Any, str] = {}
    for node in subjects:
        statements = [
            {"p": predicate, "o": term_to_json(q.object)}
            for predicate in (RDF_TYPE, RDFS_RANGE)
            for q in store.quads_for_pattern(node, named(predicate), None)
        ]
        role = classify_role({"type": "iri", "value": node.value}, statements, datatypes)
        if role == "other" and any(s["p"] == RDF_TYPE and s["o"].get("value") == RDF_PROPERTY for s in statements):
            role = "property"
        roles[node] = role
    return roles


def schema_summary(graph: Any) -> str:
    """A compact, prompt-friendly text summary of the ontology's vocabulary.

    EVERY label (with its language), every domain and every range is listed, not
    the first one in store order (UDR-0181 D5); the character cap still bounds the
    prompt. Labels (strings / language strings), IRIs and blank nodes are stored
    verbatim by the engine, so reading them from the Store shows the file.

    The Store's default graph is the merge of the graphs in scope (UDR-0182 D4); the
    named graphs are listed so generated SPARQL may use ``GRAPH`` (D5).
    """
    import pyoxigraph as ox

    store = graph.store
    named = ox.NamedNode

    def of(node: Any, predicate: str) -> list[Any]:
        return [q.object for q in store.quads_for_pattern(node, named(predicate), None)]

    def labels(node: Any) -> str:
        found = sorted((o for o in of(node, RDFS_LABEL) if isinstance(o, ox.Literal)), key=_label_order)
        if not found:
            return local_name(node.value)
        shown = [f'"{o.value}"@{o.language}' if o.language else f'"{o.value}"' for o in found[:_LABELS_PER_TERM]]
        return ", ".join(shown)

    def terms(node: Any, predicate: str) -> str:
        out = []
        for obj in sorted(of(node, predicate), key=str):  # stable prompt across loads
            if isinstance(obj, ox.NamedNode):
                out.append(f"<{obj.value}>")
            elif isinstance(obj, ox.BlankNode):
                out.append("(class expression)")
            else:
                out.append(str(getattr(obj, "value", obj)))
        return ", ".join(out) or "(none)"

    prefixes = {p["prefix"]: p["iri"] for p in (graph.document.get("prefixes") or [])}
    prefixes.setdefault("cw", CW)
    lines: list[str] = [f"PREFIX {name}: <{iri}>" for name, iri in prefixes.items()]
    graphs = list(getattr(graph, "graphs", None) or [])
    if graphs:
        lines.append(
            "Named graphs (the default graph below is the merge of the graphs in scope; "
            "use GRAPH <name> { ... } to ask about one graph): " + ", ".join(str(g) for g in graphs)
        )
    roles = _roles(store)

    def by_role(role: str) -> list[Any]:
        return sorted((node for node, r in roles.items() if r == role), key=str)

    def supers(node: Any) -> str:
        found = sorted((o for o in of(node, RDFS_SUB_PROPERTY_OF) if isinstance(o, ox.NamedNode)), key=str)
        return "; subPropertyOf " + ", ".join(f"<{o.value}>" for o in found) if found else ""

    # Order under the cap (UDR-0185 D7): classes, properties, then the hierarchy.
    lines.append("Classes:")
    lines.extend(f"- <{node.value}> label: {labels(node)}" for node in by_role("entity"))
    lines.append("Object properties (direction source -> target):")
    lines.extend(
        f"- <{node.value}> label: {labels(node)}; domain {terms(node, RDFS_DOMAIN)}; "
        f"range {terms(node, RDFS_RANGE)}; cardinality {terms(node, CW_CARDINALITY)}{supers(node)}"
        for node in by_role("object_property")
    )
    lines.append("Datatype properties:")
    lines.extend(
        f"- <{node.value}> label: {labels(node)}; domain {terms(node, RDFS_DOMAIN)}; "
        f"range {terms(node, RDFS_RANGE)}{supers(node)}"
        for node in by_role("datatype_property")
    )
    others = by_role("property")
    if others:
        lines.append("Other properties (rdf:Property without a range):")
        lines.extend(
            f"- <{node.value}> label: {labels(node)}; domain {terms(node, RDFS_DOMAIN)}{supers(node)}"
            for node in others
        )
    hierarchy = sorted(
        {
            (q.subject.value, q.object.value)
            for q in store.quads_for_pattern(None, named(RDFS_SUB_CLASS_OF), None)
            if isinstance(q.subject, ox.NamedNode) and isinstance(q.object, ox.NamedNode)
        }
    )
    if hierarchy:
        lines.append("Class hierarchy (subclass subClassOf superclass):")
        lines.extend(f"- <{sub}> subClassOf <{sup}>" for sub, sup in hierarchy)
    summary = "\n".join(lines)
    return summary[:_SCHEMA_CHAR_CAP]


def cached_schema_summary(graph: Any) -> str:
    """``schema_summary`` computed once per loaded graph and kept with it (UDR-0183 D2)."""
    summary = getattr(graph, "nl_summary", None)
    if summary is None:
        summary = schema_summary(graph)
        if hasattr(graph, "nl_summary"):
            graph.nl_summary = summary  # same value from any thread: a benign race
    return summary


def strip_code_fence(text: str) -> str:
    """Unwrap a ```sparql ... ``` fence the model may emit despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped.strip()


def query_form(sparql: str) -> str:
    """The query form keyword (upper-case), or '' when none is found."""
    match = _FORM_RE.search(sparql)
    return match.group(1).upper() if match else ""


def ensure_read_only(sparql: str, *, construct_only: bool = False) -> None:
    """Reject update forms (and, on the tool lane, non-CONSTRUCT forms) up front.

    Defense-in-depth only: the executor's ``Store.query()`` is structurally
    read-only regardless (UDR-0084 D7). Raises ``ValueError``.
    """
    form = query_form(sparql)
    if not form:
        head = sparql.strip().splitlines()[0][:80] if sparql.strip() else ""
        if _UPDATE_RE.search(sparql):
            raise ValueError("SPARQL UPDATE is not allowed: this is a read-only query lane")
        raise ValueError(f"Not a recognizable SPARQL query (starts with: {head!r})")
    if construct_only and form != "CONSTRUCT":
        raise ValueError(f"Only CONSTRUCT queries are allowed on this lane (got {form})")


async def generate_sparql(question: str, graph: Any, *, construct_only: bool = False) -> str:
    """NL -> SPARQL via one non-streaming completion through the chokepoint (D8)."""
    from agent_framework import Message
    from fastapi.concurrency import run_in_threadpool

    from app.agui.agent_registry import _build_chat_client
    from app.models_catalog import resolve_task_model

    form = "CONSTRUCT" if construct_only else "SELECT, CONSTRUCT, ASK, or DESCRIBE"
    model = resolve_task_model("ontology_nl")
    client = _build_chat_client(model)
    # The summary reads the Store: off the event loop (UDR-0183 D4), once per entry.
    schema = await run_in_threadpool(cached_schema_summary, graph)
    messages = [
        Message(role="system", contents=[_NL_SYSTEM_PROMPT.format(form=form)]),
        Message(
            role="user",
            contents=[
                _NL_USER_TEMPLATE.format(
                    schema=schema,
                    question=question[:_QUESTION_CHAR_CAP],
                    form="CONSTRUCT" if construct_only else "",
                )
            ],
        ),
    ]
    response = await client.get_response(messages, stream=False)
    # CTR-0200 (PRP-0158, UDR-0136 D5): record the pass MAF already totalled.
    append_helper_usage(
        purpose="ontology_nl_query",
        usage_details=getattr(response, "usage_details", None),
        model=model,
    )
    sparql = strip_code_fence(getattr(response, "text", "") or "")
    if not sparql:
        raise ValueError("The model produced no SPARQL query")
    ensure_read_only(sparql, construct_only=construct_only)
    return sparql


__all__ = [
    "cached_schema_summary",
    "ensure_read_only",
    "generate_sparql",
    "query_form",
    "schema_summary",
    "strip_code_fence",
]
