"""Per-conversation harness session cache (CTR-0009 v-next, PRP-0135, UDR-0119 D3/D4).

A harness agent is a per-conversation RUN-TARGET. Its live state -- the MAF
InMemoryHistoryProvider history, todo list, mode, and loop state -- lives in ONE
(agent, AgentSession) pair per (harness_id, thread_id), cached here so it
survives across turns of one conversation. Authoring writes and reloads clear the
cache so an edited YAML takes effect on the next turn.

The session is also SAVED at the end of each turn (``save_if_current``) and
RESTORED when the cache builds the conversation again -- after a restart, an
eviction or a cache clear (CTR-0279, PRP-0243, UDR-0224 D4). A changed YAML
restores the history only; the todo list and the mode start fresh.

The conversation's project folder (PRP-0244, UDR-0225 D1-D4) arrives with each turn.
It is validated here; a turn naming another folder than the one the runtime was
built for rebuilds the runtime (file store and shell) and KEEPS the session.

The cache is bounded; eviction closes the entry's persistent LocalShellTool
process (single-session ownership).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import os
from typing import Any

from app.agent.harness import history_debug, history_normalize, persist
from app.agent.harness.factory import HarnessRuntime, build_harness_runtime
from app.agent.harness.loader import resolve_spec
from app.agent.harness.spec import HarnessAgentError

logger = logging.getLogger(__name__)

# Bound on live harness conversations; oldest evicted (shell closed) beyond it.
_MAX_ENTRIES = 32


class _Entry:
    def __init__(self, runtime: HarnessRuntime, session: Any, model_id: str, spec_fingerprint: str = "") -> None:
        self.runtime = runtime
        self.session = session
        self.model_id = model_id
        # The YAML this runtime was BUILT from (UDR-0224 D3), saved with the session.
        self.spec_fingerprint = spec_fingerprint


def _history_source_id(agent: Any) -> str:
    """The source id of MAF's own history provider on this agent (UDR-0119 D4)."""
    from agent_framework import InMemoryHistoryProvider

    for provider in getattr(agent, "context_providers", None) or []:
        if isinstance(provider, InMemoryHistoryProvider):
            return str(provider.source_id)
    return InMemoryHistoryProvider.DEFAULT_SOURCE_ID


def _restore_session(harness_id: str, thread_id: str, runtime: HarnessRuntime, spec: Any) -> Any | None:
    """The saved session for this conversation, per the UDR-0224 D4 rules, or None."""
    snapshot = persist.load(thread_id, harness_id)
    if snapshot is None:
        return None
    session = snapshot["session"]
    if snapshot.get("spec_fingerprint") == spec.source_fingerprint:
        logger.info("[harness session] thread=%s harness=%s restored", thread_id, harness_id)
        return session
    model_changed = snapshot.get("model_id") != spec.model_id
    logger.info(
        "[harness session] thread=%s harness=%s restored history only (YAML changed%s)",
        thread_id,
        harness_id,
        ", model changed: reasoning dropped" if model_changed else "",
    )
    return persist.history_only(session, _history_source_id(runtime.agent), strip_reasoning=model_changed)


def resolve_project(project: str) -> tuple[str | None, str]:
    """``(absolute root, workspace-relative path)`` of a conversation's project folder.

    ``(None, "")`` without a workspace (the harness then has no file tools, shell or
    project). Raises ``HarnessAgentError`` for a refused folder (UDR-0225 D2), so the
    endpoint ends the turn with a clean RUN_ERROR; nothing is ever created here.
    """
    from app.coding.folders import FolderError, resolve_project_root
    from app.core.config import settings

    workspace = (settings.coding_workspace_dir or "").strip()
    if not workspace:
        return None, ""
    try:
        return resolve_project_root(workspace, project or "")
    except FolderError as exc:
        raise HarnessAgentError(str(exc)) from None


def _same_root(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _attach_observers(runtime: HarnessRuntime, thread_id: str) -> None:
    """The tracing and the history repair, on MAF's own provider instance of one agent."""
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


_cache: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
_lock = asyncio.Lock()


async def agent_for_thread(harness_id: str, thread_id: str, project: str = "") -> tuple[Any, Any, str]:
    """Return (agent, session, model_id) for one conversation, building on first use.

    ``project`` is the conversation's project folder (workspace-relative; "" is the
    workspace root). A folder other than the cached runtime's rebuilds the runtime and
    KEEPS the session (UDR-0225 D4).

    Raises ``HarnessAgentError`` (via resolve/build) for an unknown id, blocking
    warnings, DEMO_MODE or a refused project folder -- the caller surfaces that as a
    RUN_ERROR.
    """
    key = (harness_id, thread_id)
    project_abs, project_rel = resolve_project(project)
    async with _lock:
        entry = _cache.get(key)
        if entry is not None:
            _cache.move_to_end(key)
            if not _same_root(getattr(entry.runtime, "project_root", None), project_abs):
                spec = resolve_spec(harness_id)
                rebuilt = build_harness_runtime(spec, project_abs=project_abs, project_rel=project_rel)
                _attach_observers(rebuilt, thread_id)
                previous, entry.runtime = entry.runtime, rebuilt
                entry.model_id = spec.model_id
                entry.spec_fingerprint = spec.source_fingerprint
                logger.info(
                    "[harness session] thread=%s harness=%s project changed to %s (session kept)",
                    thread_id,
                    harness_id,
                    project_rel or "(workspace)",
                )
                await previous.aclose()
            return entry.runtime.agent, entry.session, entry.model_id

        spec = resolve_spec(harness_id)
        runtime = build_harness_runtime(spec, project_abs=project_abs, project_rel=project_rel)

        from agent_framework import AgentSession

        # MAF's DEFAULT session id (a lowercase UUID) on purpose: MAF 1.18.0 (#8123)
        # derives the harness file-memory folder from it through
        # `_storage_key_segment`, which keeps a literal-safe id verbatim and ENCODES
        # anything else -- a custom id would silently relocate every memory written
        # so far (UDR-0162 D3).
        session = _restore_session(harness_id, thread_id, runtime, spec) or AgentSession()
        _attach_observers(runtime, thread_id)
        _cache[key] = _Entry(runtime, session, spec.model_id, spec.source_fingerprint)
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

    Dropping a cached harness session removes its todo list and mode from memory
    (PRP-0148 Section 1.4). An eviction or a cache clear keeps the saved snapshot, so
    the next turn restores them (UDR-0224 D4); a truncate or delete removes it too.
    Either way the drop is never silent.
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


def save_if_current(harness_id: str, thread_id: str, session: Any) -> bool:
    """Save the conversation's session at the end of a turn (UDR-0224 D2).

    Only while ``session`` is still the cached one: a truncate or delete during the
    turn dropped the entry, and writing it back would restore history the operator
    removed (UDR-0119 D11). Synchronous and never raising -- it runs in the AG-UI
    endpoint's ``finally``. The check needs no lock: it does not await, so nothing
    else on the event loop runs between the check and the write.
    """
    entry = _cache.get((harness_id, thread_id))
    if entry is None or entry.session is not session:
        logger.info("[harness session] thread=%s harness=%s not saved (dropped during the turn)", thread_id, harness_id)
        return False
    return persist.save(
        thread_id,
        harness_id,
        session,
        spec_fingerprint=entry.spec_fingerprint,
        model_id=entry.model_id,
    )


async def drop_thread(thread_id: str, *, reason: str, keep_snapshot: bool = False) -> int:
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

    The saved snapshots go too (UDR-0224 D5), unless ``keep_snapshot`` -- archive
    moves them with the chat instead.

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
    if not keep_snapshot:
        try:
            persist.delete_thread(thread_id)
        except Exception:
            logger.exception("[harness session] failed to delete the snapshots of thread %s", thread_id)
    for entry in dropped:
        try:
            await entry.runtime.aclose()
        except Exception:
            logger.exception("[harness session] failed to close a dropped runtime")
    return len(dropped)


async def clear_cache(reason: str = "authoring_reload") -> None:
    """Drop every cached harness session.

    Called on an authoring write, and whenever what a harness agent was BUILT from
    changes outside its YAML: the Model Offering Catalog (an offering's capabilities,
    e.g. ``mcp: false``) and the MCP override store / MCP reload. A cached runtime keeps
    the tools it was built with, so without this an already-open harness conversation
    went on calling MCP tools its offering now withholds.

    The saved snapshots stay: the next turn rebuilds the runtime and restores the
    conversation (UDR-0224 D4), so a reload no longer erases harness memory.
    """
    async with _lock:
        entries = list(_cache.values())
        for key, entry in _cache.items():
            _log_drop(key, entry, reason=reason)
        _cache.clear()
    for entry in entries:
        await entry.runtime.aclose()


def project_report(harness_id: str, thread_id: str) -> dict[str, Any] | None:
    """The project folder and rules of a cached conversation, read now (UDR-0225 D8).

    For the ``harness_progress`` snapshot; never the rules text. None without a workspace
    or a cached entry. Never raises.
    """
    try:
        entry = _cache.get((harness_id, thread_id))
        provider = getattr(entry.runtime, "project_provider", None) if entry is not None else None
        if provider is None:
            return None
        from app.agent.harness.project_rules import describe

        return describe(provider.root_abs, provider.root_rel, rules_enabled=provider.rules_enabled)
    except Exception:
        logger.debug("[harness project] report failed", exc_info=True)
        return None


def cache_size() -> int:
    """Current number of live harness conversations (tests / diagnostics)."""
    return len(_cache)


__all__ = [
    "agent_for_thread",
    "cache_size",
    "clear_cache",
    "drop_thread",
    "project_report",
    "resolve_project",
    "save_if_current",
]
