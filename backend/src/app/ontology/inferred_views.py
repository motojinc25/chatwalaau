"""Stored entailment for the editor's views: consistency in Checks, inferred statements on the
canvas and in the Data view (CTR-0170 v9, PRP-0211, UDR-0193 D13, D17).

Both read the STORED result of an ontology version (``entailment_results``); nothing
here runs the reasoner. ``findings`` reads the record only (the shown form of each
finding was kept when it was stored); ``statements_op`` runs in a search worker, since
it restores the inferred statements to the file's lexical forms through the loaded
query graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
BUILTIN = (RDF, RDFS, OWL, "http://www.w3.org/2001/XMLSchema#")

# Inferred statements a view receives at most (UDR-0193 D17).
MAX_VIEW_STATEMENTS = 20_000
_SCHEMA_PREDICATES = frozenset((f"{RDFS}subClassOf", f"{OWL}equivalentClass", f"{RDFS}domain", f"{RDFS}range"))


def _state(ontology_id: str, scope: Any, imports: bool | None, mode: str) -> dict[str, Any]:
    from app.ontology import entailment_jobs

    return entailment_jobs.status(ontology_id, scope, imports)["modes"].get(mode, {"state": "missing"})


def findings(ontology_id: str, scope: Any, imports: bool | None, mode: str) -> dict[str, Any]:
    """Every inconsistency of the stored result (``key`` stable per cause, UDR-0193 D13)."""
    from app.ontology import entailment_results as results
    from app.ontology import store
    from app.ontology.vocabulary import term_to_json

    key = store.query_key(ontology_id, scope, imports)
    record = results.lookup(key, mode)
    state = _state(ontology_id, scope, imports, mode)
    out: dict[str, Any] = {
        "ontology_id": ontology_id,
        "revision": key[1],
        "mode": mode,
        "state": state.get("state"),
        "message": state.get("message"),
        "detail": state.get("detail"),
        "stated": state.get("stated"),
        "count": 0,
        "truncated": False,
        "items": [],
    }
    if record is None or record.get("state") != results.READY:
        return out
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in record.get("findings") or []:
        finding_key = results.finding_key(item)
        if finding_key in seen:
            continue
        seen.add(finding_key)
        subjects: list[dict[str, Any]] = []
        for term in item.get("terms") or []:
            if "nt" not in term:
                continue
            decoded = results.decode_term(term)
            if not isinstance(decoded, str) and type(decoded).__name__ in ("NamedNode", "BlankNode"):
                if getattr(decoded, "value", "").startswith(BUILTIN):
                    continue
                subjects.append(term_to_json(decoded))
        items.append(
            {
                "key": finding_key,
                "kind": item["kind"],
                "graph": item.get("graph_shown"),
                "message": item.get("message") or item["template"],
                "terms": item.get("shown") or [t.get("nt", t.get("text")) for t in item.get("terms") or []],
                "subjects": subjects,
            }
        )
    out["count"] = int(record.get("findings_count") or len(items))
    out["truncated"] = out["count"] > len(items)
    out["items"] = items
    return out


def statements_op(payload: dict[str, Any], phase: Callable[[str], None]) -> dict[str, Any]:
    """The inferred statements the views draw (a search-worker job, UDR-0193 D17).

    Schema: ``rdfs:subClassOf``, ``owl:equivalentClass``, ``rdfs:domain``, ``rdfs:range``
    between IRIs. Data: ``rdf:type`` of resources to a non-built-in class, and values of
    non-built-in properties. Restored to the file's lexical forms, at most
    ``MAX_VIEW_STATEMENTS``. A result that is not ready answers its state only.
    """
    import pyoxigraph as ox

    from app.ontology import entailment, store
    from app.ontology import entailment_results as results
    from app.ontology.lexical import restore_term
    from app.ontology.vocabulary import term_to_json

    phase("load")
    graph = store.load_store(payload["ontology_id"], payload.get("scope"), payload.get("imports"))
    mode = entailment.parse_mode(payload.get("mode"))
    record = results.lookup(graph.key, mode)
    answer: dict[str, Any] = {
        "ontology_id": payload["ontology_id"],
        "revision": graph.key[1] if graph.key else None,
        "mode": mode,
        "state": record.get("state") if record else "missing",
        "message": record.get("message") if record else None,
        "statements": [],
        "truncated": False,
    }
    if record is None or record.get("state") != results.READY:
        return answer
    phase("entailment")
    store.ensure_entailment(graph, mode, compute=False)
    computed = results.load(graph.key, mode, record)
    rows: list[tuple[Any, Any]] = [(t, None) for t in computed.merge_inferred]
    for name, inferred in computed.per_graph.items():
        rows.extend((t, name) for t in inferred)
    type_iri = f"{RDF}type"
    kept: list[tuple[Any, Any, str]] = []
    for t, name in rows:
        predicate = t.predicate.value
        subject, obj = t.subject, t.object
        if predicate in _SCHEMA_PREDICATES:
            if (
                isinstance(subject, ox.NamedNode)
                and isinstance(obj, ox.NamedNode)
                and not obj.value.startswith(BUILTIN)
            ):
                kept.append((t, name, "schema"))
        elif predicate == type_iri:
            if isinstance(obj, ox.NamedNode) and not obj.value.startswith(BUILTIN):
                kept.append((t, name, "type"))
        elif not predicate.startswith(BUILTIN) and isinstance(subject, ox.NamedNode | ox.BlankNode):
            kept.append((t, name, "value"))
    answer["truncated"] = len(kept) > MAX_VIEW_STATEMENTS
    kept = kept[:MAX_VIEW_STATEMENTS]
    answer["statements"] = [
        {
            "s": term_to_json(restore_term(graph.index, t.subject)[0]),
            "p": term_to_json(t.predicate),
            "o": term_to_json(restore_term(graph.index, t.object)[0]),
            "graph": None if name is None else term_to_json(name),
            "kind": kind,
        }
        for t, name, kind in kept
    ]
    return answer


__all__ = ["MAX_VIEW_STATEMENTS", "findings", "statements_op"]
