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
        _cache[key] = _Entry(runtime, session, spec.model_id)
        while len(_cache) > _MAX_ENTRIES:
            _, evicted = _cache.popitem(last=False)
            await evicted.runtime.aclose()
        return runtime.agent, session, spec.model_id


async def clear_cache() -> None:
    """Drop every cached harness session (authoring write / reload path)."""
    async with _lock:
        entries = list(_cache.values())
        _cache.clear()
    for entry in entries:
        await entry.runtime.aclose()


def cache_size() -> int:
    """Current number of live harness conversations (tests / diagnostics)."""
    return len(_cache)


__all__ = ["agent_for_thread", "cache_size", "clear_cache"]
