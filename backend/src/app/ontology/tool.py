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
  ONTOLOGY_TOOL_MAX_TRIPLES with an explicit truncation notice.

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
    "block -- treat it as authoritative structured knowledge about the domain's "
    "entities and relationships."
)


def _catalog_summary() -> list[dict[str, str]]:
    return [{"id": e["id"], "name": e["name"], "description": e["description"]} for e in store.read_catalog()]


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
        graph = store.load_store(resolved["id"])
    except Exception as exc:
        logger.warning("query_ontology could not load %s", resolved["id"], exc_info=True)
        return f"Error: could not load ontology {resolved['id']}: {exc}"

    try:
        # CONSTRUCT-only lane (UDR-0084 D9): the answer is always a graph.
        sparql = await nl.generate_sparql(question, graph, construct_only=True)
    except ValueError as exc:
        return f"Error: could not translate the question into SPARQL: {exc}"
    except Exception as exc:
        logger.warning("query_ontology NL->SPARQL failed for %s", resolved["id"], exc_info=True)
        return f"Error: the SPARQL generation model call failed: {exc}"

    try:
        result = store.execute_query(graph, sparql, max_construct_triples=settings.ontology_tool_max_triples)
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
    return (
        f"Ontology: {resolved['name']} ({resolved['id']})\n"
        f"Generated SPARQL:\n{sparql}\n\n"
        f"Result (RDF Turtle):\n```turtle\n{turtle}\n```{notice}"
    )


__all__ = ["ONTOLOGY_TOOL_INSTRUCTION", "query_ontology"]
