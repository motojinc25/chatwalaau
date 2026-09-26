"""Live delegation runner (CTR-0226, PRP-0188, UDR-0170 D4 / D5 / D10).

A GPT-Live client delegation carries NO task text -- only an id and an offset. By
the time this runs, the controller has settled the transcript (closed and persisted
every turn up to the delegation), so the user's request is the last user message in
the session file. This module then:

1. runs the ACTIVE Prompt agent (Core or Custom) headlessly on the thread -- its
   tools (function calling), MCP servers, skills and history provider -- on the chat
   model the catalog's ``live_delegation`` task role names, or on the agent's own
   model when the role is unset (step 2), with a synthetic instruction that is NOT
   persisted;
2. returns the full answer (text, tool calls, activity log, usage) for the chat;
3. derives a SPOKEN summary of at most ``SPOKEN_TOKEN_CAP`` tokens: the answer
   itself when it is short plain prose, otherwise one helper call on the same model;
4. records the run in the ledger as lane ``live`` (and the helper as ``kind:
   helper``).

The run shape follows the Teams lane (``teams/agent_run.py``, CTR-0140): the same
registry agent, the same ``_resilient_run`` retry wrapper, the same sanitiser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import TYPE_CHECKING, Any
import uuid

from agent_framework import add_usage_details

from app.agui.sanitize import sanitize_text

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Spoken summary budget. GPT-Live accepts at most 500 tokens per append (S7); the
#: margin covers estimation error.
SPOKEN_TOKEN_CAP = 400

#: The instruction that starts the delegated run. Never persisted (UDR-0170 D4).
DELEGATION_PROMPT = (
    "[Live voice delegation] The conversation above includes a live voice exchange "
    "(messages transcribed from speech; they may contain recognition errors). Answer "
    "the user's latest spoken request. Use your tools when they help. Write the "
    "complete answer: it is shown in the chat, and a short spoken summary is "
    "produced from it separately."
)

SUMMARY_SYSTEM_PROMPT = (
    "You turn a written answer into what a voice assistant says aloud. Write at most "
    "{words} words of plain speech in the same language as the answer. No markdown, "
    "no lists, no code, no tables, no URLs. Keep the key facts and numbers. End by "
    "saying briefly that the full answer is in the chat."
)

_CODE_FENCE = re.compile(r"```")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_NOISE = re.compile(r"(^\s*#+\s*|^\s*[-*+]\s+|\*\*|__|`)", re.MULTILINE)


@dataclass
class DelegationOutcome:
    """What one delegated run produced."""

    text: str
    spoken: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    activity_log: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    failed: bool = False


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 ASCII characters per token, 1 per other character.

    Deliberately pessimistic for CJK text, where one character is often a token.
    """
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return (ascii_chars + 3) // 4 + (len(text) - ascii_chars)


def is_plain_prose(text: str) -> bool:
    """True when the answer can be read aloud as-is (no code, table or image)."""
    return not (_CODE_FENCE.search(text) or _TABLE_ROW.search(text) or _IMAGE_MD.search(text))


def truncate_to_tokens(text: str, cap: int = SPOKEN_TOKEN_CAP) -> str:
    """Cut ``text`` so ``estimate_tokens`` stays within ``cap`` (sentence-friendly)."""
    if estimate_tokens(text) <= cap:
        return text
    cut = text
    while cut and estimate_tokens(cut) > cap:
        cut = cut[: max(1, int(len(cut) * 0.9))]
    for stop in (". ", "。", "! ", "? ", "\n"):
        idx = cut.rfind(stop)
        if idx > len(cut) // 2:
            return cut[: idx + len(stop)].strip()
    return cut.strip()


def speakable(text: str) -> str:
    """Strip light markdown so a plain answer reads naturally."""
    return _MD_NOISE.sub("", text).strip()


def _tool_result_text(content: Any) -> str | None:
    raw = getattr(content, "result", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(raw)


def _args_text(content: Any) -> str | None:
    args = getattr(content, "arguments", None)
    if args is None:
        return None
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


async def run_delegated_agent(
    *,
    thread_id: str,
    agent_registry: Any,
    temporary: bool,
    text_sink: list[str] | None = None,
    progress: Callable[..., None] | None = None,
) -> tuple[DelegationOutcome, dict[str, Any] | None, int, str]:
    """Run the active Prompt agent on ``thread_id``; return the outcome and raw usage.

    Returns ``(outcome_without_spoken, turn_usage, model_calls, effective_model)``.
    ``text_sink`` receives the answer text as it streams, so a caller that times the
    run out still has the partial answer. ``progress(text, tools)`` is called after
    each update with the text so far and the tool calls (step 3: the in-chat marker
    shows the run as it happens); it throttles itself.
    """
    from agent_framework import AgentSession, Message

    from app.agent.agent_memory import session_agent_memory_snapshot
    from app.agent.temporary import set_temporary_run
    from app.agent.user_memory import session_user_profile_snapshot
    from app.agui.endpoint import (  # type: ignore[attr-defined]
        _image_gen_thread_id,
        _resilient_run,
        _RetryNotice,
    )

    # The chat model is the catalog's `live_delegation` task role when it is bound,
    # else the selected agent's own model (PRP-0188 step 2, UDR-0170 D4). Either way
    # the agent is the ACTIVE Prompt agent: the registry builds every per-model Agent
    # from the active spec, so its tools, MCP servers and skills are the same.
    from app.models_catalog import resolve_task_model

    effective_model = resolve_task_model("live_delegation", agent_registry.default_model)
    agent = agent_registry.get(effective_model or None)

    set_temporary_run(temporary)
    _image_gen_thread_id.set(thread_id)
    profile_snapshot = None if temporary else session_user_profile_snapshot(thread_id)
    memory_snapshot = None if temporary else session_agent_memory_snapshot(thread_id)
    run_options: dict[str, Any] = {}
    extra = agent_registry.run_instructions(
        effective_model,
        user_profile_block=profile_snapshot,
        agent_memory_block=memory_snapshot,
        temporary=temporary,
    )
    if extra:
        run_options["instructions"] = extra

    # FileHistoryProvider (CTR-0014) loads the thread -- including the live turns the
    # controller just committed -- through metadata["ag_ui_thread_id"]. Its after_run
    # is a no-op, so the synthetic prompt below is never written to the session.
    session = AgentSession()
    session.metadata = {"ag_ui_thread_id": thread_id, "ag_ui_run_id": uuid.uuid4().hex}
    input_messages = [Message(role="user", contents=[DELEGATION_PROMPT])]

    text_parts: list[str] = []
    calls: dict[str, dict[str, Any]] = {}
    activity: list[dict[str, Any]] = []
    current_call: str | None = None
    turn_usage: dict[str, Any] | None = None
    model_calls = 0

    async for update in _resilient_run(agent, input_messages, session, run_options):
        if isinstance(update, _RetryNotice):
            continue
        for content in getattr(update, "contents", None) or []:
            ctype = getattr(content, "type", None)
            if ctype == "text":
                text = getattr(content, "text", None)
                if text:
                    text_parts.append(text)
                    if text_sink is not None:
                        text_sink.append(text)
            elif ctype == "usage":
                details = getattr(content, "usage_details", None) or {}
                turn_usage = dict(add_usage_details(turn_usage, dict(details)))
                model_calls += 1
            elif ctype == "function_call":
                # Streamed like the AG-UI seam sees it: the first chunk names the
                # tool, later chunks carry argument deltas for the current call.
                call_id = getattr(content, "call_id", None)
                name = getattr(content, "name", None)
                if name and call_id not in calls:
                    call_id = call_id or uuid.uuid4().hex
                    calls[call_id] = {"id": call_id, "name": name, "status": "running"}
                    activity.append({"type": "toolCall", "id": call_id})
                    current_call = call_id
                target = calls.get(call_id) if call_id in calls else calls.get(current_call or "")
                args = _args_text(content)
                if target is not None and args:
                    target["args"] = (target.get("args") or "") + args
            elif ctype == "function_result":
                call_id = getattr(content, "call_id", None)
                entry = calls.get(call_id) if call_id else None
                if entry is not None:
                    entry["status"] = "completed"
                    result = _tool_result_text(content)
                    if result is not None:
                        entry["result"] = result
        if progress is not None:
            try:
                progress("".join(text_parts), list(calls.values()))
            except Exception:  # progress is display only; it never fails the run
                logger.debug("Live: progress callback failed", exc_info=True)

    cleaned, stripped = sanitize_text("".join(text_parts))
    if stripped:
        logger.warning("Live: removed model-private citation markup: count=%d", len(stripped))
    for entry in calls.values():
        if entry["status"] == "running":
            entry["status"] = "completed"
    outcome = DelegationOutcome(
        text=cleaned.strip(),
        spoken="",
        tool_calls=list(calls.values()),
        activity_log=activity,
    )
    return outcome, turn_usage, model_calls, effective_model


async def spoken_summary(text: str, *, model: str, thread_id: str) -> str:
    """The text GPT-Live speaks for ``text`` (UDR-0170 D5), at most SPOKEN_TOKEN_CAP."""
    if not text:
        return ""
    if is_plain_prose(text) and estimate_tokens(text) <= SPOKEN_TOKEN_CAP:
        return speakable(text)
    try:
        from agent_framework import Message

        from app.agui.agent_registry import _build_chat_client
        from app.usage.ledger import append_helper_usage

        client = _build_chat_client(model)
        messages = [
            Message(role="system", contents=[SUMMARY_SYSTEM_PROMPT.format(words=120)]),
            Message(role="user", contents=[text[:24000]]),
        ]
        response = await client.get_response(messages, stream=False)
        append_helper_usage(
            purpose="live-summary",
            usage_details=getattr(response, "usage_details", None),
            model=model,
            thread_id=thread_id,
        )
        summary = (getattr(response, "text", "") or "").strip()
        if summary:
            return truncate_to_tokens(speakable(summary))
    except Exception:
        logger.warning("Live: spoken summary helper failed; falling back to a truncation", exc_info=True)
    # Fallback: the prose part of the answer, truncated. Never read code or tables.
    prose = _IMAGE_MD.sub("", text)
    prose = re.sub(r"```.*?```", "", prose, flags=re.DOTALL)
    prose = _TABLE_ROW.sub("", prose)
    return truncate_to_tokens(speakable(prose)) or "The full answer is in the chat."


def record_usage(
    *,
    turn_usage: dict[str, Any] | None,
    model_calls: int,
    model: str,
    thread_id: str,
    temporary: bool,
    outcome: str,
) -> dict[str, Any] | None:
    """Append the ledger record (lane ``live``) and return the per-message usage."""
    if not turn_usage or model_calls <= 0:
        return None
    summary: dict[str, Any] | None = None
    try:
        from app import providers
        from app.agui.token_usage import turn_summary
        from app.usage.ledger import append_turn_usage

        summary = turn_summary(
            turn_usage,
            model_calls=model_calls,
            includes_cache_read=providers.input_tokens_include_cache_read(model),
        )
        append_turn_usage(
            lane="live",
            turn=summary,
            model=model,
            thread_id=thread_id,
            temporary=temporary,
            outcome=outcome,
        )
    except Exception:
        logger.warning("Live: usage ledger append failed for thread %s", thread_id, exc_info=True)
    usage: dict[str, Any] = {"model": model}
    for key in ("input_token_count", "output_token_count", "total_token_count"):
        value = turn_usage.get(key)
        if isinstance(value, int):
            usage[key] = value
    return usage
