"""Per-run narrowing of a knowledge scope (CTR-0288, PRP-0258, UDR-0239 D5).

The scoped ``rag_search`` and ``ontology_query`` wrappers (PRP-0256) are built once per
agent from the spec's lists. A routed ``/v1/responses`` run narrows them further to the
matched route WITHOUT rebuilding the agent: the API lane sets :data:`_narrowing` for the
duration of the run, and each wrapper intersects its own list with it.

- Only ``app.openai_api`` sets it (an invariant pins that). Unset -- every other lane and
  every unrouted request -- the wrappers behave exactly as in PRP-0256.
- It can only intersect, never add: a name the wrapper does not already allow stays refused.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class Narrowing:
    """The route's sources: collections by name, ontologies by id or exact name."""

    rag_collections: tuple[str, ...] = ()
    ontologies: tuple[str, ...] = ()


_narrowing: contextvars.ContextVar[Narrowing | None] = contextvars.ContextVar("knowledge_narrowing", default=None)


def current() -> Narrowing | None:
    return _narrowing.get()


def set_for_run(narrowing: Narrowing | None) -> None:
    """Set the narrowing for the current run, with no reset (the ``set_temporary_run`` pattern):
    each request -- and each SSE response -- runs in its own task context, which ends with it.
    ``None`` (every unrouted run) leaves the wrappers unchanged."""
    _narrowing.set(narrowing)


def narrow_collections(allowed: tuple[str, ...]) -> tuple[str, ...]:
    """``allowed`` limited to the current narrowing, in the route's order (unchanged when unset)."""
    narrowing = _narrowing.get()
    if narrowing is None:
        return allowed
    return tuple(c for c in narrowing.rag_collections if c in allowed)


def narrow_ontologies(allowed: tuple[str, ...]) -> tuple[str, ...]:
    """``allowed`` limited to the current narrowing (case-insensitive names; unchanged when unset)."""
    narrowing = _narrowing.get()
    if narrowing is None:
        return allowed
    route = {o.casefold() for o in narrowing.ontologies}
    return tuple(o for o in allowed if o.casefold() in route)


__all__ = ["Narrowing", "current", "narrow_collections", "narrow_ontologies", "set_for_run"]
