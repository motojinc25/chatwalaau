"""In-process background task runner (CTR-0108, PRP-0077, UDR-0053).

A thin, fire-and-forget asyncio runner that executes registered background
tasks ON THE MAIN FastAPI event loop, AFTER a chat turn, with total error
isolation. It exists for short-lived, post-conversation, LLM-driven side work
that must NOT block or alter the chat path.

Deliberately distinct from the two existing "background" mechanisms:

- CTR-0045 Background Responses -- MAF resumable agent responses (continuation
  token). This runner does not touch the agent run lifecycle.
- CTR-0073 Batch Processing -- a separate FastMCP subprocess job queue with
  persistence, progress, cancellation, and a dashboard. This runner is
  ephemeral, in-process, and invisible: no persistence, no dashboard, no
  cancellation-by-id.

Adding a task = register a runner in ``BACKGROUND_TASK_REGISTRY`` (via
``register_task``) and ``dispatch`` it from a trigger site. The first consumer
is Auto Session Title (CTR-0109); the deferred User Preference Memory
background extraction (UDR-0051 D5) is the anticipated next one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any

logger = logging.getLogger(__name__)

# A registered task body. Receives an opaque ctx dict supplied by the trigger
# site (e.g. {"thread_id": ...}); it reads its own inputs, does its own I/O, and
# writes its own outputs. Any exception it raises is caught and logged here.
Runner = Callable[[dict[str, Any]], Awaitable[None]]

# task-type name -> async runner. Consumers register at import time.
BACKGROUND_TASK_REGISTRY: dict[str, Runner] = {}

# Strong references to scheduled tasks so they are not garbage-collected before
# they finish, plus a dedup set keyed by (task_type, dedup_key) so a re-dispatch
# while a task is in flight is a no-op.
_inflight_tasks: set[asyncio.Task[None]] = set()
_inflight_keys: set[tuple[str, str]] = set()


def register_task(task_type: str, runner: Runner) -> None:
    """Register a runner under ``task_type`` (idempotent; last write wins)."""
    BACKGROUND_TASK_REGISTRY[task_type] = runner


async def _run_isolated(task_type: str, dedup_key: str, ctx: dict[str, Any]) -> None:
    """Run a registered task with full error isolation.

    Any exception (runner bug, LLM error, write failure) is caught and logged at
    WARNING and swallowed, so a failing background task can never reach the chat
    path. Cancellation (shutdown) propagates normally.
    """
    runner = BACKGROUND_TASK_REGISTRY.get(task_type)
    try:
        if runner is None:  # pragma: no cover -- guarded again in dispatch()
            logger.error("background task %r is not registered; skipping", task_type)
            return
        await runner(ctx)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("background task %r failed (dedup_key=%s)", task_type, dedup_key, exc_info=True)
    finally:
        _inflight_keys.discard((task_type, dedup_key))


def dispatch(task_type: str, *, dedup_key: str, ctx: dict[str, Any]) -> None:
    """Schedule a background task; return immediately (never awaited).

    No-op if ``task_type`` is unregistered, if an identical
    ``(task_type, dedup_key)`` task is already in flight (dedup), or if there is
    no running event loop. Errors inside the task are isolated by
    ``_run_isolated`` and never propagate to the caller / chat path.
    """
    if task_type not in BACKGROUND_TASK_REGISTRY:
        logger.error("dispatch for unregistered background task %r; skipping", task_type)
        return
    key = (task_type, dedup_key)
    if key in _inflight_keys:
        logger.debug("background task %r already in flight for %s; skipping duplicate", task_type, dedup_key)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("dispatch called with no running event loop; skipping %r", task_type)
        return
    _inflight_keys.add(key)
    task = loop.create_task(_run_isolated(task_type, dedup_key, ctx))
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)


async def shutdown(timeout: float = 5.0) -> None:
    """Best-effort drain of in-flight tasks at app shutdown (CTR-0108).

    Gives running tasks a brief grace period, then cancels any stragglers.
    A cancelled task leaves prior state intact (e.g. the truncation title
    persists); cancellation is not an error.
    """
    if not _inflight_tasks:
        return
    pending = list(_inflight_tasks)
    _done, still_running = await asyncio.wait(pending, timeout=timeout)
    for task in still_running:
        task.cancel()
    if still_running:
        await asyncio.gather(*still_running, return_exceptions=True)


# Register built-in tasks (CTR-0109). Imported at the BOTTOM so the registry and
# register_task() defined above already exist when the consumer module imports
# them (avoids a circular-import failure).
from app.background import session_title  # noqa: E402  (import-time task registration side effect)

__all__ = [
    "BACKGROUND_TASK_REGISTRY",
    "Runner",
    "dispatch",
    "register_task",
    "shutdown",
]
