"""SSE keep-alive for a silent workflow run on the AG-UI stream (PRP-0195, UDR-0177, CTR-0009).

A workflow Prompt node can think for 20 minutes without sending one byte: the chat lane
reports node progress and finished output only (UDR-0176 D4). The production front ends
(Azure App Service, Azure Container Apps) close an HTTP connection that carried no data
for about four minutes, and the SPA then shows a network error. ``with_keepalive`` sends
an SSE comment line whenever the wrapped stream has been silent for ``interval``
seconds. Conforming SSE parsers -- the SPA's included -- skip comment lines, so the
events and the SPA's "committed" point (UDR-0088 D3) are unchanged; a stream that emits
more often than the interval is byte-identical.

The shape is fixed by UDR-0177 D3, and each part closes a trap:

* ONE pending step, awaited with ``asyncio.wait(timeout=...)``. ``asyncio.wait_for``
  would cancel the step on timeout and so close the stream it is meant to keep alive.
* Every step and the final ``aclose()`` run in ONE shared ``contextvars.Context``. A
  fresh task per step would otherwise see a fresh copy of the caller's context, and a
  context variable set inside the stream would be lost at its next step.
* A cancellation of the wrapper (Starlette cancels the response on a client disconnect)
  cancels the pending step and closes the stream at once, so its cleanup -- the usage
  ``interrupted`` drain, the title clear -- runs exactly as it did before.

This is a transport keep-alive on an open response, not liveness monitoring: the SPA
never reads or times it (UDR-0177 D4, UDR-0088 D5).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator

# The Live event stream's value (CTR-0224, app/live/router.py). A constant, not
# configuration (UDR-0177 D2): it only has to stay well under the ~4-minute idle limits.
KEEPALIVE_INTERVAL_SEC = 15.0
KEEPALIVE_COMMENT = ": keep-alive\n\n"


async def with_keepalive(
    source: AsyncIterable[Any],
    interval: float = KEEPALIVE_INTERVAL_SEC,
) -> AsyncIterator[Any]:
    """Yield ``source``'s chunks, and a keep-alive comment after each silent ``interval``."""
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    iterator = source.__aiter__()
    pending: asyncio.Task[Any] | None = None
    try:
        while True:
            if pending is None:
                pending = loop.create_task(iterator.__anext__(), context=ctx)
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield KEEPALIVE_COMMENT
                continue
            step, pending = pending, None
            try:
                chunk = step.result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        # The cleanup runs in its OWN task, so it completes even when this frame is being
        # cancelled: Starlette cancels through an anyio cancel scope, which re-delivers the
        # cancellation at every later await here.
        cleanup: asyncio.Task[Any] | None = None
        if pending is not None and not pending.done():
            # The stream is inside a step: it sees CancelledError there and runs its own
            # cleanup in that task and context.
            pending.cancel()
            cleanup = pending
        else:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                cleanup = loop.create_task(aclose(), context=ctx)
        if cleanup is not None:
            cleanup.add_done_callback(_retrieve)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({cleanup})


def _retrieve(task: asyncio.Task[Any]) -> None:
    """Mark a cleanup task's outcome as seen (no 'exception was never retrieved' noise)."""
    if not task.cancelled():
        task.exception()


__all__ = ["KEEPALIVE_COMMENT", "KEEPALIVE_INTERVAL_SEC", "with_keepalive"]
