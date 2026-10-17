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
- PRP-0211 (CTR-0172 v7, UDR-0193): the search runs in a search worker under the agent
  time limit ``ontology_tool_timeout_seconds`` (a timeout answers a message asking for a
  narrower question); the catalog lists the entailment modes already computed for each
  ontology (``entailment_ready``); ``action="explain"`` explains why one statement is
  inferred (the rules applied and the stated statements it rests on).
- PRP-0212 (CTR-0172 v8, UDR-0194 D14): ``action="validate"`` answers the stored SHACL
  validation of an ontology's current version (counts, results grouped by shape, path
  and constraint, then examples; ``focus`` narrows to one node); the catalog lists each
  ontology's ``validation`` state.

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
from app.ontology import nl, store, workers

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
    "properties (OWL 2 RL entailment; limited to smaller ontologies). If an owl-rl "
    "answer reports an inconsistent ontology, give the answer together with that warning. "
    "When the user asks WHY something holds or why the ontology is inconsistent, use "
    "action='explain' with the statement as one Turtle triple (prefixes of the ontology allowed) "
    "and the entailment mode; it answers the rules applied and the stated statements behind it. "
    "When the user asks whether the data satisfies its constraints or shapes (SHACL), use "
    "action='validate' (optionally focus='<node>' for one node); ontologies with shapes show "
    "validation in the catalog. "
    "Searches have a time limit: if one times out, ask a narrower question."
)

# Result lines a validate answer lists at most (besides the groups; UDR-0194 D14).
_VALIDATE_RESULTS = 50


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
        ready = _ready_modes(e["id"])
        if ready:
            item["entailment_ready"] = ready
        validation = _validation_summary(e)
        if validation is not None:
            item["validation"] = validation
        out.append(item)
    return out


def _validation_summary(entry: dict[str, object]) -> dict[str, object] | None:
    """PRP-0212 (UDR-0194 D14): the SHACL state of an ontology with shapes, else None."""
    from app.ontology import shacl_results

    config = store.shacl_config(entry)  # type: ignore[arg-type]
    if config is None:
        return None
    try:
        record = shacl_results.lookup(shacl_results.key_for(str(entry["id"]), config))
    except (KeyError, ValueError):
        return None
    if record is None:
        return {"state": "not_validated_yet"}
    if record.get("state") != shacl_results.READY:
        return {"state": record.get("state")}
    counts = record.get("counts") or {}
    return {"state": "ready", "conforms": bool(record.get("conforms")), "violations": counts.get("violation", 0)}


def _ready_modes(ontology_id: str) -> list[str]:
    """The entailment modes whose stored result is ready (UDR-0193 D12); [] when not computed."""
    from app.ontology import entailment_jobs
    from app.ontology import entailment_results as results

    if not entailment_jobs.active():
        return []
    try:
        key = store.query_key(ontology_id)
    except (KeyError, ValueError):
        return []
    return [
        mode
        for mode in entailment_jobs.build_modes()
        if (record := results.lookup(key, mode)) is not None and record.get("state") == results.READY
    ]


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
    action: Annotated[str, Field(description="One of: catalog, query, explain, validate.")],
    ontology: Annotated[
        str,
        Field(description="For query / explain / validate: the target ontology id (preferred) or its exact name."),
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
            "equivalence, owl:sameAs, restrictions, keys; smaller ontologies only). Inferred "
            "triples come in a separate block. For explain: 'rdfs' or 'owl-rl'."
        ),
    ] = "simple",
    statement: Annotated[
        str,
        Field(
            description="For explain: ONE statement as a Turtle triple, e.g. 'ex:bolt ex:partOf ex:engine .' "
            "(the ontology's prefixes may be used)."
        ),
    ] = "",
    focus: Annotated[
        str,
        Field(
            description="For validate, optional: only the results about this node (a prefixed name "
            "of the ontology, <iri>, or a full IRI)."
        ),
    ] = "",
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

    if act not in ("query", "explain", "validate"):
        return "Error: 'action' must be one of catalog, query, explain, validate."

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
    if act == "explain":
        return await _explain(resolved, statement, entailment, graph)
    if act == "validate":
        return await _validate(resolved, focus)
    if not (question or "").strip():
        return "Error: 'question' is required for query."

    try:
        scope = store.parse_scope(graph.strip() or None)
        mode = entailment_modes.parse_mode(entailment)
    except ValueError as exc:
        return f"Error: {exc}"
    # PRP-0211 (UDR-0193 D3 / D4): the summary and the query run in a search worker under
    # the agent time limit; the model's SPARQL generation is not counted.
    budget = workers.Budget(workers.AGENT)
    try:
        schema = await workers.summary(resolved["id"], scope=scope, imports=None, budget=budget)
    except workers.QueryTimeout as exc:
        return f"Error: {exc} Ask a narrower question."
    except Exception as exc:
        logger.warning("ontology_query could not load %s", resolved["id"], exc_info=True)
        return f"Error: could not load ontology {resolved['id']}: {exc}"

    try:
        # CONSTRUCT-only lane (UDR-0084 D9): the answer is always a graph.
        sparql = await nl.generate_sparql(question, schema, construct_only=True)
    except ValueError as exc:
        return f"Error: could not translate the question into SPARQL: {exc}"
    except Exception as exc:
        logger.warning("ontology_query NL->SPARQL failed for %s", resolved["id"], exc_info=True)
        return f"Error: the SPARQL generation model call failed: {exc}"

    try:
        result = await workers.query(
            resolved["id"],
            scope=scope,
            imports=None,
            sparql=sparql,
            mode=mode,
            budget=budget,
            max_construct_triples=settings.ontology_tool_max_triples,
        )
    except workers.QueryTimeout as exc:
        return f"Error: {exc} Ask a narrower question.\nGenerated SPARQL:\n{sparql}"
    except entailment_modes.EntailmentError as exc:
        return f"Error: {exc}"
    except workers.WorkerFailed as exc:
        return f"Error: the search failed ({exc}); try again."
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
            f"Ontology: {resolved['name']} ({resolved['id']}), {store.scope_label(scope)}\n"
            f"Generated SPARQL:\n{sparql}\n\n"
            "Result (RDF TriG; each triple is under the named graph(s) that hold it, the rest is "
            f"in the default graph):\n```trig\n{turtle}\n```{inferred_block}{notice}"
        )
    return warning + (
        f"Ontology: {resolved['name']} ({resolved['id']})\n"
        f"Generated SPARQL:\n{sparql}\n\n"
        f"Result (RDF Turtle):\n```turtle\n{turtle}\n```{inferred_block}{notice}"
    )


async def _explain(resolved: dict[str, str], statement: str, entailment: str, graph: str) -> str:
    """``action="explain"``: why one statement holds (UDR-0193 D16)."""
    from app.ontology import explain

    if not (statement or "").strip():
        return "Error: 'statement' (one Turtle triple) is required for explain."
    try:
        scope = store.parse_scope(graph.strip() or None)
        mode = entailment_modes.parse_mode(entailment or "rdfs")
    except ValueError as exc:
        return f"Error: {exc}"
    if mode == entailment_modes.SIMPLE:
        mode = entailment_modes.RDFS
    try:
        result = await workers.explain(
            resolved["id"],
            scope=scope,
            imports=None,
            mode=mode,
            statement=statement.strip(),
            budget=workers.Budget(workers.AGENT),
        )
    except workers.QueryTimeout as exc:
        return f"Error: {exc}"
    except entailment_modes.EntailmentError as exc:
        return f"Error: {exc}"
    except workers.WorkerFailed as exc:
        return f"Error: the explanation failed ({exc}); try again."
    except ValueError as exc:
        return f"Error: {exc}"
    return explain.render_text(result, ontology=f"{resolved['name']} ({resolved['id']})")


def _validation_view(ontology_id: str, focus: str) -> dict[str, object]:
    """The stored record and results of the current version, ``focus`` applied (blocking)."""
    from app.ontology import shacl_results

    config = store.shacl_config(store.get_entry(ontology_id))
    if config is None:
        return {"record": None}
    key = shacl_results.key_for(ontology_id, config)
    record = shacl_results.lookup(key)
    if record is None or record.get("state") != shacl_results.READY:
        return {"record": record}
    items = shacl_results.load_results(key)
    wanted = (focus or "").strip()
    if wanted:
        prefixes = dict(record.get("prefixes") or {})
        iri = wanted[1:-1] if wanted.startswith("<") and wanted.endswith(">") else wanted
        name, _, local = wanted.partition(":")
        if name in prefixes and not wanted.startswith("<"):
            iri = prefixes[name] + local
        targets = {f"<{iri}>", wanted}
        items = [item for item in items if item["focus"]["nt"] in targets or item["focus"]["shown"] == wanted]
    return {"record": record, "items": items}


def _render_validation(resolved: dict[str, str], record: dict[str, object], items: list, focus: str) -> str:
    counts = dict(record.get("counts") or {})  # type: ignore[call-overload]
    lines = [
        f"SHACL validation of {resolved['name']} ({resolved['id']}), version {str(record.get('revision'))[:12]}; "
        f"inference before validation: {record.get('inference', 'none')}.",
    ]
    shapes = []
    for source in record.get("sources") or []:  # type: ignore[union-attr]
        label = (
            f"graph <{source['ref']}>"
            if source["kind"] == "graph"
            else f"ontology {source.get('name') or source['ref']}"
        )
        shapes.append(label + (" (missing)" if source.get("missing") else ""))
    if shapes:
        lines.append("Shapes: " + "; ".join(shapes) + ".")
    verdict = "conforms" if record.get("conforms") else "does not conform"
    lines.append(
        f"Result: {verdict} -- {counts.get('violation', 0)} violation(s), {counts.get('warning', 0)} warning(s), "
        f"{counts.get('info', 0)} note(s)."
    )
    budget = max(1, min(_VALIDATE_RESULTS, int(settings.ontology_tool_max_triples)))
    if focus.strip():
        lines.append(f"Results about {focus.strip()}: {len(items)}.")
    else:
        groups = list(record.get("groups") or [])  # type: ignore[call-overload]
        if groups:
            lines.append("By shape, path and constraint (count):")
            for group in groups[:budget]:
                shape = (group.get("shape") or {}).get("shown") or "?"
                path = group.get("path") or "(node)"
                constraint = group.get("constraint") or group.get("component")
                lines.append(
                    f"- [{group['severity']}] {shape} -- {path} -- {constraint}: {group['count']} "
                    f'("{group.get("message")}")'
                )
            if len(groups) > budget:
                lines.append(f"- ... and {len(groups) - budget} more group(s)")
    if items:
        lines.append(f"Results (first {min(budget, len(items))}):")
        for item in items[:budget]:
            value = f" (value {item['value']['shown']})" if item.get("value") else ""
            lines.append(
                f"- [{item['severity']}] {item['focus']['shown']} -- {item.get('path') or '(node)'} -- "
                f"{item['message']}{value}"
            )
    unsupported = list(record.get("unsupported") or [])  # type: ignore[call-overload]
    if unsupported:
        ignored = ", ".join(f"{u['construct']} on {u['shape']}" for u in unsupported[:10])
        lines.append(f"Ignored (only SHACL Core is validated): {ignored}.")
    return "\n".join(lines)


async def _validate(resolved: dict[str, str], focus: str) -> str:
    """``action="validate"``: the stored SHACL validation of the current version (UDR-0194 D14)."""
    import time

    from app.ontology import shacl_jobs

    if store.shacl_config(await run_in_threadpool(store.get_entry, resolved["id"])) is None:
        return (
            f"Ontology '{resolved['name']}' ({resolved['id']}) has no shapes; SHACL validation is not "
            "configured for it (the operator declares shapes graphs or shapes ontologies in the catalog)."
        )
    budget = workers.Budget(workers.AGENT)
    try:
        if shacl_jobs.active():
            limit = budget.remaining() or float(max(1, int(settings.ontology_tool_timeout_seconds) or 20))
            current = await shacl_jobs.wait_ready(resolved["id"], time.monotonic() + limit)
        else:
            await workers.run("shacl", {"ontology_id": resolved["id"]}, budget=budget)
            current = await run_in_threadpool(shacl_jobs.status, resolved["id"])
    except workers.QueryTimeout as exc:
        return f"Error: {exc}"
    except workers.WorkerFailed as exc:
        return f"Error: the validation failed ({exc}); try again."
    except KeyError:
        return f"Error: ontology {resolved['id']} no longer exists."
    state = current.get("state")
    if state in ("missing", "queued", "running"):
        return "Validation is still running; try again shortly."
    if state != "ready":
        return (
            f"SHACL validation of {resolved['name']} ({resolved['id']}) did not run: {current.get('message') or state}."
        )
    view = await run_in_threadpool(_validation_view, resolved["id"], focus)
    record = view.get("record")
    if not isinstance(record, dict) or record.get("state") != "ready":
        return "Validation is still running; try again shortly."
    return _render_validation(resolved, record, list(view.get("items") or []), focus)


__all__ = ["ONTOLOGY_TOOL_INSTRUCTION", "ontology_query"]
