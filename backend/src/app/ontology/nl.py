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

from app.core.config import settings
from app.ontology.vocabulary import (
    CW,
    CW_CARDINALITY,
    OWL_CLASS,
    OWL_DATATYPE_PROPERTY,
    OWL_OBJECT_PROPERTY,
    RDF_TYPE,
    RDFS_DOMAIN,
    RDFS_LABEL,
    RDFS_RANGE,
    local_name,
)

logger = logging.getLogger(__name__)

# Bound the schema summary so a huge ontology cannot balloon the prompt.
_SCHEMA_CHAR_CAP = 6000
_QUESTION_CHAR_CAP = 2000

_NL_SYSTEM_PROMPT = (
    "You translate a natural-language question about an RDF ontology into ONE SPARQL 1.1 "
    "{form} query. Output ONLY the SPARQL query -- no prose, no markdown fence, no "
    "explanation. Ground every IRI in the provided schema; never invent terms. The data "
    "is a CONCEPT model: classes (owl:Class), datatype properties, and object properties "
    "with rdfs:domain/rdfs:range and a cw:cardinality annotation. Include the PREFIX "
    "declarations your query uses."
)

_NL_USER_TEMPLATE = "Ontology schema:\n{schema}\n\nQuestion:\n{question}\n\nSPARQL {form} query:"

# The first query-form keyword after the prolog decides the form.
_FORM_RE = re.compile(r"\b(SELECT|CONSTRUCT|ASK|DESCRIBE)\b", re.IGNORECASE)
# Cheap early rejection of update forms for a clear error message; the read-only
# executor (Store.query) would reject them anyway (UDR-0084 D7).
_UPDATE_RE = re.compile(r"\b(INSERT|DELETE|DROP|CLEAR|LOAD|CREATE|MOVE|COPY|ADD)\b", re.IGNORECASE)


def schema_summary(store: Any) -> str:
    """A compact, prompt-friendly text summary of the ontology's vocabulary."""
    import pyoxigraph as ox

    named = ox.NamedNode

    def first_value(subject: Any, predicate: str) -> str:
        for quad in store.quads_for_pattern(subject, named(predicate), None):
            value = getattr(quad.object, "value", None)
            if value is not None:
                return str(value)
        return ""

    def typed_subjects(type_iri: str) -> list[Any]:
        return sorted(
            (q.subject for q in store.quads_for_pattern(None, named(RDF_TYPE), named(type_iri))),
            key=str,
        )

    lines: list[str] = [f"PREFIX cw: <{CW}>"]
    lines.append("Classes:")
    for node in typed_subjects(OWL_CLASS):
        label = first_value(node, RDFS_LABEL) or local_name(str(getattr(node, "value", node)))
        lines.append(f"- <{node.value}> label: {label}")
    lines.append("Object properties (direction source -> target):")
    for node in typed_subjects(OWL_OBJECT_PROPERTY):
        domain = first_value(node, RDFS_DOMAIN)
        range_ = first_value(node, RDFS_RANGE)
        cardinality = first_value(node, CW_CARDINALITY)
        label = first_value(node, RDFS_LABEL) or local_name(node.value)
        lines.append(f"- <{node.value}> label: {label}; domain <{domain}>; range <{range_}>; cardinality {cardinality}")
    lines.append("Datatype properties:")
    for node in typed_subjects(OWL_DATATYPE_PROPERTY):
        domain = first_value(node, RDFS_DOMAIN)
        range_ = first_value(node, RDFS_RANGE)
        label = first_value(node, RDFS_LABEL) or local_name(node.value)
        lines.append(f"- <{node.value}> label: {label}; domain <{domain}>; range <{range_}>")
    summary = "\n".join(lines)
    return summary[:_SCHEMA_CHAR_CAP]


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


def _default_model() -> str:
    """Resolve the fallback conversion model (honors DEMO_MODE)."""
    from app.demo import is_demo_mode

    if is_demo_mode():
        from app.demo import resolve_demo_models

        models = resolve_demo_models()
        return models[0] if models else "chatwalaau-demo"
    from app import providers

    resolved = providers.resolve_models()
    return resolved[0][0] if resolved else ""


async def generate_sparql(question: str, store: Any, *, construct_only: bool = False) -> str:
    """NL -> SPARQL via one non-streaming completion through the chokepoint (D8)."""
    from agent_framework import Message

    from app.agui.agent_registry import _build_chat_client

    form = "CONSTRUCT" if construct_only else "SELECT, CONSTRUCT, ASK, or DESCRIBE"
    model = settings.ontology_nl_model.strip() or _default_model()
    client = _build_chat_client(model)
    messages = [
        Message(role="system", contents=[_NL_SYSTEM_PROMPT.format(form=form)]),
        Message(
            role="user",
            contents=[
                _NL_USER_TEMPLATE.format(
                    schema=schema_summary(store),
                    question=question[:_QUESTION_CHAR_CAP],
                    form="CONSTRUCT" if construct_only else "",
                )
            ],
        ),
    ]
    response = await client.get_response(messages, stream=False)
    sparql = strip_code_fence(getattr(response, "text", "") or "")
    if not sparql:
        raise ValueError("The model produced no SPARQL query")
    ensure_read_only(sparql, construct_only=construct_only)
    return sparql


__all__ = ["ensure_read_only", "generate_sparql", "query_form", "schema_summary", "strip_code_fence"]
