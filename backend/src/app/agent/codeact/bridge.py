"""Off-loop Monty execution (PRP-0193 Section 2.3, UDR-0175 D6 / D7).

``agent_framework_monty.InlineCodeBridge`` drives the pydantic-monty worker pool with
SYNCHRONOUS calls made on the event loop: ``session.feed_start()`` and every
``snapshot.resume()`` block until the worker yields. Measured before PRP-0193: a
100 ms ticker task advanced ZERO times while a 2-second snippet ran, i.e. every SSE
stream of every user in the process stalled for the whole run limit.

This bridge is the same protocol loop over the PUBLIC pool API (UDR-0175 D10), with
every call that can block moved to a worker thread via ``asyncio.to_thread``. The
pydantic-monty documentation states those calls block "the calling thread (with the
GIL released)", so the event loop keeps serving while the worker computes. Host tool
callbacks -- none are registered in Phase 1 (D2) -- would be awaited ON the loop, so
contextvars and loop-bound clients keep working (D6).

A cancelled run (Stop, disconnect) still reaches ``finally``: the session and the pool
are closed, which terminates the worker process, so no computation outlives its run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fixed in code, not a setting (PRP-0193 Q2, operator: 1 for the first release). A
# second execution waits for the first; the time limit bounds that wait.
MAX_CONCURRENT_EXECUTIONS = 1

# The pool kills a worker that exceeds this beyond the sandbox's own time limit
# (UDR-0175 D7): a backstop for a worker that stops answering.
REQUEST_TIMEOUT_MARGIN_SECS = 5.0

# MAF's stdout cap (agent_framework_monty._monty_bridge.MAX_PRINT_OUTPUT_CHARS).
MAX_PRINT_OUTPUT_CHARS = 8192

_SCRIPT_NAME = "codeact.py"

_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _execution_slot() -> asyncio.Semaphore:
    """The process-wide concurrency cap, bound to the running loop.

    Created lazily and re-created when the loop changes: an ``asyncio.Semaphore``
    binds to the first loop that waits on it, and tests run several loops.
    """
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)
        _semaphore_loop = loop
    return _semaphore


class _PrintCollector:
    """Collect sandbox stdout / stderr, capped at ``MAX_PRINT_OUTPUT_CHARS``.

    Called from the worker thread that is blocked in ``feed_start`` / ``resume``;
    only one such call is in flight per execution, so no locking is needed.
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._size = 0
        self.truncated = False

    def __call__(self, stream: str, text: str) -> None:
        if self.truncated:
            return
        remaining = MAX_PRINT_OUTPUT_CHARS - self._size
        value = str(text)
        if len(value) > remaining:
            self._chunks.append(value[:remaining])
            self._size = MAX_PRINT_OUTPUT_CHARS
            self.truncated = True
            return
        self._chunks.append(value)
        self._size += len(value)

    @property
    def output(self) -> str:
        return "".join(self._chunks)


def _json_safe(value: Any) -> Any:
    """Return ``value`` if it serializes as JSON, else its ``repr`` (never raises)."""
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)
    return value


def _error(exc_type: str, message: str) -> dict[str, str]:
    return {"exc_type": exc_type, "message": message}


class OffLoopCodeBridge:
    """Run one snippet in a fresh Monty worker without blocking the event loop."""

    def __init__(self, *, resource_limits: dict[str, Any] | None = None) -> None:
        self._limits = dict(resource_limits) if resource_limits else None

    def _request_timeout(self) -> float | None:
        duration = (self._limits or {}).get("max_duration_secs")
        return float(duration) + REQUEST_TIMEOUT_MARGIN_SECS if duration else None

    async def run(self, code: str) -> dict[str, Any]:
        """Execute ``code``; return ``{"output", "stdout", "truncated"}``.

        Raises whatever pydantic-monty raises for a failed snippet (syntax, runtime,
        time / memory limit, crashed worker); the caller turns it into an error result.
        """
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Code must be a non-empty string.")

        import pydantic_monty as monty

        printer = _PrintCollector()
        slot = _execution_slot()
        await slot.acquire()
        pool = monty.Monty(request_timeout=self._request_timeout())
        session: Any = None
        handed_off = False
        try:
            checkout_kwargs: dict[str, Any] = {"script_name": _SCRIPT_NAME}
            if self._limits:
                checkout_kwargs["limits"] = self._limits
            await asyncio.to_thread(pool.__enter__)
            session = pool.checkout(**checkout_kwargs)
            await asyncio.to_thread(session.__enter__)
            progress = await asyncio.to_thread(session.feed_start, code, print_callback=printer)
            while True:
                if isinstance(progress, monty.MontyComplete):
                    return {
                        "output": _json_safe(progress.output),
                        "stdout": printer.output,
                        "truncated": printer.truncated,
                    }
                if isinstance(progress, monty.FunctionSnapshot):
                    progress = await asyncio.to_thread(progress.resume, self._answer_function(progress))
                    continue
                if isinstance(progress, monty.NameLookupSnapshot):
                    # No external names exist (UDR-0175 D2): an unknown name is a NameError.
                    progress = await asyncio.to_thread(progress.resume)
                    continue
                if isinstance(progress, monty.FutureSnapshot):
                    # Only a registered host tool creates a future; Phase 1 registers none.
                    results = {
                        int(cid): _error("RuntimeError", "No sandbox tools are registered.")
                        for cid in progress.pending_call_ids
                    }
                    progress = await asyncio.to_thread(progress.resume, results)
                    continue
                raise RuntimeError(f"Unsupported Monty progress type: {type(progress).__name__}")
        except asyncio.CancelledError:
            # The sync pool API has no interrupt: the worker thread stays blocked until
            # the snippet ends or hits its time limit. The cancelled run returns NOW;
            # a background task closes the session and the pool once the worker is
            # free, and only then releases the slot, so the cap stays true (D6).
            handed_off = True
            _spawn_cleanup(session, pool, slot)
            raise
        finally:
            if not handed_off:
                await _cleanup(session, pool, slot)

    @staticmethod
    def _answer_function(snapshot: Any) -> dict[str, str]:
        if snapshot.is_os_function:
            return _error("PermissionError", "OS, filesystem and network calls are not available.")
        return _error("NameError", f"Function {str(snapshot.function_name)!r} is not available.")


_cleanup_tasks: set[asyncio.Task[None]] = set()


async def _cleanup(session: Any, pool: Any, slot: asyncio.Semaphore) -> None:
    """Close the session and the pool off the loop, then free the execution slot."""
    try:
        if session is not None:
            await asyncio.to_thread(_close, session)
        await asyncio.to_thread(_close, pool)
    finally:
        slot.release()


def _spawn_cleanup(session: Any, pool: Any, slot: asyncio.Semaphore) -> None:
    task = asyncio.get_running_loop().create_task(_cleanup(session, pool, slot))
    _cleanup_tasks.add(task)  # keep a strong reference until it finishes
    task.add_done_callback(_cleanup_tasks.discard)


def _close(ctx: Any) -> None:
    try:
        ctx.__exit__(None, None, None)
    except Exception:
        logger.debug("CodeAct: closing %s failed", type(ctx).__name__, exc_info=True)


__all__ = [
    "MAX_CONCURRENT_EXECUTIONS",
    "MAX_PRINT_OUTPUT_CHARS",
    "REQUEST_TIMEOUT_MARGIN_SECS",
    "OffLoopCodeBridge",
]
