"""query_ontology MAF Function Tool (CTR-0172, PRP-0105, UDR-0084 D9).

A session-common agent tool (registered on the shared agent when
ONTOLOGY_ENABLED -- the manage_cron / manage_webhook precedent) that lets the
LLM answer questions from the operator's concept models:

- ``action="catalog"``: the catalog id + name + description list, so the model
  can identify the target ontology (the description is the disambiguation key).
- ``action="query"``: ontology id/name + a natural-language question. The tool
  converts NL -> SPARQL through the CTR-0102 chokepoint restricted to
  CONSTRUCT-ONLY (RESULT-1: a CONSTRUCT result is a graph, so "answer in RDF"
  holds by construction), executes on the read-only lane, and returns the
  result graph as Turtle inside a fenced ```turtle code block, capped at
  ONTOLOGY_TOOL_MAX_TRIPLES with an explicit truncation notice. For an ontology
  with named graphs the answer is TriG (```trig), each triple under the graphs
  that hold it, and ``graph`` narrows the question to one graph (CTR-0172 v3,
  UDR-0182 D4 / D5).

Errors (unknown ontology, un-convertible question, empty result) return short
diagnostic text and never raise into the run.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from pydantic import Field

from app.core.config import settings
from app.ontology import nl, store

logger = logging.getLogger(__name__)

ONTOLOGY_TOOL_INSTRUCTION = (
    "\n\n## Ontology Concept Models\n"
    "The operator maintains RDF concept models (ontologies) you can query with the "
    "query_ontology tool. Use action='catalog' first to see which ontologies exist and "
    "what they describe, then action='query' with the ontology id (or exact name) and a "
    "natural-language question. The answer arrives as RDF Turtle in a ```turtle code "
    "block (TriG in a ```trig block when the ontology has named graphs; pass graph to "
    "ask about one of them) -- treat it as authoritative structured knowledge about the "
    "domain's entities and relationships."
)


# Named graphs listed per ontology in the catalog summary (UDR-0182 D5).
_GRAPHS_PER_ONTOLOGY = 20


def _catalog_summary() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for e in store.read_catalog():
        item: dict[str, object] = {"id": e["id"], "name": e["name"], "description": e["description"]}
        if store.is_dataset(e):
            item["graphs"] = store.graph_names(e["id"])[:_GRAPHS_PER_ONTOLOGY]
        out.append(item)
    return out


def _resolve_entry(selector: str) -> dict[str, str] | list[dict[str, str]] | None:
    """Resolve id (exact) then name (case-insensitive, unique); list = ambiguous."""
    entries = store.read_catalog()
    wanted = (selector or "").strip()
    if not wanted:
        return None
    for entry in entries:
        if entry["id"] == wanted:
            return entry
    matches = [e for e in entries if e["name"].casefold() == wanted.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches
    return None


async def query_ontology(
    action: Annotated[str, Field(description="One of: catalog, query.")],
    ontology: Annotated[
        str,
        Field(description="For query: the target ontology id (preferred) or its exact name."),
    ] = "",
    question: Annotated[
        str,
        Field(description="For query: the natural-language question to answer from the ontology."),
    ] = "",
    graph: Annotated[
        str,
        Field(
            description="For query, optional: ask about ONE named graph (its IRI, or _:label), or "
            "'default' for the default graph. Empty = all graphs."
        ),
    ] = "",
) -> str:
    """Query the operator's RDF ontology concept models.

    Use action="catalog" to list the available ontologies (id, name,
    description), then action="query" with an ontology id/name plus a
    natural-language question. The answer is the matching subgraph as RDF
    Turtle in a fenced code block.
    """
    act = (action or "").strip().lower()

    if act == "catalog":
        return json.dumps({"ontologies": _catalog_summary()}, ensure_ascii=False)

    if act != "query":
        return "Error: 'action' must be one of catalog, query."

    resolved = _resolve_entry(ontology)
    if resolved is None:
        return json.dumps(
            {"error": f"No ontology matches {ontology!r}.", "ontologies": _catalog_summary()},
            ensure_ascii=False,
        )
    if isinstance(resolved, list):
        return json.dumps(
            {
                "error": f"Ontology name {ontology!r} is ambiguous; use the id.",
                "candidates": [{"id": e["id"], "name": e["name"]} for e in resolved],
            },
            ensure_ascii=False,
        )
    if not (question or "").strip():
        return "Error: 'question' is required for query."

    try:
        scope = store.parse_scope(graph.strip() or None)
    except ValueError as exc:
        return f"Error: {exc}"
    try:
        loaded = store.load_store(resolved["id"], scope)
    except Exception as exc:
        logger.warning("query_ontology could not load %s", resolved["id"], exc_info=True)
        return f"Error: could not load ontology {resolved['id']}: {exc}"

    try:
        # CONSTRUCT-only lane (UDR-0084 D9): the answer is always a graph.
        sparql = await nl.generate_sparql(question, loaded, construct_only=True)
    except ValueError as exc:
        return f"Error: could not translate the question into SPARQL: {exc}"
    except Exception as exc:
        logger.warning("query_ontology NL->SPARQL failed for %s", resolved["id"], exc_info=True)
        return f"Error: the SPARQL generation model call failed: {exc}"

    try:
        result = store.execute_query(loaded, sparql, max_construct_triples=settings.ontology_tool_max_triples)
    except ValueError as exc:
        return f"Error: the generated SPARQL failed to execute: {exc}\nGenerated query:\n{sparql}"

    turtle = result.get("turtle", "")
    if not turtle:
        return (
            f"Ontology '{resolved['name']}' ({resolved['id']}) returned no triples for this "
            f"question.\nGenerated SPARQL:\n{sparql}"
        )
    notice = ""
    if result.get("truncated"):
        notice = (
            f"\n(Note: the result was truncated to the first {settings.ontology_tool_max_triples} "
            "triples -- ask a narrower question for the rest.)"
        )
    # UDR-0181 D3: say what the query engine could not give back as written, so the
    # model never reports a count the file contradicts.
    for item in result.get("notices") or []:
        notice += f"\n(Note: {item['message']})"
    if result.get("format") == "trig":
        # UDR-0182 D5: each triple is written under the named graph(s) that hold it.
        return (
            f"Ontology: {resolved['name']} ({resolved['id']}), {store.scope_label(loaded.scope)}\n"
            f"Generated SPARQL:\n{sparql}\n\n"
            "Result (RDF TriG; each triple is under the named graph(s) that hold it, the rest is "
            f"in the default graph):\n```trig\n{turtle}\n```{notice}"
        )
    return (
        f"Ontology: {resolved['name']} ({resolved['id']})\n"
        f"Generated SPARQL:\n{sparql}\n\n"
        f"Result (RDF Turtle):\n```turtle\n{turtle}\n```{notice}"
    )


__all__ = ["ONTOLOGY_TOOL_INSTRUCTION", "query_ontology"]
