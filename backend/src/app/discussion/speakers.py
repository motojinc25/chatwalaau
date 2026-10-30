"""One AI Discussion model turn (CTR-0275, PRP-0232, UDR-0215 D1/D7/D9).

A turn is ONE stateless MAF ``Agent.run`` over messages the context builder made: no context
providers, no history provider, no Identity (D7), and no tools unless the seat has research tools
from the ``app.discussion.tools`` allowlist (PRP-0234, UDR-0217 D1); the moderator never has any. The client comes from the provider
factory (CTR-0102), the same seam every lane uses. Streamed content is folded by CTR-0266, so a
``replace`` update never doubles the text or the usage.

DEMO_MODE (and the browser tests, which switch the same seam) uses scripted replies instead of a
model: deterministic text per role and phase, and no ledger record.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

#: Extra output room for a reasoning model, on top of the reply length, by effort (PRP-0232 2.9).
#: The providers' ``max_tokens`` counts reasoning AND visible text, so a cap equal to the reply
#: length would leave a reasoning model with nothing to say; a higher effort reasons longer.
REASONING_HEADROOM_TOKENS: dict[str, int] = {
    "low": 4_000,
    "medium": 8_000,
    "high": 16_000,
    "xhigh": 24_000,
    "max": 32_000,
}
#: The moderator (and a turn with no effort given) thinks briefly: the lowest shared level.
TURN_EFFORT = "low"
#: Pace of a scripted reply, so the streaming UI is exercised like a real one.
SCRIPTED_CHUNK_CHARS = 24
SCRIPTED_DELAY_SEC = 0.01


def use_scripted_replies() -> bool:
    """True when turns are scripted instead of sent to a model (DEMO_MODE)."""
    from app.demo import is_demo_mode

    return is_demo_mode()


@dataclass
class TurnResult:
    text: str = ""
    #: The normalized ``turn`` summary (CTR-0009 shape); None when nothing was measured.
    usage: dict[str, Any] | None = None
    model_calls: int = 0
    scripted: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        """The tokens the turn used: its input (as the provider reported it) plus its output."""
        if not self.usage:
            return 0
        raw_input = self.usage.get("input_token_count")
        if raw_input is None:
            raw_input = self.usage.get("uncached_input_token_count")
        return int(raw_input or 0) + int(self.usage.get("output_token_count") or 0)


def estimate_tokens(messages: list[tuple[str, str]], max_output_tokens: int) -> int:
    """A deliberately generous pre-call estimate for the budget check (D9)."""
    chars = sum(len(text) for _, text in messages)
    return chars // 3 + max_output_tokens


def _options(model: str, max_output_tokens: int, schema: dict[str, Any] | None, effort: str | None) -> dict[str, Any]:
    from app import providers

    level = effort or TURN_EFFORT
    options = dict(providers.build_model_options(model, {"effort": level}) or {})
    if "max_tokens" in options:
        # A reasoning family: the reply length plus room for its reasoning, never more than the
        # tier the provider already chose.
        headroom = REASONING_HEADROOM_TOKENS.get(level, REASONING_HEADROOM_TOKENS["low"])
        options["max_tokens"] = min(int(options["max_tokens"]), max_output_tokens + headroom)
    else:
        options["max_tokens"] = max_output_tokens
    if providers.stores_responses_server_side(model):
        options.setdefault("store", False)
    if schema is not None:
        try:
            if providers.structured_output_support(model).get("supported", True):
                options = providers.merge_generation_options(
                    options, providers.build_structured_output(model, schema, "json_schema")
                )
        except Exception:
            logger.debug("structured output probe failed for %s", model, exc_info=True)
    return options


async def model_turn(
    *,
    model: str,
    messages: list[tuple[str, str]],
    max_output_tokens: int,
    result: TurnResult,
    schema: dict[str, Any] | None = None,
    effort: str | None = None,
    turn_tools: Any = None,
) -> AsyncIterator[str]:
    """Stream one turn's text deltas; ``result`` holds the whole text and usage at the end.

    ``turn_tools`` (a ``tools.TurnTools``) mounts the seat's research tools; MAF runs the tool
    loop inside this one ``Agent.run``, and every model call of it is in the folded usage.
    """
    from agent_framework import Agent, Message

    from app import providers
    from app.agent.stream_merge import TextFold, UsageFold, merge_mode
    from app.agui.token_usage import turn_summary

    client = providers.build_chat_client(model)
    mounted = turn_tools.build() if turn_tools is not None else []
    agent = Agent(
        name="ChatWalaau-Discussion",
        client=client,
        tools=mounted or None,
        context_providers=None,
        default_options=_options(model, max_output_tokens, schema, effort) or None,
    )
    maf_messages = [Message(role=role, contents=[text]) for role, text in messages]
    text_fold = TextFold("text")
    usage_fold = UsageFold()
    parts: list[str] = []
    async for update in agent.run(maf_messages, stream=True):
        for content in getattr(update, "contents", None) or []:
            kind = getattr(content, "type", None)
            if kind == "text":
                delta = text_fold.feed(getattr(content, "text", None), merge_mode(content))
                if delta:
                    parts.append(delta)
                    yield delta
            elif kind == "usage":
                usage_fold.observe(getattr(content, "usage_details", None), merge_mode(content))
            elif kind == "function_call" and turn_tools is not None and getattr(content, "name", "") == "web_search":
                turn_tools.note_hosted("web_search", str(getattr(content, "call_id", "") or ""))
    result.text = "".join(parts)
    result.model_calls = usage_fold.calls
    if usage_fold.turn:
        result.usage = turn_summary(
            usage_fold.turn,
            model_calls=max(usage_fold.calls, 1),
            includes_cache_read=providers.input_tokens_include_cache_read(model),
        )


# ---------------------------------------------------------------------------
# Scripted replies (DEMO_MODE, browser tests)
# ---------------------------------------------------------------------------

_SCRIPTED_VIEWS: dict[str, str] = {
    "proposer": "Start small: one guided flow, a fixed order of speakers and a check-in after each step.",
    "critic": "The main risk is cost and noise: cap the rounds and keep every reply short.",
    "implementer": "Build it on the existing chat: store the run with the session and reuse the stream.",
    "user_advocate": "Keep the controls few and obvious: pause, continue and summarize.",
    "code_analyst": "The code already has the pieces; the change touches one module.",
}
#: A reference a scripted Code seat adds that never resolves, so the "unverified" mark is exercised.
SCRIPTED_MISSING_CITATION = "missing_module.py:12"


def _scripted_participant(participant: dict[str, Any], phase: str) -> str:
    view = _SCRIPTED_VIEWS.get(str(participant.get("role")), "My view follows from my remit.")
    if phase == "opening":
        return f"**Opening view ({participant['display_name']})**\n\n- {view}\n- I would measure the result early."
    if phase.startswith("ask:"):
        return f"**Answer ({participant['display_name']})**\n\n- {view}"
    return f"**{phase.replace(':', ' ').title()} ({participant['display_name']})**\n\n- I keep my view: {view}"


def _scripted_moderator(log: list[dict[str, str]], final: bool) -> str:
    speakers = [e["speaker"] for e in log]
    if final:
        return json.dumps(
            {
                "recommendation": "Start with the small guided version and review it after first use.",
                "agreements": ["Keep replies short", "Ask the person after each step"],
                "dissent": ["The critic still worries about cost"],
                "open_issues": ["How many rounds are enough"],
                "next_actions": ["Write the first version", "Collect feedback"],
            }
        )
    return json.dumps(
        {
            "common": [f"{len(speakers)} contributions agree on a small first version"],
            "conflicts": ["How much should run without a check-in"],
            "missing": ["The expected number of users"],
            "questions_for_human": ["Should every step wait for your confirmation?"],
            "suggest_finish": {"value": False, "reason": "The main conflict is still open"},
        }
    )


def _scripted_report(log: list[dict[str, str]]) -> str:
    speakers = sorted({e["speaker"] for e in log})
    positions = "\n".join(f"- **{name}**: keeps a short, bounded first version." for name in speakers)
    return (
        "# Report\n\n## Background and question\n\nA scripted discussion.\n\n"
        f"## Positions\n\n{positions or '- (none)'}\n\n"
        "## Recommendation and next actions\n\nStart with the small guided version and review it after first use."
    )


async def scripted_turn(
    *,
    participant: dict[str, Any] | None,
    phase: str,
    log: list[dict[str, str]],
    result: TurnResult,
    final: bool = False,
    report: bool = False,
    code_hint: str | None = None,
) -> AsyncIterator[str]:
    """A deterministic reply, streamed in chunks (no request, no ledger record).

    ``code_hint`` (PRP-0236): a real ``path:line`` of the code folder that a Code seat cites,
    next to one that does not resolve.
    """
    if report:
        text = _scripted_report(log)
    elif participant is None:
        text = _scripted_moderator(log, final)
    else:
        text = _scripted_participant(participant, phase)
        if code_hint:
            text += f"\n- The entry point is `{code_hint}`.\n- An old note points at `{SCRIPTED_MISSING_CITATION}`."
    if participant is not None and participant.get("tools"):
        # A scripted seat "uses" each of its tools once, so the record and the UI are exercised.
        from app.discussion.tools import TOOL_LABELS

        used = list(participant["tools"])
        result.extra["tools"] = [{"name": t, "query": "scripted"} for t in used]
        text += "\n\nSources: " + ", ".join(TOOL_LABELS.get(t, t) for t in used) + " (scripted)"
    result.scripted = True
    result.model_calls = 1
    for start in range(0, len(text), SCRIPTED_CHUNK_CHARS):
        chunk = text[start : start + SCRIPTED_CHUNK_CHARS]
        await asyncio.sleep(SCRIPTED_DELAY_SEC)
        yield chunk
    result.text = text
    # Synthetic counts (like DemoChatClient's chars // 4) so the budget meter moves.
    result.usage = {"input_token_count": 200, "output_token_count": max(len(text) // 4, 1), "model_calls": 1}


# ---------------------------------------------------------------------------
# Advisory signals (PRP-0232 2.6) -- the Decision model only ADVISES (UDR-0215 D8)
# ---------------------------------------------------------------------------

ADVISORY_QUESTIONS: dict[str, dict[str, Any]] = {
    "repeating": {
        "type": "noul",
        "instructions": "Do the latest contributions mostly repeat points that were already made?",
    },
    "sufficient": {
        "type": "noul",
        "instructions": "Is the wanted outcome already covered well enough to write the final summary?",
    },
}


#: Scripted seat scores (DEMO_MODE, browser tests): the implementer and the user advocate fall below
#: the default threshold, so a scripted decision-mode run leaves a seat out and brings it back.
_SCRIPTED_SEAT_SCORES: dict[str, float] = {
    "proposer": 0.82,
    "critic": 0.74,
    "implementer": 0.31,
    "user_advocate": 0.22,
}


def decision_available() -> bool:
    """True when a decision offering answers (or turns are scripted): speaker selection is offered."""
    if use_scripted_replies():
        return True
    try:
        from app.decision import client

        return client.resolve_target() is not None
    except Exception:  # an unreadable catalog: no selection, never an error
        return False


async def advisory(state_text: str, seats: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """``{repeating, sufficient, source}`` probabilities, or None when no decision model answers.

    PRP-0235 (UDR-0218 D4): with ``seats`` the same call also scores each seat for the next
    round; the scores come back as ``seat_scores`` ``{seat_id: probability | None}``.
    """
    from app.discussion import selection

    if use_scripted_replies():
        out: dict[str, Any] = {"repeating": 0.2, "sufficient": 0.3, "source": "demo"}
        if seats:
            out["seat_scores"] = {p["id"]: _SCRIPTED_SEAT_SCORES.get(str(p.get("role")), 0.5) for p in seats}
        return out
    from app.decision import client
    from app.decision.schema import read_answers, read_usage

    target = client.resolve_target()
    if target is None:
        return None
    questions = dict(ADVISORY_QUESTIONS)
    if seats:
        questions.update(selection.seat_questions(seats))
    try:
        data = await client.evaluate(target, state_text[:90_000], questions)
    except Exception as exc:  # advisory only: never fail a phase
        logger.info("discussion advisory skipped: %s", getattr(exc, "code", type(exc).__name__))
        return None
    answers = read_answers(data.get("answers"), questions)
    usage = read_usage(data.get("usage"))
    out = {"source": "decision", "offering": target.offering_id}
    for name in ADVISORY_QUESTIONS:
        p = answers.get(name, {}).get("probability")
        out[name] = float(p) if isinstance(p, (int, float)) else None
    if seats:
        out["seat_scores"] = selection.scores_from_answers(seats, answers)
    out["usage"] = {
        "uncached_input_token_count": usage.get("input_tokens"),
        "output_token_count": usage.get("output_tokens"),
    }
    return out


__all__ = [
    "REASONING_HEADROOM_TOKENS",
    "TurnResult",
    "advisory",
    "decision_available",
    "estimate_tokens",
    "model_turn",
    "scripted_turn",
    "use_scripted_replies",
]
