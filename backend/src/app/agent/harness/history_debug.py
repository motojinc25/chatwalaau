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

    __slots__ = (
        "ambiguous_calls",
        "families",
        "messages",
        "mode",
        "oldest_family",
        "orphan_calls",
        "orphan_results",
        "todos",
    )

    def __init__(
        self,
        *,
        messages: int = 0,
        orphan_calls: int = 0,
        orphan_results: int = 0,
        ambiguous_calls: int = 0,
        families: int = 0,
        oldest_family: str = "-",
        todos: int = 0,
        mode: str = "-",
    ) -> None:
        self.messages = messages
        self.orphan_calls = orphan_calls
        self.orphan_results = orphan_results
        self.ambiguous_calls = ambiguous_calls
        self.families = families
        self.oldest_family = oldest_family
        self.todos = todos
        self.mode = mode

    def __str__(self) -> str:
        return (
            f"stored_messages={self.messages} orphan_calls={self.orphan_calls} "
            f"orphan_results={self.orphan_results} ambiguous_calls={self.ambiguous_calls} "
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


def count_orphan_results(messages: Any) -> int:
    """``function_result`` contents whose ``function_call`` is not in ``messages``.

    The mirror of ``count_orphan_calls``, and the one this lane actually failed on
    (PRP-0149 Finding B): compaction collapsed the group holding a declaration and
    kept the group holding its result, so the request carried an output with no call
    and the provider rejected it. Measured here, one layer above the request, because
    a property a consumer requires must be measured where it is produced (UDR-0127 D3).
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
        return len(answered - called)
    except Exception:  # a counter must never break a run
        logger.exception("[history] failed to count orphan results")
        return -1


def count_ambiguous_calls(messages: Any) -> int:
    """``call_id``s declared by more than one ``function_call`` content.

    THE precondition of the PRP-0149 failure. MAF's compaction refuses to link a
    result to a declaration whose ``call_id`` has more than one unmatched occurrence
    (``_compaction.py:105-122``), so a non-zero count here means the store is in the
    state that makes compaction split a call/result family. After the C1 normaliser
    this MUST be 0; a non-zero value afterwards is a regression signal rather than a
    mystery (UDR-0127 D3).
    """
    try:
        counts: dict[str, int] = {}
        for content in _iter_contents(messages):
            if getattr(content, "type", None) != "function_call":
                continue
            call_id = getattr(content, "call_id", None)
            if call_id:
                counts[call_id] = counts.get(call_id, 0) + 1
        return sum(1 for total in counts.values() if total > 1)
    except Exception:  # a counter must never break a run
        logger.exception("[history] failed to count ambiguous calls")
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
        snap.orphan_results = count_orphan_results(messages)
        snap.ambiguous_calls = count_ambiguous_calls(messages)
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
                        "[history] thread=%s source=%s stored=%d loaded=%d orphan_calls=%d "
                        "orphan_results=%d ambiguous_calls=%d families=%d oldest=%s",
                        thread_id,
                        _source_id,
                        stored,
                        len(messages),
                        count_orphan_calls(messages),
                        count_orphan_results(messages),
                        count_ambiguous_calls(messages),
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


def attach_compaction_tracing(agent: Any, *, thread_id: str) -> bool:
    """Make the in-run compaction say what it did (PRP-0149 C4, UDR-0127 D3).

    The harness wires its BEFORE strategy as the agent's ``compaction_strategy`` chat
    option, so it runs per model call, inside the client
    (``agent_framework/_harness/_agent.py:99-107``). That is the pass which took the
    reported turn from 268 messages to 116 between two model calls -- and it logged
    nothing at all, so the collapse had to be reconstructed by reading framework
    source. One line per firing closes that.

    ``split_families`` is the number that matters: results left with no declaration
    among the messages compaction KEPT. It is the defect the request seam then
    rejects, measured where it is created rather than where it fails.

    Wraps the callable the framework built; idempotent and best-effort.
    """
    try:
        strategy = getattr(agent, "compaction_strategy", None)
        if strategy is None or getattr(strategy, "_chatwalaau_traced", False):
            return strategy is not None

        async def traced(messages: Any, _inner: Any = strategy) -> Any:
            before_total = len(messages or [])
            before_included = _count_included(messages)
            changed = await _inner(messages)
            try:
                after_total = len(messages or [])
                included = _included(messages)
                logger.info(
                    "[harness compaction] thread=%s fired=%s messages=%d->%d included=%d->%d "
                    "summaries=+%d split_families=%d",
                    thread_id,
                    bool(changed),
                    before_total,
                    after_total,
                    before_included,
                    len(included),
                    max(after_total - before_total, 0),
                    count_orphan_results(included),
                )
            except Exception:  # tracing must never break a compaction pass
                logger.exception("[harness compaction] failed to describe a compaction pass")
            return changed

        traced._chatwalaau_traced = True  # type: ignore[attr-defined]
        agent.compaction_strategy = traced
        return True
    except Exception:  # attaching must never break agent construction
        logger.exception("[harness compaction] failed to attach compaction tracing")
    return False


def _excluded_key() -> str:
    """MAF's own exclusion marker, DERIVED rather than mirrored (UDR-0128 D8).

    Only the fallback path uses this; ``_included`` prefers MAF's own helper. The
    literal remains for a MAF that stops exporting the name.
    """
    try:
        from agent_framework import EXCLUDED_KEY

        return str(EXCLUDED_KEY)
    except Exception:  # pragma: no cover - a MAF that no longer exports the marker
        logger.warning("[harness compaction] MAF no longer exports EXCLUDED_KEY; assuming '_excluded'")
        return "_excluded"


def _included(messages: Any) -> list[Any]:
    """The messages compaction has NOT excluded (UDR-0128 D8).

    DELEGATES to MAF's own ``included_messages`` rather than re-implementing its
    exclusion test. This function is the base of every compaction counter UDR-0127 D3
    requires, and a mirror that drifts here does not FAIL -- it goes BLIND: nothing
    matches the stale marker, every message counts as included, and
    ``split_families`` reports 0 forever while the defect it exists to catch runs
    free. UDR-0109 D4 settled this class of question for the skills discovery mirror
    (derive from the live package, never hardcode a value while claiming to mirror a
    constant); delegating to the exported helper is the same rule taken one step
    further, because it also survives a change in HOW exclusion is represented, not
    just in what the key is called.

    The local filter is kept as a fallback for a MAF that stops exporting the helper.
    """
    items = list(messages or [])
    try:
        from agent_framework import included_messages

        return list(included_messages(items))
    except Exception:  # pragma: no cover - fallback for a MAF without the helper
        key = _excluded_key()
        return [m for m in items if not (getattr(m, "additional_properties", None) or {}).get(key, False)]


def _count_included(messages: Any) -> int:
    try:
        return len(_included(messages))
    except Exception:
        return -1


__all__ = [
    "HistorySnapshot",
    "attach_compaction_tracing",
    "attach_history_tracing",
    "count_ambiguous_calls",
    "count_orphan_calls",
    "count_orphan_results",
    "log_run_start",
    "reasoning_families",
    "snapshot",
]
