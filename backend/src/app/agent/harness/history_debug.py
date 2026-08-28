"""Visibility into the harness conversation's own history (PRP-0148 Section 4).

RES-0003 F6: the request trace reports the message list AFTER every middleware has
run, fused into one ``maf_messages=`` number. Nothing reported what the history
provider HELD, what it LOADED, or which session it read from -- which is why the
jump from 3 to 70 messages in the reported failure still cannot be read from the
log, even though the code path is understood.

This module closes that gap on the harness lane and nowhere else. Everything here
is counts, ids and shapes -- never arguments, results, or prompt text -- matching
the posture ``app.agent.approval_debug`` sets for the request seam.

Nothing here may raise. A broken instrument must degrade to silence, never to a
failed turn (UDR-0126 D5 posture, PRP-0148 Section 4.5).
"""

from __future__ import annotations

import logging
from typing import Any

from agent_framework import InMemoryHistoryProvider

logger = logging.getLogger("app.agent.harness_history")

# Prefix length used to group reasoning ids into the response family that produced
# them. Two calls in one provider response share this prefix; two different runs do
# not. RES-0003 F2 used exactly this to separate three runs inside one request.
_FAMILY_PREFIX = 16


class HistorySnapshot:
    """Counts describing one harness history store. All fields are best-effort."""

    __slots__ = ("families", "messages", "mode", "oldest_family", "orphan_calls", "todos")

    def __init__(
        self,
        *,
        messages: int = 0,
        orphan_calls: int = 0,
        families: int = 0,
        oldest_family: str = "-",
        todos: int = 0,
        mode: str = "-",
    ) -> None:
        self.messages = messages
        self.orphan_calls = orphan_calls
        self.families = families
        self.oldest_family = oldest_family
        self.todos = todos
        self.mode = mode

    def __str__(self) -> str:
        return (
            f"stored_messages={self.messages} orphan_calls={self.orphan_calls} "
            f"families={self.families} oldest={self.oldest_family} "
            f"todos={self.todos} mode={self.mode}"
        )


def _iter_contents(messages: Any) -> Any:
    for message in messages or []:
        yield from getattr(message, "contents", None) or []


def count_orphan_calls(messages: Any) -> int:
    """``function_call`` contents in ``messages`` with no matching result.

    The same definition the request seam uses, applied one layer up so a polluted
    store is visible BEFORE it is assembled into a request. An approval response
    counts as an answer, mirroring ``heal_dangling_tool_calls``.
    """
    try:
        called: set[str] = set()
        answered: set[str] = set()
        for content in _iter_contents(messages):
            ctype = getattr(content, "type", None)
            call_id = getattr(content, "call_id", None)
            if ctype == "function_call" and call_id:
                called.add(call_id)
            elif ctype == "function_result" and call_id:
                answered.add(call_id)
            elif ctype == "function_approval_response":
                wrapped = getattr(content, "function_call", None)
                approval_call = getattr(wrapped, "call_id", None) or getattr(content, "id", None)
                if approval_call:
                    answered.add(approval_call)
        return len(called - answered)
    except Exception:  # a counter must never break a run
        logger.exception("[history] failed to count orphan calls")
        return -1


def reasoning_families(messages: Any) -> list[str]:
    """Distinct response families present, oldest first (RES-0003 F2 technique)."""
    try:
        seen: list[str] = []
        for content in _iter_contents(messages):
            if getattr(content, "type", None) != "text_reasoning":
                continue
            rs_id = getattr(content, "id", None) or getattr(content, "raw_representation_id", None)
            if not isinstance(rs_id, str) or not rs_id:
                continue
            family = rs_id[:_FAMILY_PREFIX]
            if family not in seen:
                seen.append(family)
        return seen
    except Exception:
        logger.exception("[history] failed to group reasoning families")
        return []


def snapshot(session: Any) -> HistorySnapshot:
    """Describe what a harness ``AgentSession`` is holding. Never raises."""
    snap = HistorySnapshot()
    try:
        state = getattr(session, "state", None)
        if not isinstance(state, dict):
            return snap
        messages = (state.get(InMemoryHistoryProvider.DEFAULT_SOURCE_ID) or {}).get("messages") or []
        snap.messages = len(messages)
        snap.orphan_calls = count_orphan_calls(messages)
        families = reasoning_families(messages)
        snap.families = len(families)
        snap.oldest_family = families[0] if families else "-"
        # The todo list and the mode live in their own provider slices. They are
        # reported because dropping the session takes them with it (PRP-0148
        # Section 1.4), and a drop must never be silent about what it discarded.
        for value in state.values():
            if not isinstance(value, dict):
                continue
            items = value.get("items")
            if isinstance(items, list) and snap.todos == 0:
                snap.todos = len(items)
            mode = value.get("mode") or value.get("current_mode")
            if isinstance(mode, str) and snap.mode == "-":
                snap.mode = mode
    except Exception:  # snapshotting must never break a run
        logger.exception("[history] failed to snapshot session")
    return snap


def log_run_start(*, thread_id: str, harness_id: str, session: Any, cached: bool) -> None:
    """PRP-0148 Section 4.3 -- did this turn START polluted?

    An operator reporting "it failed again" can then say whether the agent began
    the turn already carrying debris from an earlier, aborted run.
    """
    try:
        snap = snapshot(session)
        logger.info(
            "[harness history] thread=%s harness=%s cached=%s %s",
            thread_id,
            harness_id,
            cached,
            snap,
        )
    except Exception:
        logger.exception("[harness history] failed to log run start")


def attach_history_tracing(agent: Any, *, thread_id: str) -> bool:
    """Make MAF's own history provider report what it loads (PRP-0148 Section 4.1).

    RES-0003 F6: the ``[wire]`` line reports the message list AFTER every middleware
    has run, fused into one ``maf_messages=`` number, so nothing said what the store
    HELD or LOADED -- which is why the jump from 3 to 70 messages in the reported
    failure still cannot be read from the log.

    The observer is attached to the provider INSTANCE the framework built, rather
    than a subclass being passed into ``create_harness_agent``. That is deliberate:
    UDR-0119 D4 says the history provider parameter is OMITTED, and PRP-0135 /
    PRP-0144 invariants pin exactly that. Adding observability must not require
    relaxing a shipped decision, and wrapping the real object is also a stronger
    guarantee than a subclass -- there is no constructor to get wrong, so this
    cannot become the deferred RES-0002 F1 behaviour change
    (``store_inputs=False``) by accident.

    Idempotent, and best-effort: returns True when an observer was attached. A
    failure here costs a log line, never a turn.
    """
    try:
        for provider in getattr(agent, "context_providers", None) or []:
            if not isinstance(provider, InMemoryHistoryProvider):
                continue
            if getattr(provider, "_chatwalaau_traced", False):
                return True
            inner = provider.get_messages

            # `_inner` and `_source_id` are bound as defaults, not captured: the closure
            # is created inside a loop and a late-binding capture would make every
            # wrapper describe the last provider (ruff B023).
            async def get_messages(
                session_id: str | None,
                *,
                state: Any = None,
                _inner: Any = inner,
                _source_id: str = getattr(provider, "source_id", "?"),
                **kwargs: Any,
            ) -> Any:
                messages = await _inner(session_id, state=state, **kwargs)
                try:
                    stored = len(state.get("messages", [])) if isinstance(state, dict) else -1
                    families = reasoning_families(messages)
                    logger.info(
                        "[history] thread=%s source=%s stored=%d loaded=%d orphan_calls=%d families=%d oldest=%s",
                        thread_id,
                        _source_id,
                        stored,
                        len(messages),
                        count_orphan_calls(messages),
                        len(families),
                        families[0] if families else "-",
                    )
                except Exception:  # tracing must never break the load
                    logger.exception("[history] failed to describe a history load")
                return messages

            provider.get_messages = get_messages  # type: ignore[method-assign]
            provider._chatwalaau_traced = True  # type: ignore[attr-defined]
            return True
    except Exception:  # attaching must never break agent construction
        logger.exception("[history] failed to attach history tracing")
    return False


__all__ = [
    "HistorySnapshot",
    "attach_history_tracing",
    "count_orphan_calls",
    "log_run_start",
    "reasoning_families",
    "snapshot",
]
