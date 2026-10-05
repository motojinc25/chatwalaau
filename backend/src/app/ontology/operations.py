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
"""

from __future__ import annotations

import secrets
from typing import Any

from app.ontology.vocabulary import (
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

    def asserted(self, triple: Any) -> bool:
        return any(q.triple == triple for q in self.quads)

    def reifier_quads(self, triple_term: Any) -> list[Any]:
        return [q for q in self.quads if q.predicate.value == RDF_REIFIES and _contains(q.object, triple_term)]

    def subjects(self) -> set[str]:
        return {q.subject.value for q in self.quads if hasattr(q.subject, "value")}


def apply_operations(quads: list[Any], operations: Any, *, ontology_base: str) -> tuple[list[Any], dict[str, Any]]:
    """Apply ``operations`` to ``quads``; returns (new quads, report).

    Raises ``ProjectionError`` (with a JSON pointer) for a malformed operation and
    ``StatementNotFound`` for a statement the file does not hold. Nothing is
    written here; the caller saves the result.
    """
    import pyoxigraph as ox

    if not isinstance(operations, list) or not operations:
        raise ProjectionError("/operations", "must be a non-empty list")
    data = _Dataset(quads)
    reifies = ox.NamedNode(RDF_REIFIES)
    minted: list[str] = []
    kept: list[dict[str, Any]] = []
    applied = 0
    for i, op in enumerate(operations):
        pointer = f"/operations/{i}"
        if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
            raise ProjectionError(f"{pointer}/op", f"must be one of {', '.join(OPERATIONS)}")
        kind = op["op"]
        if kind == "add":
            data.quads.setdefault(_statement(op, pointer), None)
        elif kind == "remove":
            quad = _statement(op, pointer)
            if quad not in data.quads:
                raise StatementNotFound(pointer)
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
                raise StatementNotFound(f"{pointer}/from")
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
                raise StatementNotFound(pointer)
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
    report: dict[str, Any] = {"applied": applied}
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


__all__ = ["OPERATIONS", "StatementNotFound", "apply_operations", "mint_reifier"]
