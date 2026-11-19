"""Decision-Routed Knowledge Scope (CTR-0288, PRP-0258, UDR-0239).

A ``/v1/responses`` request with ``route: "auto"``, from a key bound to an agent whose spec
has ``metadata.chatwalaau.knowledge.routing``, is classified BEFORE strict retrieval and
before any model call:

1. state = the strict-grounding retrieval query (last user message, plus the previous one
   on a chained turn) -- D4;
2. ONE ``choice`` question ``route`` whose options are the route ids and descriptions, sent
   to the decision offering with ``responses_route_timeout_seconds`` and no retry -- D4, D7;
3. top probability >= the bar -> the run scope is the route's sources intersected with the
   spec scope (narrow, never widen) -- D5; below it -> the full scope (``all``) or the fixed
   clarification answer (``clarify``); an error -> the full scope (``all``) or 503
   ``routing_unavailable`` (``fail``) -- D6;
4. the outcome becomes string ``metadata`` (``route``, ``route_reason``, ``route_probability``)
   -- D8, and the call is recorded per key (lane ``openai-api``, kind ``helper``, purpose
   ``responses_route``); its tokens count toward the key's token limits -- D9.

The question text is never logged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from app.agent.knowledge_narrowing import Narrowing

if TYPE_CHECKING:
    from app.agent.declarative.knowledge import KnowledgeScope, Routing
    from app.openai_api.quota import Ticket

logger = logging.getLogger(__name__)

#: The only accepted value of the request field ``route`` (D1).
ROUTE_AUTO = "auto"

#: Fixed texts (English, PRP-0258). Defined ONLY here.
INVALID_ROUTE = 'route must be "auto" or omitted.'
ROUTE_NOT_AVAILABLE = 'This API key\'s agent has no routing; omit "route".'
ROUTING_UNAVAILABLE = "The question could not be routed. Please try again later."
CLARIFY_ANSWER = (
    "I am not sure which topic your question is about. Please add a few details, for example the area it concerns."
)
ROUTE_QUESTION_INSTRUCTIONS = "Classify the user's question into the one category that best matches its subject."
#: The decision question's name.
QUESTION_NAME = "route"
#: The ledger purpose of the routing call.
LEDGER_PURPOSE = "responses_route"

#: ``route_reason`` values (D8).
MATCHED = "matched"
LOW_CONFIDENCE = "low_confidence"
ERROR = "error"
REASONS: tuple[str, ...] = (MATCHED, LOW_CONFIDENCE, ERROR)
#: ``route`` values that are not route ids.
ALL_ROUTE = "all"
NONE_ROUTE = "none"


class RoutingUnavailable(Exception):
    """``on_error: fail`` and the routing call failed: 503 before any stream starts."""


@dataclass(frozen=True)
class RoutingOutcome:
    #: A route id, ``all`` (full scope) or ``none`` (the clarification answer).
    route: str
    reason: str
    probability: float | None
    #: The run scope; None for the clarification answer (no retrieval, no model call).
    scope: KnowledgeScope | None
    #: The per-run tool narrowing; None when the full scope is used.
    narrowing: Narrowing | None

    @property
    def clarify(self) -> bool:
        return self.scope is None

    def metadata(self) -> dict[str, str]:
        """String values only: OpenAI ``metadata`` values are strings (D8)."""
        out = {"route": self.route, "route_reason": self.reason}
        if self.probability is not None:
            out["route_probability"] = f"{self.probability:.2f}"
        return out


def check_request(route: Any, spec: Any) -> tuple[int, str, str] | None:
    """``(status, code, message)`` for a refused ``route`` field, or None (D1, D2)."""
    if route is None:
        return None
    if route != ROUTE_AUTO:
        return 400, "invalid_route", INVALID_ROUTE
    knowledge = getattr(spec, "knowledge", None) if spec is not None else None
    if knowledge is None or knowledge.routing is None or not knowledge.routing.routes:
        return 400, "route_not_available", ROUTE_NOT_AVAILABLE
    return None


def _setting(name: str, default: int) -> int:
    try:
        from app.core.config import settings

        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def timeout_seconds() -> float:
    """``responses_route_timeout_seconds`` clamped to 1..30 (D7)."""
    return float(min(30, max(1, _setting("responses_route_timeout_seconds", 5))))


def min_probability(routing: Routing) -> float:
    """The agent's ``min_probability``, else the App Setting percent (default 60) (D6)."""
    if routing.min_probability is not None:
        return routing.min_probability
    return min(100, max(1, _setting("responses_route_min_probability_percent", 60))) / 100.0


def build_question(routing: Routing) -> dict[str, dict[str, Any]]:
    """The one ``choice`` question (D4)."""
    instructions = ROUTE_QUESTION_INSTRUCTIONS
    if routing.instructions:
        instructions = f"{instructions} {routing.instructions}"
    return {
        QUESTION_NAME: {
            "type": "choice",
            "instructions": instructions,
            "criteria": {route.id: route.description for route in routing.routes},
        }
    }


def _top(answer: dict[str, Any]) -> tuple[str | None, float | None]:
    """The chosen route and its probability from a normalized choice answer."""
    choice = answer.get("choice") if isinstance(answer.get("choice"), str) else None
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and probabilities:
        best = max(probabilities, key=lambda k: float(probabilities[k]))
        return best, float(probabilities[best])
    return choice, None


async def _ask(state: str, questions: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """``(answer, offering_id, usage)`` or raise. DEMO_MODE answers with the tool's placeholder."""
    from app.decision import client, schema
    from app.decision.tools import _demo_answers, build_result
    from app.demo import is_demo_mode

    normalized, problems = schema.validate_request(state, questions, schema.DecisionLimits.from_settings())
    if problems:
        raise client.DecisionError("invalid_questions", "; ".join(problems))
    if is_demo_mode():
        data = {"model": "microsoft-decision-1 (demo)", "answers": _demo_answers(state, normalized)}
        result, usage = build_result(data, normalized, "demo")
        return result["answers"][QUESTION_NAME], "demo", {}
    target = client.resolve_target()
    if target is None:
        raise client.DecisionError("decision_unavailable", "no decision offering is registered")
    seconds = timeout_seconds()
    data = await asyncio.wait_for(
        client.evaluate(target, state, normalized, timeout=seconds, retry=False),
        # httpx bounds each phase; this bounds the whole call (auth included).
        timeout=seconds + 1.0,
    )
    result, usage = build_result(data, normalized, target.offering_id)
    return result["answers"][QUESTION_NAME], target.offering_id, dict(usage)


def _record(offering_id: str, usage: dict[str, Any], ticket: Ticket | None) -> None:
    """One helper ledger record per key, and the tokens on the request's ticket (D9)."""
    if offering_id == "demo":
        return
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if ticket is not None:
        ticket.add_tokens(input_tokens, output_tokens)
    try:
        from app.usage.ledger import append_decision_usage

        append_decision_usage(
            offering_id=offering_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            lane="openai-api",
            purpose=LEDGER_PURPOSE,
            key_id=getattr(ticket, "key_id", None),
        )
    except Exception:
        logger.warning("usage ledger routing append failed", exc_info=True)


def _full(scope: KnowledgeScope, reason: str, probability: float | None) -> RoutingOutcome:
    return RoutingOutcome(ALL_ROUTE, reason, probability, scope, None)


async def route_question(
    agent_id: str, scope: KnowledgeScope, state: str, ticket: Ticket | None = None
) -> RoutingOutcome:
    """Classify ``state`` and return the run scope (raises :class:`RoutingUnavailable`)."""
    routing = scope.routing
    if routing is None:  # check_request refused this earlier; defensive
        return _full(scope, ERROR, None)
    try:
        answer, offering_id, usage = await _ask(state, build_question(routing))
    except Exception as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        logger.warning("Routing failed for agent %s: %s (on_error=%s)", agent_id, code, routing.on_error)
        if routing.on_error == "fail":
            raise RoutingUnavailable(code) from exc
        return _full(scope, ERROR, None)
    _record(offering_id, usage, ticket)

    route_id, probability = _top(answer)
    route = routing.route(route_id) if route_id else None
    if route is None:
        # An unreadable or unknown answer is a failed call, not a low-confidence one.
        logger.warning("Routing for agent %s returned no known route (on_error=%s)", agent_id, routing.on_error)
        if routing.on_error == "fail":
            raise RoutingUnavailable("decision_unreadable")
        return _full(scope, ERROR, None)

    bar = min_probability(routing)
    if probability is None or probability < bar:
        logger.info(
            "Routing for agent %s: low confidence (top=%s p=%s bar=%.2f, %s)",
            agent_id,
            route.id,
            "-" if probability is None else f"{probability:.2f}",
            bar,
            routing.low_confidence,
        )
        if routing.low_confidence == "clarify":
            return RoutingOutcome(NONE_ROUTE, LOW_CONFIDENCE, probability, None, None)
        return _full(scope, LOW_CONFIDENCE, probability)

    run_scope = scope.narrowed_to(route)
    logger.info("Routing for agent %s: route=%s p=%.2f", agent_id, route.id, probability)
    return RoutingOutcome(
        route.id,
        MATCHED,
        probability,
        run_scope,
        Narrowing(run_scope.rag_collections, run_scope.ontologies),
    )


__all__ = [
    "ALL_ROUTE",
    "CLARIFY_ANSWER",
    "ERROR",
    "INVALID_ROUTE",
    "LEDGER_PURPOSE",
    "LOW_CONFIDENCE",
    "MATCHED",
    "NONE_ROUTE",
    "REASONS",
    "ROUTE_AUTO",
    "ROUTE_NOT_AVAILABLE",
    "ROUTE_QUESTION_INSTRUCTIONS",
    "ROUTING_UNAVAILABLE",
    "RoutingOutcome",
    "RoutingUnavailable",
    "build_question",
    "check_request",
    "min_probability",
    "route_question",
    "timeout_seconds",
]
