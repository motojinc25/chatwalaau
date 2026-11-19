"""Knowledge scope of a declarative agent (CTR-0142 v4, PRP-0256, PRP-0258, UDR-0237, UDR-0239).

An agent YAML may limit where its tools search and how it answers:

```yaml
metadata:
  chatwalaau:
    knowledge:
      rag_collections: [hr-handbook, hr-faq]   # the first is the default collection
      ontologies: [hr-rules]                   # ids or exact names
      grounding: strict                        # off (default) | strict
      max_distance: 1.2                        # optional (L2); strict drops farther hits
      web_search: false                        # default false: no provider web search
      routing:                                 # optional (PRP-0258): /v1/responses route "auto"
        instructions: "..."                    # optional, at most 2000 characters
        min_probability: 0.6                   # optional; default: App Setting (60 %)
        low_confidence: all                    # all (default) | clarify
        on_error: all                          # all (default) | fail
        routes:
          - id: leave
            description: "Paid leave, sick leave and absence."
            rag_collections: [hr-handbook]
            ontologies: [hr-rules]
```

The block lives under ``metadata.chatwalaau`` because the MAF declarative ``PromptAgent``
rejects an unknown top-level key, while ``metadata`` is part of its schema: a desk YAML
stays loadable by MAF tooling. A top-level ``knowledge`` key is a warning that says so.

The scope is applied at the tool-assembly chokepoint (``app.agui.agent_factory``), so
every lane that runs the spec gets it. ``grounding`` and ``routing`` are read only by the
/v1/responses lane of a key-bound agent (CTR-0287, CTR-0288). A route can narrow the scope,
never widen it (UDR-0239 D3, D5).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

GROUNDING_MODES: tuple[str, ...] = ("off", "strict")
KNOWLEDGE_KEYS: tuple[str, ...] = (
    "rag_collections",
    "ontologies",
    "grounding",
    "max_distance",
    "web_search",
    "routing",
)
#: The path of the block inside the YAML, for messages.
KNOWLEDGE_PATH = "metadata.chatwalaau.knowledge"

# ---- Routing (PRP-0258, UDR-0239 D3) ---------------------------------------------------
ROUTING_PATH = f"{KNOWLEDGE_PATH}.routing"
ROUTING_KEYS: tuple[str, ...] = ("instructions", "min_probability", "low_confidence", "on_error", "routes")
ROUTE_KEYS: tuple[str, ...] = ("id", "description", "rag_collections", "ontologies")
LOW_CONFIDENCE_MODES: tuple[str, ...] = ("all", "clarify")
ON_ERROR_MODES: tuple[str, ...] = ("all", "fail")
ROUTE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Route ids the response metadata uses for itself (``all`` = full scope, ``none`` = clarify).
RESERVED_ROUTE_IDS: tuple[str, ...] = ("all", "none")
MIN_ROUTES = 2
#: The decision schema's limits for a choice option description and question instructions.
MAX_ROUTE_DESCRIPTION_CHARS = 2_000
MAX_ROUTING_INSTRUCTIONS_CHARS = 1_500


@dataclass(frozen=True)
class Route:
    id: str
    description: str
    rag_collections: tuple[str, ...] = ()
    ontologies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "rag_collections": list(self.rag_collections),
            "ontologies": list(self.ontologies),
        }


@dataclass(frozen=True)
class Routing:
    routes: tuple[Route, ...]
    instructions: str = ""
    #: None = the App Setting ``responses_route_min_probability_percent`` decides.
    min_probability: float | None = None
    low_confidence: str = "all"
    on_error: str = "all"

    def route(self, route_id: str) -> Route | None:
        return next((r for r in self.routes if r.id == route_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instructions": self.instructions,
            "min_probability": self.min_probability,
            "low_confidence": self.low_confidence,
            "on_error": self.on_error,
            "routes": [r.to_dict() for r in self.routes],
        }


@dataclass(frozen=True)
class KnowledgeScope:
    rag_collections: tuple[str, ...] = ()
    ontologies: tuple[str, ...] = ()
    grounding: str = "off"
    max_distance: float | None = None
    web_search: bool = False
    routing: Routing | None = None

    @property
    def strict(self) -> bool:
        return self.grounding == "strict"

    def narrowed_to(self, route: Route) -> KnowledgeScope:
        """The run scope of ``route``: the intersection with this scope, in the route's order.

        Never widens (UDR-0239 D5): a name the spec does not allow is dropped even if a
        drifted route still lists it. Grounding, distance and web search stay the spec's.
        """
        allowed_onto = {o.casefold() for o in self.ontologies}
        return KnowledgeScope(
            rag_collections=tuple(c for c in route.rag_collections if c in self.rag_collections),
            ontologies=tuple(o for o in route.ontologies if o.casefold() in allowed_onto),
            grounding=self.grounding,
            max_distance=self.max_distance,
            web_search=self.web_search,
            routing=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rag_collections": list(self.rag_collections),
            "ontologies": list(self.ontologies),
            "grounding": self.grounding,
            "max_distance": self.max_distance,
            "web_search": self.web_search,
            "routing": self.routing.to_dict() if self.routing is not None else None,
        }


def _names(raw: Any, key: str, warnings: list[str], path: str = KNOWLEDGE_PATH) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        warnings.append(f"{path}.{key} ignored (expected a list of names).")
        return ()
    seen: set[str] = set()
    return tuple(name.strip() for name in raw if not (name.strip() in seen or seen.add(name.strip())))


def raw_knowledge(data: dict[str, Any]) -> Any:
    """The raw block under metadata.chatwalaau.knowledge, or None."""
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return None
    ours = meta.get("chatwalaau")
    if not isinstance(ours, dict):
        return None
    return ours.get("knowledge")


def _max_routes() -> int:
    """The decision schema's choice-option limit (App Setting ``decision_max_choice_options``)."""
    try:
        from app.core.config import settings

        return max(MIN_ROUTES, int(settings.decision_max_choice_options))
    except Exception:
        return 50


def _map_route(
    raw: Any, index: int, collections: tuple[str, ...], ontologies: tuple[str, ...], warnings: list[str]
) -> Route | None:
    path = f"{ROUTING_PATH}.routes[{index}]"
    if not isinstance(raw, dict):
        warnings.append(f"{path} ignored (expected a mapping with id, description and sources).")
        return None
    unknown = sorted(set(raw) - set(ROUTE_KEYS))
    if unknown:
        warnings.append(f"{path}: unknown key(s) {unknown}; use {', '.join(ROUTE_KEYS)}.")
    route_id = raw.get("id")
    if not isinstance(route_id, str) or not ROUTE_ID_RE.match(route_id):
        warnings.append(f"{path}.id must match {ROUTE_ID_RE.pattern} (lowercase letters, digits, - and _).")
        return None
    if route_id in RESERVED_ROUTE_IDS:
        warnings.append(f"{path}.id {route_id!r} is reserved; choose another id.")
        return None
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        warnings.append(f"{path} ({route_id}): description must be non-empty text.")
        description = ""
    elif len(description.strip()) > MAX_ROUTE_DESCRIPTION_CHARS:
        warnings.append(f"{path} ({route_id}): description is longer than {MAX_ROUTE_DESCRIPTION_CHARS} characters.")
    route_collections = _names(raw.get("rag_collections"), "rag_collections", warnings, path)
    route_ontologies = _names(raw.get("ontologies"), "ontologies", warnings, path)
    if not route_collections and not route_ontologies:
        warnings.append(f"{path} ({route_id}) names no rag_collections and no ontologies.")
    # Narrow, never widen (UDR-0239 D3).
    outside = [c for c in route_collections if c not in collections]
    allowed_onto = {o.casefold() for o in ontologies}
    outside += [o for o in route_ontologies if o.casefold() not in allowed_onto]
    if outside:
        warnings.append(
            f"{path} ({route_id}) names {outside}, which the knowledge block does not list; a route can "
            "only narrow the agent's sources."
        )
    return Route(route_id, (description or "").strip(), route_collections, route_ontologies)


def map_routing(
    raw: Any, collections: tuple[str, ...], ontologies: tuple[str, ...], grounding: str, warnings: list[str]
) -> Routing | None:
    """Map ``knowledge.routing``; every problem is a warning (it blocks activation, UDR-0072 D9)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.append(f"{ROUTING_PATH} ignored (expected a mapping).")
        return None
    unknown = sorted(set(raw) - set(ROUTING_KEYS))
    if unknown:
        warnings.append(f"{ROUTING_PATH}: unknown key(s) {unknown}; use {', '.join(ROUTING_KEYS)}.")

    instructions = raw.get("instructions", "")
    if instructions is None:
        instructions = ""
    if not isinstance(instructions, str):
        warnings.append(f"{ROUTING_PATH}.instructions must be text.")
        instructions = ""
    elif len(instructions.strip()) > MAX_ROUTING_INSTRUCTIONS_CHARS:
        warnings.append(f"{ROUTING_PATH}.instructions is longer than {MAX_ROUTING_INSTRUCTIONS_CHARS} characters.")

    min_probability = raw.get("min_probability")
    if min_probability is not None and (
        isinstance(min_probability, bool)
        or not isinstance(min_probability, int | float)
        or not 0 < float(min_probability) <= 1
    ):
        warnings.append(f"{ROUTING_PATH}.min_probability must be a number above 0 and at most 1.")
        min_probability = None

    low_confidence = raw.get("low_confidence", "all")
    if low_confidence not in LOW_CONFIDENCE_MODES:
        warnings.append(f"{ROUTING_PATH}.low_confidence must be one of {', '.join(LOW_CONFIDENCE_MODES)}.")
        low_confidence = "all"
    on_error = raw.get("on_error", "all")
    if on_error not in ON_ERROR_MODES:
        warnings.append(f"{ROUTING_PATH}.on_error must be one of {', '.join(ON_ERROR_MODES)}.")
        on_error = "all"

    raw_routes = raw.get("routes")
    if not isinstance(raw_routes, list):
        warnings.append(f"{ROUTING_PATH}.routes must be a list of at least {MIN_ROUTES} routes.")
        raw_routes = []
    routes: list[Route] = []
    for index, raw_route in enumerate(raw_routes):
        route = _map_route(raw_route, index, collections, ontologies, warnings)
        if route is None:
            continue
        if any(r.id == route.id for r in routes):
            warnings.append(f"{ROUTING_PATH}: route id {route.id!r} is used more than once.")
            continue
        # Strict grounding searches collections only, so an ontology-only route would always
        # answer no_match under strict (the PRP-0256 rule "strict needs collections", per route).
        if grounding == "strict" and not route.rag_collections:
            warnings.append(
                f"{ROUTING_PATH} ({route.id}): with grounding strict every route needs at least one "
                "rag_collections entry."
            )
        routes.append(route)
    if isinstance(raw.get("routes"), list) and len(raw_routes) < MIN_ROUTES:
        warnings.append(f"{ROUTING_PATH}.routes needs at least {MIN_ROUTES} routes.")
    limit = _max_routes()
    if len(raw_routes) > limit:
        warnings.append(
            f"{ROUTING_PATH}.routes has {len(raw_routes)} routes, more than the decision limit of {limit} "
            "(App Settings > Decision)."
        )
    return Routing(
        routes=tuple(routes),
        instructions=instructions.strip(),
        min_probability=float(min_probability) if min_probability is not None else None,
        low_confidence=str(low_confidence),
        on_error=str(on_error),
    )


def map_knowledge(data: dict[str, Any], warnings: list[str]) -> KnowledgeScope | None:
    """Map the knowledge block; structural problems are warnings (they block activation)."""
    if "knowledge" in data:
        warnings.append(f"knowledge must be written under {KNOWLEDGE_PATH}, not at the top level.")
    raw = raw_knowledge(data)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.append(f"{KNOWLEDGE_PATH} ignored (expected a mapping).")
        return None
    unknown = sorted(set(raw) - set(KNOWLEDGE_KEYS))
    if unknown:
        warnings.append(f"{KNOWLEDGE_PATH}: unknown key(s) {unknown}; use {', '.join(KNOWLEDGE_KEYS)}.")

    collections = _names(raw.get("rag_collections"), "rag_collections", warnings)
    ontologies = _names(raw.get("ontologies"), "ontologies", warnings)

    grounding = raw.get("grounding", "off")
    if grounding not in GROUNDING_MODES:
        warnings.append(f"{KNOWLEDGE_PATH}.grounding must be one of {', '.join(GROUNDING_MODES)}.")
        grounding = "off"

    max_distance = raw.get("max_distance")
    if max_distance is not None and (
        isinstance(max_distance, bool) or not isinstance(max_distance, int | float) or max_distance <= 0
    ):
        warnings.append(f"{KNOWLEDGE_PATH}.max_distance must be a number above 0.")
        max_distance = None

    web_search = raw.get("web_search", False)
    if not isinstance(web_search, bool):
        warnings.append(f"{KNOWLEDGE_PATH}.web_search must be true or false.")
        web_search = False

    if grounding == "strict" and not collections:
        warnings.append(f"{KNOWLEDGE_PATH}.grounding strict needs at least one rag_collections entry.")
    if not collections and not ontologies:
        warnings.append(f"{KNOWLEDGE_PATH} names no rag_collections and no ontologies.")

    routing = map_routing(raw.get("routing"), collections, ontologies, str(grounding), warnings)

    return KnowledgeScope(
        rag_collections=collections,
        ontologies=ontologies,
        grounding=str(grounding),
        max_distance=float(max_distance) if max_distance is not None else None,
        web_search=web_search,
        routing=routing,
    )


def allows_web_search(spec: Any) -> bool:
    """False for a spec with a knowledge scope that did not set web_search: true (D4)."""
    scope = getattr(spec, "knowledge", None)
    return scope is None or scope.web_search


def check_tools(scope: KnowledgeScope | None, tool_allowlist: list[str] | None, warnings: list[str]) -> None:
    """A named source whose tool the allow-list leaves out can never be searched."""
    if scope is None or tool_allowlist is None:
        return
    if scope.rag_collections and "function:rag_search" not in tool_allowlist:
        warnings.append(f"{KNOWLEDGE_PATH} names rag_collections but tools does not list rag_search.")
    if scope.ontologies and "function:ontology_query" not in tool_allowlist:
        warnings.append(f"{KNOWLEDGE_PATH} names ontologies but tools does not list ontology_query.")


def document_knowledge(data: dict[str, Any]) -> dict[str, Any] | None:
    """The block reshaped for the authoring form (CTR-0177), or None. Tolerant."""
    raw = raw_knowledge(data)
    if not isinstance(raw, dict):
        return None
    scope = map_knowledge({"metadata": {"chatwalaau": {"knowledge": raw}}}, [])
    return scope.to_dict() if scope is not None else None


def _clean_names(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or []:
        name = str(value).strip()
        if name and name not in out:
            out.append(name)
    return out


def routing_yaml(document: Any) -> dict[str, Any] | None:
    """The canonical routing block written by the authoring serializer, or None when empty.

    Tolerant: it keeps what the operator wrote (even an invalid route), so the loader's
    warnings can name the problem instead of the problem silently disappearing on save.
    """
    if not isinstance(document, dict):
        return None
    routes_out: list[dict[str, Any]] = []
    for route in document.get("routes") or []:
        if not isinstance(route, dict):
            continue
        entry: dict[str, Any] = {
            "id": str(route.get("id") or "").strip(),
            "description": str(route.get("description") or "").strip(),
        }
        collections = _clean_names(route.get("rag_collections"))
        ontologies = _clean_names(route.get("ontologies"))
        if collections:
            entry["rag_collections"] = collections
        if ontologies:
            entry["ontologies"] = ontologies
        if entry["id"] or entry["description"] or collections or ontologies:
            routes_out.append(entry)
    if not routes_out:
        return None
    out: dict[str, Any] = {}
    instructions = str(document.get("instructions") or "").strip()
    if instructions:
        out["instructions"] = instructions
    min_probability = document.get("min_probability")
    if isinstance(min_probability, int | float) and not isinstance(min_probability, bool) and min_probability > 0:
        out["min_probability"] = float(min_probability)
    low_confidence = str(document.get("low_confidence") or "all")
    if low_confidence in LOW_CONFIDENCE_MODES and low_confidence != "all":
        out["low_confidence"] = low_confidence
    on_error = str(document.get("on_error") or "all")
    if on_error in ON_ERROR_MODES and on_error != "all":
        out["on_error"] = on_error
    out["routes"] = routes_out
    return out


def knowledge_yaml(document: Any) -> dict[str, Any] | None:
    """The canonical block written by the authoring serializer, or None when empty."""
    if not isinstance(document, dict):
        return None
    collections = _clean_names(document.get("rag_collections"))
    ontologies = _clean_names(document.get("ontologies"))
    if not collections and not ontologies:
        return None
    out: dict[str, Any] = {}
    if collections:
        out["rag_collections"] = collections
    if ontologies:
        out["ontologies"] = ontologies
    grounding = str(document.get("grounding") or "off")
    out["grounding"] = grounding if grounding in GROUNDING_MODES else "off"
    max_distance = document.get("max_distance")
    if isinstance(max_distance, int | float) and not isinstance(max_distance, bool) and max_distance > 0:
        out["max_distance"] = float(max_distance)
    if document.get("web_search") is True:
        out["web_search"] = True
    routing = routing_yaml(document.get("routing"))
    if routing is not None:
        out["routing"] = routing
    return out


__all__ = [
    "GROUNDING_MODES",
    "KNOWLEDGE_KEYS",
    "KNOWLEDGE_PATH",
    "LOW_CONFIDENCE_MODES",
    "ON_ERROR_MODES",
    "RESERVED_ROUTE_IDS",
    "ROUTE_ID_RE",
    "ROUTING_PATH",
    "KnowledgeScope",
    "Route",
    "Routing",
    "allows_web_search",
    "check_tools",
    "document_knowledge",
    "knowledge_yaml",
    "map_knowledge",
    "map_routing",
    "raw_knowledge",
    "routing_yaml",
]
