"""Explicit statement operations with reifier follow (CTR-0171 v5 ``POST /{id}/statements``, UDR-0182 D8 / D9).

A full-state ``PUT`` cannot tell "statement X became Y" from "X removed, Y added",
so reifiers (``r rdf:reifies <<( s p o )>>``) cannot follow an edit made through it.
These operations say what happened:

- ``add``      -- add a statement (a no-op when it already exists);
- ``remove``   -- remove a statement; ``reifiers: "delete"`` also removes the
                  reifiers of its triple (default ``"keep"``);
- ``replace``  -- remove ``from`` and add ``to``; with ``follow_reifiers`` (default
                  true) every ``rdf:reifies`` object equal to the old triple term,
                  also nested inside other triple terms, is rewritten;
- ``annotate`` -- add a reifier (an IRI, minted when absent) with ``rdf:reifies``
                  in the statement's graph and optional first annotations.

When the old triple is still asserted in another graph after the operation, its
reifiers may describe that occurrence: they are left alone and reported as kept.
Operations apply in order to the parsed dataset; the caller writes the result with
the same lossless writer as ``PUT`` (UDR-0180).

Rebase (CTR-0171 v6, UDR-0183 D5): a request made against an older revision with
``on_stale: "rebase"`` is applied to the CURRENT file. Adding a statement that is
already there changes nothing (``skipped``); removing, replacing or annotating one
that is gone is a conflict, and any conflict refuses the whole request
(``StaleConflict``). Layout positions (``cw:x`` / ``cw:y`` in the default graph) are
last-writer-wins instead. Blank-node labels a client lists as fresh are renamed to
labels the current file does not use, on every save (D6).
"""

from __future__ import annotations

import secrets
from typing import Any

from app.ontology.vocabulary import (
    CW_X,
    CW_Y,
    RDF,
    ProjectionError,
    _iri,
    is_named,
    statement_quad,
    term_from_json,
    term_to_json,
)

RDF_REIFIES = f"{RDF}reifies"
OPERATIONS = ("add", "remove", "replace", "annotate")


LAYOUT_PREDICATES = frozenset({CW_X, CW_Y})
# At most this many conflicts are listed in a 409 body; ``count`` has them all.
MAX_LISTED_CONFLICTS = 20


class StaleConflict(ValueError):
    """A rebased request touches statements the current file no longer holds (409)."""

    def __init__(self, conflicts: list[dict[str, Any]], count: int) -> None:
        super().__init__(f"{count} operation(s) conflict with changes saved since this revision")
        self.conflicts = conflicts
        self.count = count
        self.revision: str | None = None  # the current revision, set by the caller


class StatementNotFound(ValueError):
    """A ``remove`` / ``replace`` / ``annotate`` names a statement the file does not hold (422)."""

    def __init__(self, pointer: str) -> None:
        super().__init__(f"{pointer}: no such statement in the ontology")
        self.pointer = pointer


def mint_reifier(base: str, taken: set[str]) -> str:
    """``<ontology base>r-<8 hex>``, unique among the subjects in use (UDR-0182 D8, PRP-0200 Q5)."""
    while True:
        candidate = f"{base}r-{secrets.token_hex(4)}"
        if candidate not in taken:
            return candidate


def _statement(op: dict[str, Any], pointer: str) -> Any:
    """``{s, p, o, g?}`` as a quad."""
    if not isinstance(op, dict):
        raise ProjectionError(pointer, "must be {s, p, o, g?}")
    subject = term_from_json(op.get("s"), f"{pointer}.s", position="subject")
    return statement_quad(subject, op, pointer)


def _replace_nested(term: Any, old: Any, new: Any) -> Any:
    """``term`` with every occurrence of the triple term ``old`` replaced by ``new``."""
    import pyoxigraph as ox

    if not isinstance(term, ox.Triple):
        return term
    if term == old:
        return new
    subject = _replace_nested(term.subject, old, new)
    obj = _replace_nested(term.object, old, new)
    if subject is term.subject and obj is term.object:
        return term
    return ox.Triple(subject, term.predicate, obj)


def _contains(term: Any, target: Any) -> bool:
    import pyoxigraph as ox

    if not isinstance(term, ox.Triple):
        return False
    return term == target or _contains(term.subject, target) or _contains(term.object, target)


class _Dataset:
    """The parsed file as an insertion-ordered set of quads."""

    def __init__(self, quads: list[Any]) -> None:
        self.quads: dict[Any, None] = dict.fromkeys(quads)
        self._layout: dict[tuple[Any, Any], list[Any]] | None = None

    def asserted(self, triple: Any) -> bool:
        return any(q.triple == triple for q in self.quads)

    def reifier_quads(self, triple_term: Any) -> list[Any]:
        return [q for q in self.quads if q.predicate.value == RDF_REIFIES and _contains(q.object, triple_term)]

    def subjects(self) -> set[str]:
        return {q.subject.value for q in self.quads if hasattr(q.subject, "value")}

    def add_layout(self, quad: Any) -> None:
        """Add a layout position, replacing the node's other values of that predicate (D5)."""
        if self._layout is None:
            # Built once: a full layout commit rebases one add per entity.
            self._layout = {}
            for q in self.quads:
                if _is_layout(q):
                    self._layout.setdefault((q.subject, q.predicate), []).append(q)
        values = self._layout.setdefault((quad.subject, quad.predicate), [])
        for old in values:
            self.quads.pop(old, None)  # an entry removed by an earlier operation is already gone
        values[:] = [quad]
        self.quads[quad] = None


def _is_layout(quad: Any) -> bool:
    return not is_named(quad) and quad.predicate.value in LAYOUT_PREDICATES


def _walk_labels(value: Any, out: set[str]) -> None:
    """Every blank-node label in operation JSON (terms, nested triple terms, graphs)."""
    if isinstance(value, dict):
        if value.get("type") == "bnode" and isinstance(value.get("value"), str):
            out.add(value["value"])
        for item in value.values():
            _walk_labels(item, out)
    elif isinstance(value, list):
        for item in value:
            _walk_labels(item, out)


def _relabel(value: Any, mapping: dict[str, str]) -> Any:
    """A copy of operation JSON with the blank-node labels in ``mapping`` renamed."""
    if isinstance(value, dict):
        if value.get("type") == "bnode" and value.get("value") in mapping:
            return {**value, "value": mapping[value["value"]]}
        return {key: _relabel(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_relabel(item, mapping) for item in value]
    return value


def _file_labels(quads: list[Any]) -> set[str]:
    import pyoxigraph as ox

    labels: set[str] = set()

    def visit(term: Any) -> None:
        if isinstance(term, ox.BlankNode):
            labels.add(term.value)
        elif isinstance(term, ox.Triple):
            visit(term.subject)
            visit(term.object)

    for quad in quads:
        visit(quad.subject)
        visit(quad.object)
        visit(quad.graph_name)
    return labels


def fresh_label_map(quads: list[Any], operations: Any, fresh: list[str]) -> dict[str, str]:
    """Rename each fresh label the current file (or another label of the request) uses (D6).

    A fresh label nobody uses keeps its name; the map lists only the renamed ones.
    """
    if not fresh:
        return {}
    for i, label in enumerate(fresh):
        if not isinstance(label, str) or not label:
            raise ProjectionError(f"/fresh_blank_nodes/{i}", "must be a blank node label")
    fresh_set = set(fresh)
    referenced: set[str] = set()
    _walk_labels(operations, referenced)
    taken = _file_labels(quads) | (referenced - fresh_set)
    mapping: dict[str, str] = {}
    counter = 0
    for label in dict.fromkeys(fresh):
        if label not in taken:
            taken.add(label)
            continue
        while True:
            counter += 1
            candidate = f"b{counter}"
            if candidate not in taken and candidate not in fresh_set:
                break
        taken.add(candidate)
        mapping[label] = candidate
    return mapping


def _conflict(pointer: str, kind: str, quad: Any) -> dict[str, Any]:
    statement = {
        "s": term_to_json(quad.subject),
        "p": quad.predicate.value,
        "o": term_to_json(quad.object),
    }
    if is_named(quad):
        statement["g"] = term_to_json(quad.graph_name)
    return {"pointer": pointer, "op": kind, "statement": statement}


def apply_operations(
    quads: list[Any],
    operations: Any,
    *,
    ontology_base: str,
    rebase: bool = False,
    fresh_blank_nodes: list[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Apply ``operations`` to ``quads``; returns (new quads, report).

    Raises ``ProjectionError`` (with a JSON pointer) for a malformed operation and
    ``StatementNotFound`` for a statement the file does not hold. With ``rebase``
    (the request was made against an older revision, UDR-0183 D5) a missing
    statement is collected as a conflict instead, layout positions are
    last-writer-wins, and any conflict raises ``StaleConflict``. Nothing is
    written here; the caller saves the result.
    """
    import pyoxigraph as ox

    if not isinstance(operations, list) or not operations:
        raise ProjectionError("/operations", "must be a non-empty list")
    renamed = fresh_label_map(quads, operations, list(fresh_blank_nodes or []))
    if renamed:
        operations = _relabel(operations, renamed)
    data = _Dataset(quads)
    reifies = ox.NamedNode(RDF_REIFIES)
    minted: list[str] = []
    kept: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    conflict_count = 0
    applied = 0
    skipped = 0

    def conflict(pointer: str, kind: str, quad: Any) -> None:
        nonlocal conflict_count
        conflict_count += 1
        if len(conflicts) < MAX_LISTED_CONFLICTS:
            conflicts.append(_conflict(pointer, kind, quad))

    for i, op in enumerate(operations):
        pointer = f"/operations/{i}"
        if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
            raise ProjectionError(f"{pointer}/op", f"must be one of {', '.join(OPERATIONS)}")
        kind = op["op"]
        if kind == "add":
            quad = _statement(op, pointer)
            if quad in data.quads:
                skipped += 1
                continue
            if rebase and _is_layout(quad):
                data.add_layout(quad)  # last writer wins (D5)
            else:
                data.quads[quad] = None
        elif kind == "remove":
            quad = _statement(op, pointer)
            if quad not in data.quads:
                if not rebase:
                    raise StatementNotFound(pointer)
                if _is_layout(quad):
                    skipped += 1  # the other save moved it; the add that follows wins
                else:
                    conflict(pointer, kind, quad)
                continue
            mode = op.get("reifiers", "keep")
            if mode not in ("keep", "delete"):
                raise ProjectionError(f"{pointer}/reifiers", 'must be "keep" or "delete"')
            del data.quads[quad]
            if mode == "delete":
                if data.asserted(quad.triple):
                    kept.extend(_report(q) for q in data.reifier_quads(quad.triple))
                else:
                    _delete_reifiers(data, quad.triple)
        elif kind == "replace":
            old = _statement(op.get("from"), f"{pointer}/from")
            new = _statement(op.get("to"), f"{pointer}/to")
            if old not in data.quads:
                if not rebase:
                    raise StatementNotFound(f"{pointer}/from")
                if _is_layout(old) and _is_layout(new) and old.subject == new.subject:
                    data.add_layout(new)
                    applied += 1
                else:
                    conflict(f"{pointer}/from", kind, old)
                continue
            follow = op.get("follow_reifiers", True)
            if not isinstance(follow, bool):
                raise ProjectionError(f"{pointer}/follow_reifiers", "must be true or false")
            del data.quads[old]
            data.quads.setdefault(new, None)
            if follow and old.triple != new.triple:
                holders = data.reifier_quads(old.triple)
                if data.asserted(old.triple):
                    kept.extend(_report(q) for q in holders)
                else:
                    for q in holders:
                        del data.quads[q]
                        target = _replace_nested(q.object, old.triple, new.triple)
                        data.quads.setdefault(ox.Quad(q.subject, q.predicate, target, q.graph_name), None)
        else:  # annotate
            quad = _statement(op, pointer)
            if quad not in data.quads:
                if not rebase:
                    raise StatementNotFound(pointer)
                conflict(pointer, kind, quad)
                continue
            given = op.get("reifier")
            if given is None:
                reifier = ox.NamedNode(mint_reifier(ontology_base, data.subjects() | set(minted)))
            else:
                reifier = _iri(given, f"{pointer}/reifier")
            minted.append(reifier.value)
            data.quads.setdefault(ox.Quad(reifier, reifies, quad.triple, quad.graph_name), None)
            annotations = op.get("annotations") or []
            if not isinstance(annotations, list):
                raise ProjectionError(f"{pointer}/annotations", "must be a list of {p, o}")
            for j, annotation in enumerate(annotations):
                if isinstance(annotation, dict):
                    annotation = {**annotation, "g": op.get("g")}  # the statement's graph
                data.quads.setdefault(
                    statement_quad(reifier, annotation, f"{pointer}/annotations/{j}"),
                    None,
                )
        applied += 1
    if conflict_count:
        raise StaleConflict(conflicts, conflict_count)
    report: dict[str, Any] = {"applied": applied, "skipped": skipped}
    if renamed:
        report["blank_nodes"] = renamed
    if minted:
        report["reifier"] = minted
    if kept:
        report["kept_reifiers"] = kept
    return list(data.quads), report


def _report(quad: Any) -> dict[str, Any]:
    out = {"reifier": term_to_json(quad.subject), "o": term_to_json(quad.object)}
    if is_named(quad):
        out["g"] = term_to_json(quad.graph_name)
    return out


def _delete_reifiers(data: _Dataset, triple: Any) -> None:
    """Remove the ``rdf:reifies`` links to ``triple``; a reifier left with none loses all its statements."""
    for link in data.reifier_quads(triple):
        if link.object != triple:
            continue  # nested inside another triple term: that reifier describes something else
        del data.quads[link]
        reifier = link.subject
        if not any(q.subject == reifier and q.predicate.value == RDF_REIFIES for q in data.quads):
            for q in [q for q in data.quads if q.subject == reifier]:
                del data.quads[q]


__all__ = [
    "LAYOUT_PREDICATES",
    "OPERATIONS",
    "StaleConflict",
    "StatementNotFound",
    "apply_operations",
    "fresh_label_map",
    "mint_reifier",
]
