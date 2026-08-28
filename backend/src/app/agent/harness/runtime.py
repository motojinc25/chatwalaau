"""Per-conversation harness session cache (CTR-0009 v-next, PRP-0135, UDR-0119 D3/D4).

A harness agent is a per-conversation RUN-TARGET. Its live state -- the MAF
InMemoryHistoryProvider history, todo list, mode, and loop state -- lives in ONE
(agent, AgentSession) pair per (harness_id, thread_id), cached here so it
survives across turns of one conversation and is lost on restart (the UDR-0072
D7 in-memory precedent). Authoring writes and reloads clear the cache so an
edited YAML takes effect on the next turn.

The cache is bounded; eviction closes the entry's persistent LocalShellTool
process (single-session ownership).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
from typing import Any

from app.agent.harness import history_debug, history_normalize
from app.agent.harness.factory import HarnessRuntime, build_harness_runtime
from app.agent.harness.loader import resolve_spec

logger = logging.getLogger(__name__)

# Bound on live harness conversations; oldest evicted (shell closed) beyond it.
_MAX_ENTRIES = 32


class _Entry:
    def __init__(self, runtime: HarnessRuntime, session: Any, model_id: str) -> None:
        self.runtime = runtime
        self.session = session
        self.model_id = model_id


_cache: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
_lock = asyncio.Lock()


async def agent_for_thread(harness_id: str, thread_id: str) -> tuple[Any, Any, str]:
    """Return (agent, session, model_id) for one conversation, building on first use.

    Raises ``HarnessAgentError`` (via resolve/build) for an unknown id, blocking
    warnings, or DEMO_MODE -- the caller surfaces that as a RUN_ERROR.
    """
    key = (harness_id, thread_id)
    async with _lock:
        entry = _cache.get(key)
        if entry is not None:
            _cache.move_to_end(key)
            return entry.runtime.agent, entry.session, entry.model_id

        spec = resolve_spec(harness_id)
        runtime = build_harness_runtime(spec)

        from agent_framework import AgentSession

        session = AgentSession()
        # PRP-0148 Section 4.1: make MAF's own history provider report what it loads.
        # Attached here rather than passed into the factory so UDR-0119 D4's "parameter
        # omitted" -- pinned by the PRP-0135 / PRP-0144 invariants -- stays literally true.
        history_debug.attach_history_tracing(runtime.agent, thread_id=thread_id)
        # PRP-0149 C4: the in-run compaction pass says what it collapsed and whether it
        # left a call/result family split (UDR-0127 D3).
        history_debug.attach_compaction_tracing(runtime.agent, thread_id=thread_id)
        # PRP-0149 C1: the store is kept identity-unique at the SAVE seam of the same
        # instance (UDR-0119 D12, UDR-0127 D2). This is the root fix -- the duplication
        # it removes is what makes MAF's compaction split a call from its result, and
        # the request seam cannot repair that from downstream.
        history_normalize.attach_history_normalization(runtime.agent, thread_id=thread_id)
        _cache[key] = _Entry(runtime, session, spec.model_id)
        logger.info("[harness session] thread=%s harness=%s created", thread_id, harness_id)
        while len(_cache) > _MAX_ENTRIES:
            evicted_key, evicted = _cache.popitem(last=False)
            # PRP-0148 Section 4.2: name the reason. LRU eviction silently REPAIRS a
            # polluted session today (RES-0003 Open Question 5), which distorts how
            # often the pollution is believed to happen. Naming it makes the
            # difference measurable.
            _log_drop(evicted_key, evicted, reason="lru_evicted")
            await evicted.runtime.aclose()
        return runtime.agent, session, spec.model_id


def _log_drop(key: tuple[str, str], entry: _Entry, *, reason: str) -> None:
    """One line per discarded conversation, naming what went with it (Section 4.2).

    Dropping a harness session also discards the agent's todo list and mode
    (PRP-0148 Section 1.4). That cost is accepted, but it is never silent.
    """
    try:
        harness_id, thread_id = key
        snap = history_debug.snapshot(entry.session)
        logger.info(
            "[harness session] thread=%s harness=%s dropped reason=%s discarded_%s",
            thread_id,
            harness_id,
            reason,
            snap,
        )
    except Exception:  # logging a drop must never break the drop
        logger.exception("[harness session] failed to describe a dropped session")


async def drop_thread(thread_id: str, *, reason: str) -> int:
    """Discard every cached harness conversation for one thread (UDR-0119 D11).

    A harness conversation's runtime session is state the operator CANNOT SEE, and
    any operation that rewinds or removes the VISIBLE conversation must discard it:
    the postcondition that matters is that the agent's history holds nothing the
    operator cannot see. RES-0003 F3 is what happens without this -- a truncated
    conversation reported ``first_turn=True`` while the agent still carried eight
    orphaned tool calls from two runs that had already died, and the next request
    was rejected because of them.

    A thread can hold entries for more than one ``harness_id`` when the run-target
    was switched mid-conversation, so every matching entry is dropped.

    Returns the number of conversations dropped. Never raises: a cleanup failure
    must not fail the user's edit.
    """
    dropped: list[_Entry] = []
    try:
        async with _lock:
            for key in [k for k in _cache if k[1] == thread_id]:
                entry = _cache.pop(key)
                _log_drop(key, entry, reason=reason)
                dropped.append(entry)
    except Exception:
        logger.exception("[harness session] failed to drop thread %s", thread_id)
    for entry in dropped:
        try:
            await entry.runtime.aclose()
        except Exception:
            logger.exception("[harness session] failed to close a dropped runtime")
    return len(dropped)


async def clear_cache() -> None:
    """Drop every cached harness session (authoring write / reload path)."""
    async with _lock:
        entries = list(_cache.values())
        for key, entry in _cache.items():
            _log_drop(key, entry, reason="authoring_reload")
        _cache.clear()
    for entry in entries:
        await entry.runtime.aclose()


def cache_size() -> int:
    """Current number of live harness conversations (tests / diagnostics)."""
    return len(_cache)


__all__ = ["agent_for_thread", "cache_size", "clear_cache", "drop_thread"]
