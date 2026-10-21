"""The ``decision_evaluate`` agent tool (PRP-0219, CTR-0248, UDR-0201 D3-D11).

One tool for every combination the service accepts: one question, several questions of
one type, or several types, all about ONE input (operator A6 = interpretation (a)).
It is registered exactly when the catalog has a ``decision`` offering, or in
DEMO_MODE (UDR-0201 D3, ``agui.agent_factory``).

The tool never raises into the agent run: every outcome is a JSON string -- the
normalized result (D7), ``invalid_questions`` with the problems (D6), or a
``decision_*`` error (D8). A successful call appends one helper ledger record (D9).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any

from pydantic import Field

from app.decision import client, schema

logger = logging.getLogger(__name__)

#: The description the agent reads (UDR-0201 D10). Pinned by an invariant test.
TOOL_DESCRIPTION = (
    "Judge ONE input with typed questions on the Microsoft-Decision-1 model and get "
    "probabilities back. Question types: 'noul' (yes/no -> probability of true), "
    "'choice' (pick one of fixed options -> the choice and a probability per option), "
    "'score' (a position on an ordered scale, lowest level first -> a score, the nearest "
    "level and its label). Put EVERY question about the same input in ONE call; call once "
    "per input. The result is numbers without a reason: explain the outcome from the "
    "probabilities and say when the top probability is low. Never use the result as the "
    "sole basis of a consequential decision about a person (credit, employment, housing, "
    "healthcare, legal)."
)

_STATE_HELP = "The input to judge: plain text, or a JSON object (for example a ticket with fields)."
_QUESTIONS_HELP = (
    "An object mapping a question name (letters, digits, _) to {type, instructions, criteria}. "
    "type: 'noul' | 'choice' | 'score'. instructions: the question. criteria: for 'choice' an "
    "object {label: description} with at least 2 options; for 'score' a list of level "
    'descriptions, lowest first, at least 2; for \'noul\' optional {"true": ..., "false": ...}.'
)


def _current_thread_id() -> str | None:
    try:
        from app.image_gen.tools import current_thread_id

        return current_thread_id.get("") or None
    except Exception:
        return None


def _demo_answers(state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Deterministic placeholder answers in the wire shape (UDR-0201 D11): a hash decides."""
    seed = json.dumps({"state": state, "questions": questions}, sort_keys=True, ensure_ascii=False)
    answers: dict[str, Any] = {}
    for name, question in questions.items():
        digest = int(hashlib.sha256(f"{seed}|{name}".encode()).hexdigest(), 16)
        qtype = question["type"]
        if qtype == "noul":
            answers[name] = {"type": "noul", "probability": round((digest % 1000) / 1000, 3)}
        elif qtype == "choice":
            labels = list(question["criteria"])
            pick = digest % len(labels)
            rest = round(0.3 / max(1, len(labels) - 1), 4)
            answers[name] = {
                "type": "choice",
                "choice": labels[pick],
                "probabilities": {lab: (0.7 if i == pick else rest) for i, lab in enumerate(labels)},
            }
        else:
            levels = len(question["criteria"])
            pick = digest % levels
            rest = round(0.3 / max(1, levels - 1), 4)
            probs = [0.7 if i == pick else rest for i in range(levels)]
            answers[name] = {
                "type": "score",
                "score": sum(i * p for i, p in enumerate(probs)),
                "probabilities": probs,
            }
    return answers


def build_result(
    data: dict[str, Any], questions: dict[str, dict[str, Any]], offering_id: str
) -> tuple[dict[str, Any], dict[str, int | None]]:
    """The normalized result (UDR-0201 D7) and the usage figures for the ledger."""
    usage = schema.read_usage(data.get("usage"))
    result: dict[str, Any] = {
        "model": data.get("model") if isinstance(data.get("model"), str) else None,
        "offering": offering_id,
        "answers": schema.read_answers(data.get("answers"), questions),
        "usage": usage,
    }
    return result, usage


def _error(code: str, message: str, **extra: Any) -> str:
    return json.dumps({"error": code, "message": message, **extra}, ensure_ascii=False)


async def decision_evaluate(
    state: Annotated[str | dict[str, Any], Field(description=_STATE_HELP)],
    questions: Annotated[dict[str, dict[str, Any]], Field(description=_QUESTIONS_HELP)],
) -> str:
    # The docstring is replaced by TOOL_DESCRIPTION below (one pinned source).
    limits = schema.DecisionLimits.from_settings()
    normalized, problems = schema.validate_request(state, questions, limits)
    if problems:
        return json.dumps({"error": "invalid_questions", "problems": problems}, ensure_ascii=False)

    from app.demo import is_demo_mode

    if is_demo_mode():
        result, _usage = build_result(
            {"model": "microsoft-decision-1 (demo)", "answers": _demo_answers(state, normalized)},
            normalized,
            "demo",
        )
        result["demo"] = True
        return json.dumps(result, ensure_ascii=False)

    target = client.resolve_target()
    if target is None:
        return _error("decision_unavailable", "no decision offering is registered in the Model Offering Catalog")
    try:
        data = await client.evaluate(target, state, normalized)
    except client.DecisionError as exc:
        logger.info("decision_evaluate failed: %s (HTTP %s)", exc.code, exc.status or "-")
        return _error(exc.code, exc.message)
    except Exception as exc:  # never fail the turn (UDR-0201 D8)
        logger.warning("decision_evaluate unexpected failure", exc_info=True)
        return _error("decision_failed", type(exc).__name__)

    result, usage = build_result(data, normalized, target.offering_id)
    from app.usage.ledger import append_decision_usage

    append_decision_usage(
        offering_id=target.offering_id,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        thread_id=_current_thread_id(),
    )
    return json.dumps(result, ensure_ascii=False)


decision_evaluate.__doc__ = TOOL_DESCRIPTION

__all__ = ["TOOL_DESCRIPTION", "build_result", "decision_evaluate"]
