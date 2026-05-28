"""Tool Approval Policy resolver and store (CTR-0099, PRP-0067, UDR-0043).

Three responsibilities:

1. ``resolve_require_set()`` -- maps ``TOOL_APPROVAL_MODE`` and
   ``TOOL_APPROVAL_REQUIRE_LIST`` to a ``frozenset[str]`` of tool names
   that should receive ``@tool(approval_mode="always_require")`` at
   registration time. Skip mode returns the empty set so no tool is
   wrapped (UDR-0043 D2).
2. ``wrap_with_approval(fn, require_set)`` -- decorates ``fn`` with
   ``@tool(approval_mode="always_require")`` iff ``fn.__name__`` is in
   ``require_set``; returns ``fn`` untouched otherwise. The decoration
   is one-shot at agent-factory build time (UDR-0043 D1).
3. ``approval_store`` -- a process-local registry of pending approval
   requests. Each entry pairs a ``thread_id`` and the
   ``function_approval_request`` content with an ``asyncio.Event`` that
   the AG-UI parked stream awaits. ``POST /api/tool-approval`` resolves
   the matching entry; a background sweeper drops expired records.
   Persistence is forbidden (UDR-0043 D6); the store lives in memory
   only.

Both ``resolve_require_set`` and ``wrap_with_approval`` are pure --
their behavior is fully determined by ``app.core.config.settings``.
The approval store carries shared state and is exposed via a singleton
helper, ``approval_store``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any, Literal
import uuid

from app.core.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

_logger = logging.getLogger(__name__)

# Default require-list. Mirrored in CTR-0099 / UDR-0043 D2. Kept as a
# tuple constant so test code can introspect without importing settings.
DEFAULT_REQUIRE_LIST: tuple[str, ...] = ("bash_execute", "file_write")

# Time-to-live grace on top of TOOL_APPROVAL_TIMEOUT_SEC during which
# expired records remain readable so a slow POST sees a clean 410 rather
# than a 404 (UDR-0043 D7).
_GC_GRACE_SEC = 60.0

ApprovalSource = Literal["user", "session-cache", "timeout", "abort", "api-auto"]


@dataclass(frozen=True)
class ApprovalResolution:
    """Final decision for a parked approval request."""

    approved: bool
    source: ApprovalSource


@dataclass
class ApprovalRecord:
    """One pending approval request in flight."""

    id: str  # join key shared with the AG-UI CUSTOM event
    thread_id: str
    tool_name: str
    call_id: str  # MAF FunctionCallContent.call_id
    arguments_preview: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    event: asyncio.Event = field(default_factory=asyncio.Event)
    resolution: ApprovalResolution | None = None

    def resolve(self, *, approved: bool, source: ApprovalSource) -> None:
        """Mark the record resolved and unblock the parked waiter.

        Idempotent: subsequent calls leave the original resolution in
        place. Callers race the timeout sweeper.
        """
        if self.resolution is not None:
            return
        self.resolution = ApprovalResolution(approved=approved, source=source)
        self.event.set()


class _ApprovalStore:
    """In-process registry of pending approval records.

    NOT persisted to disk (UDR-0043 D6). Lifetimes are bounded by
    ``TOOL_APPROVAL_TIMEOUT_SEC`` plus a small grace window. The
    session-scoped cache ((thread_id, tool_name) -> approved) is a
    separate dict so it can outlive the approval records themselves
    until the session ends.
    """

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        self._session_cache: dict[tuple[str, str], bool] = {}
        self._lock = asyncio.Lock()

    # ---- record lifecycle ----

    async def register(
        self,
        *,
        thread_id: str,
        tool_name: str,
        call_id: str,
        arguments_preview: dict[str, Any],
    ) -> ApprovalRecord:
        """Create and store a new ApprovalRecord, returning it.

        The caller awaits ``record.event`` to park on the approval
        handshake, with a ``wait_for`` timeout layered on top.
        """
        record_id = uuid.uuid4().hex
        now = time.time()
        record = ApprovalRecord(
            id=record_id,
            thread_id=thread_id,
            tool_name=tool_name,
            call_id=call_id,
            arguments_preview=arguments_preview,
            created_at=now,
            expires_at=now + float(settings.tool_approval_timeout_sec),
        )
        async with self._lock:
            self._records[record_id] = record
        return record

    async def get(self, record_id: str) -> ApprovalRecord | None:
        async with self._lock:
            return self._records.get(record_id)

    async def resolve(
        self,
        record_id: str,
        *,
        approved: bool,
        source: ApprovalSource,
    ) -> tuple[Literal["ok", "missing", "already-resolved"], ApprovalRecord | None]:
        """Apply a resolution to a pending record.

        Returns ("ok", record) on first-time release, ("already-resolved",
        record) when the record exists but has a prior resolution
        (rendered as HTTP 410 by the REST endpoint), or ("missing", None)
        when the record was never registered or has been GC'd.
        """
        async with self._lock:
            record = self._records.get(record_id)
        if record is None:
            return "missing", None
        if record.resolution is not None:
            return "already-resolved", record
        record.resolve(approved=approved, source=source)
        return "ok", record

    async def drop(self, record_id: str) -> None:
        """Remove a record from the store (called after the parked waiter releases)."""
        async with self._lock:
            self._records.pop(record_id, None)

    async def gc_expired(self) -> int:
        """Remove records whose expires_at + grace has passed.

        Returns the number of records dropped. Called periodically by
        the FastAPI lifespan startup task (or on demand from tests).
        """
        cutoff = time.time() - _GC_GRACE_SEC
        async with self._lock:
            stale = [rid for rid, rec in self._records.items() if rec.expires_at < cutoff]
            for rid in stale:
                self._records.pop(rid, None)
        return len(stale)

    # ---- session-scoped "approve for this session" cache ----

    async def cache_decision(self, *, thread_id: str, tool_name: str, approved: bool) -> None:
        """Persist a (thread_id, tool_name) decision in-process.

        Keyed by (thread_id, tool_name) ONLY (UDR-0043 D8). Argument
        hashing is intentionally out of scope.
        """
        async with self._lock:
            self._session_cache[(thread_id, tool_name)] = approved

    async def lookup_session_cache(self, *, thread_id: str, tool_name: str) -> bool | None:
        async with self._lock:
            return self._session_cache.get((thread_id, tool_name))

    async def clear_session(self, thread_id: str) -> None:
        """Drop every cached decision for ``thread_id`` (session delete / abort)."""
        async with self._lock:
            stale = [key for key in self._session_cache if key[0] == thread_id]
            for key in stale:
                self._session_cache.pop(key, None)

    # ---- diagnostics ----

    async def snapshot(self) -> dict[str, Any]:
        """Inspection helper used by tests / debugging."""
        async with self._lock:
            return {
                "pending_count": len(self._records),
                "session_cache_count": len(self._session_cache),
            }


# Module-level singleton. AG-UI endpoint and the REST handler both
# import this name; tests can swap the singleton via monkeypatch.
approval_store = _ApprovalStore()


def resolve_require_set() -> frozenset[str]:
    """Return the set of tool names that should be approval-wrapped.

    Mode dispatch (case-insensitive, normalized by the Settings validator):

    - ``skip``   -> empty frozenset; no tool is wrapped.
    - ``auto``   -> ``TOOL_APPROVAL_REQUIRE_LIST`` parsed, with the
                    default pair (``bash_execute, file_write``) used when
                    the env var is empty or whitespace-only.
    - ``always`` -> a sentinel ``"*"`` entry. Callers (the tool
                    registration helper below) interpret ``"*"`` as
                    "every callable that is not in the read-only
                    safelist".

    The Settings property ``tool_approval_require_set`` carries the
    skip / auto branch; the ``always`` branch is handled here so the
    require_set semantics stay co-located.
    """
    mode = settings.tool_approval_mode
    if mode == "always":
        return frozenset({"*"})
    return settings.tool_approval_require_set


# Tools that MUST NEVER be approval-wrapped even under "always" mode.
# Read-only file operations are documented as safe (CTR-0031 v3,
# UDR-0043 D2). Adding entries here requires a UDR amendment.
_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "file_read",
        "file_glob",
        "file_grep",
    }
)


def wrap_with_approval(fn: Callable[..., Any], require_set: frozenset[str]) -> Callable[..., Any]:
    """Decorate ``fn`` iff ``fn.__name__`` requires approval.

    Returns ``fn`` unchanged when the require-set is empty (skip mode)
    or when ``fn.__name__`` is not listed and the require-set is not the
    ``"*"`` wildcard. The wildcard branch additionally exempts the
    read-only tools enumerated in ``_READ_ONLY_TOOL_NAMES``.
    """
    if not require_set:
        return fn

    name = getattr(fn, "__name__", "")
    if "*" in require_set:
        # "always" mode: wrap everything except read-only helpers.
        if name in _READ_ONLY_TOOL_NAMES:
            return fn
    elif name not in require_set:
        return fn

    from agent_framework import tool

    wrapped = tool(approval_mode="always_require")(fn)
    _logger.info("Tool %r registered with approval_mode=always_require (PRP-0067)", name)
    return wrapped


def truncate_arguments_preview(
    arguments: dict[str, Any] | None,
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Truncate string values inside an arguments dict for the AG-UI event.

    The full arguments still reach the tool on approval; only the
    preview the SPA renders is shortened so an AG-UI CUSTOM event does
    not balloon to e.g. a 1 MiB file_write content blob.
    """
    cap = max_chars if max_chars is not None else settings.tool_approval_arg_max_chars
    preview: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > cap:
            head = value[:cap]
            preview[key] = f"{head}... [truncated {len(value) - cap} chars]"
        else:
            preview[key] = value
    return preview


__all__ = [
    "DEFAULT_REQUIRE_LIST",
    "ApprovalRecord",
    "ApprovalResolution",
    "ApprovalSource",
    "approval_store",
    "resolve_require_set",
    "truncate_arguments_preview",
    "wrap_with_approval",
]
