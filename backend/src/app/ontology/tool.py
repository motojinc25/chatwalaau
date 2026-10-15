"""ontology_query MAF Function Tool (CTR-0172, PRP-0105, UDR-0084 D9; renamed from
query_ontology by PRP-0209 / UDR-0191 D9 -- ontology tools are named ``ontology_*``).

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
- ``entailment``: ``simple`` (default, stated statements only), ``rdfs`` (CTR-0172
  v5, UDR-0191 D9) or ``owl-rl`` (CTR-0172 v6, UDR-0192 D10): the inferred triples come
  in a second block labelled as inferred and not stated in any file. An ``owl-rl``
  answer from an inconsistent ontology starts with the inconsistencies (UDR-0192 D5).

Errors (unknown ontology, un-convertible question, empty result) return short
diagnostic text and never raise into the run.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from app.core.config import settings
from app.ontology import entailment as entailment_modes
from app.ontology import nl, store

logger = logging.getLogger(__name__)

ONTOLOGY_TOOL_INSTRUCTION = (
    "\n\n## Ontology Concept Models\n"
    "The operator maintains RDF concept models (ontologies) you can query with the "
    "ontology_query tool. Use action='catalog' first to see which ontologies exist and "
    "what they describe, then action='query' with the ontology id (or exact name) and a "
    "natural-language question. The answer arrives as RDF Turtle in a ```turtle code "
    "block (TriG in a ```trig block when the ontology has named graphs; pass graph to "
    "ask about one of them) -- treat it as authoritative structured knowledge about the "
    "domain's entities and relationships. An ontology's answer also covers the ontologies "
    "it imports (listed as imports in the catalog) unless the operator turned that off. "
    "Pass entailment='rdfs' for questions about class membership, class or property "
    "hierarchies, or what follows from domains, ranges and sub-properties: the answer then "
    "adds a second block of INFERRED triples (RDFS entailment, not stated in any file) -- "
    "when you use them, say that they are inferred. Pass entailment='owl-rl' only when the "
    "question depends on OWL: transitive, symmetric or inverse properties, equivalent classes "
    "or properties, owl:sameAs, restrictions, intersections or unions, keys or functional "
    "properties (OWL 2 RL entailment; slower and limited to smaller ontologies). If an owl-rl "
    "answer reports an inconsistent ontology, give the answer together with that warning."
)


# Named graphs listed per ontology in the catalog summary (UDR-0182 D5).
_GRAPHS_PER_ONTOLOGY = 20


def _catalog_summary() -> list[dict[str, object]]:
    from app.ontology.imports import Resolver, direct_import_names

    resolver = Resolver()
    out: list[dict[str, object]] = []
    for e in store.read_catalog():
        item: dict[str, object] = {"id": e["id"], "name": e["name"], "description": e["description"]}
        if store.is_dataset(e):
            item["graphs"] = store.graph_names(e["id"])[:_GRAPHS_PER_ONTOLOGY]
        # UDR-0189 D10: which ontologies this one extends (owl:imports resolved in the catalog).
        imported = direct_import_names(e["id"], resolver)
        if imported:
            item["imports"] = imported
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


async def ontology_query(
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
    entailment: Annotated[
        str,
        Field(
            description="For query, optional: 'simple' (default) answers from the stated triples only; "
            "'rdfs' also applies RDFS entailment (subclass / subproperty hierarchies, domains, ranges); "
            "'owl-rl' applies OWL 2 RL entailment on top (transitive / symmetric / inverse properties, "
            "equivalence, owl:sameAs, restrictions, keys; slower, smaller ontologies only). Inferred "
            "triples come in a separate block."
        ),
    ] = "simple",
) -> str:
    """Query the operator's RDF ontology concept models.

    Use action="catalog" to list the available ontologies (id, name,
    description), then action="query" with an ontology id/name plus a
    natural-language question. The answer is the matching subgraph as RDF
    Turtle in a fenced code block; with entailment="rdfs" or "owl-rl" the
    inferred triples follow in a second block.
    """
    act = (action or "").strip().lower()

    if act == "catalog":
        return json.dumps({"ontologies": await run_in_threadpool(_catalog_summary)}, ensure_ascii=False)

    if act != "query":
        return "Error: 'action' must be one of catalog, query."

    resolved = _resolve_entry(ontology)
    if resolved is None:
        return json.dumps(
            {"error": f"No ontology matches {ontology!r}.", "ontologies": await run_in_threadpool(_catalog_summary)},
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
        mode = entailment_modes.parse_mode(entailment)
    except ValueError as exc:
        return f"Error: {exc}"
    try:
        # Off the event loop, from the query cache when the revision is cached (UDR-0183 D1 / D4).
        loaded = await run_in_threadpool(store.load_store, resolved["id"], scope)
    except Exception as exc:
        logger.warning("ontology_query could not load %s", resolved["id"], exc_info=True)
        return f"Error: could not load ontology {resolved['id']}: {exc}"

    try:
        # CONSTRUCT-only lane (UDR-0084 D9): the answer is always a graph.
        sparql = await nl.generate_sparql(question, loaded, construct_only=True)
    except ValueError as exc:
        return f"Error: could not translate the question into SPARQL: {exc}"
    except Exception as exc:
        logger.warning("ontology_query NL->SPARQL failed for %s", resolved["id"], exc_info=True)
        return f"Error: the SPARQL generation model call failed: {exc}"

    try:
        result = await run_in_threadpool(
            store.execute_query,
            loaded,
            sparql,
            max_construct_triples=settings.ontology_tool_max_triples,
            entailment=mode,
        )
    except entailment_modes.EntailmentError as exc:
        return f"Error: {exc}"
    except ValueError as exc:
        return f"Error: the generated SPARQL failed to execute: {exc}\nGenerated query:\n{sparql}"

    turtle = result.get("turtle", "")
    inferred = result.get("inferred_turtle", "")
    notice = ""
    if result.get("truncated"):
        notice = (
            f"\n(Note: the result was truncated to the first {settings.ontology_tool_max_triples} "
            "triples -- ask a narrower question for the rest.)"
        )
    # UDR-0181 D3: say what the query engine could not give back as written, so the
    # model never reports a count the file contradicts.
    warning = ""
    for item in result.get("notices") or []:
        if item.get("code") == "entailment_inconsistent":
            # UDR-0192 D5: the inconsistencies come BEFORE the triples.
            lines = [
                f"- {finding['message']}" + (f" (graph {finding['graph']})" if finding.get("graph") else "")
                for finding in item.get("items") or []
            ]
            more = item.get("count", 0) - len(lines)
            if more > 0:
                lines.append(f"- ... and {more} more")
            warning = (
                f"Inconsistent ontology (OWL 2 RL): {item.get('count', len(lines))} finding(s)\n"
                + "\n".join(lines)
                + "\nResults come from an inconsistent ontology; present them with this warning.\n\n"
            )
            continue
        notice += f"\n(Note: {item['message']})"
    if not turtle and not inferred:
        return warning + (
            f"Ontology '{resolved['name']}' ({resolved['id']}) returned no triples for this "
            f"question.\nGenerated SPARQL:\n{sparql}"
        )
    # UDR-0191 D9: the inferred triples in their own block, labelled as not stated.
    inferred_block = ""
    if mode != entailment_modes.SIMPLE:
        label = entailment_modes.MODE_LABELS[mode]
        inferred_block = (
            f"\n\nInferred ({label}; not stated in any ontology file):\n```turtle\n{inferred}\n```"
            if inferred
            else f"\n\nInferred ({label}): no further triples."
        )
    if result.get("format") == "trig":
        # UDR-0182 D5: each triple is written under the named graph(s) that hold it.
        return warning + (
            f"Ontology: {resolved['name']} ({resolved['id']}), {store.scope_label(loaded.scope)}\n"
            f"Generated SPARQL:\n{sparql}\n\n"
            "Result (RDF TriG; each triple is under the named graph(s) that hold it, the rest is "
            f"in the default graph):\n```trig\n{turtle}\n```{inferred_block}{notice}"
        )
    return warning + (
        f"Ontology: {resolved['name']} ({resolved['id']})\n"
        f"Generated SPARQL:\n{sparql}\n\n"
        f"Result (RDF Turtle):\n```turtle\n{turtle}\n```{inferred_block}{notice}"
    )


__all__ = ["ONTOLOGY_TOOL_INSTRUCTION", "ontology_query"]
