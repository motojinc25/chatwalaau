"""Child-process runner for pipeline jobs (CTR-0073 v5, PRP-0211, UDR-0193 D1).

Pipeline jobs run as asyncio tasks in the API process (UDR-0074 D1); blocking work
goes to a thread, which nothing can stop from outside. A runner that must be stoppable
-- by ``cancel``, by its job type's timeout, or by a newer job superseding it -- runs
its work through ``run_in_process`` instead: a module-level function in a fresh
``spawn`` child process, whose result (or exception, with its type) comes back over a
pipe. When the cancel event is set or the time runs out, the child is ENDED
(terminate, then kill after a grace period), so the work really stops.

The child reads no App Settings of its own that could differ from the parent's: the
caller passes a ``settings`` snapshot (``settings_snapshot()``) that is applied in the
child before the function runs, so run-time App Settings changes and test overrides
reach it.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import multiprocessing
import pickle
import time
from typing import Any

logger = logging.getLogger(__name__)

# Seconds a terminated child gets before it is killed.
END_GRACE_SECONDS = 2.0


class ProcessCancelled(Exception):
    """The cancel event was set; the child process was ended."""


class ProcessTimeout(TimeoutError):
    """The time limit passed; the child process was ended."""


class ProcessFailed(RuntimeError):
    """The child process exited without an answer (a crash, or killed from outside)."""


def settings_snapshot(prefixes: tuple[str, ...] = ("ontology_",)) -> dict[str, Any]:
    """The current values of every App Settings field whose name starts with ``prefixes``."""
    from app.core.config import settings

    return {name: value for name, value in settings.model_dump().items() if name.startswith(prefixes)}


def apply_settings(snapshot: dict[str, Any] | None) -> None:
    """Set the snapshot's values on this process's settings object."""
    if not snapshot:
        return
    from app.core.config import settings

    for name, value in snapshot.items():
        if getattr(settings, name, value) != value:
            setattr(settings, name, value)


def resolve(target: str) -> Any:
    """The function named ``"package.module:function"``."""
    module, _, name = target.partition(":")
    if not module or not name:
        raise ValueError(f"Not a 'module:function' target: {target!r}")
    return getattr(importlib.import_module(module), name)


def encode_error(exc: BaseException) -> tuple[bytes | None, str]:
    """An exception for the pipe: pickled when possible, always with a text."""
    text = f"{type(exc).__name__}: {exc}"
    try:
        return pickle.dumps(exc), text
    except Exception:
        return None, text


def decode_error(payload: bytes | None, text: str) -> BaseException:
    """The exception ``encode_error`` sent (a RuntimeError with its text when it could not travel)."""
    if payload is not None:
        try:
            exc = pickle.loads(payload)
            if isinstance(exc, BaseException):
                return exc
        except Exception:
            pass
    return RuntimeError(text)


def _child_main(target: str, args: tuple[Any, ...], kwargs: dict[str, Any], conn: Any, snapshot: Any) -> None:
    """The child's entry point: run the target once and send ("ok", result) or ("err", ...)."""
    try:
        apply_settings(snapshot)
        result = resolve(target)(*args, **kwargs)
        conn.send(("ok", result))
    except BaseException as exc:  # everything goes back to the parent
        payload, text = encode_error(exc)
        try:
            conn.send(("err", payload, text))
        except Exception:
            logger.exception("pipeline child could not report its error")
    finally:
        conn.close()


def end_process(process: Any, grace: float = END_GRACE_SECONDS) -> None:
    """Terminate ``process``; kill it when it is still alive after ``grace`` seconds."""
    if not process.is_alive():
        process.join(0)
        return
    process.terminate()
    process.join(grace)
    if process.is_alive():
        process.kill()
        process.join(grace)


def _receive(conn: Any, wait: float) -> Any:
    """One message within ``wait`` seconds, else None (runs in a thread)."""
    if conn.poll(wait):
        return conn.recv()
    return None


async def run_in_process(
    target: str,
    *args: Any,
    cancel_event: asyncio.Event | None = None,
    timeout: float | None = None,
    settings: dict[str, Any] | None = None,
    poll_seconds: float = 0.1,
    **kwargs: Any,
) -> Any:
    """Run ``target`` (``"module:function"``) with ``args`` in a child process and return its result.

    Raises the child's exception (type preserved when it can be pickled),
    ``ProcessCancelled`` when ``cancel_event`` is set, ``ProcessTimeout`` when
    ``timeout`` seconds pass, ``ProcessFailed`` when the child exits without an answer.
    In the three last cases the child has been ended.
    """
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_main, args=(target, args, kwargs, child, settings), daemon=True, name=f"pipeline:{target}"
    )
    process.start()
    child.close()
    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
    try:
        while True:
            try:
                message = await asyncio.to_thread(_receive, parent, poll_seconds)
            except EOFError:
                message = None
                if not process.is_alive():
                    raise ProcessFailed(f"The worker process exited (code {process.exitcode}).") from None
            if message is not None:
                if message[0] == "ok":
                    return message[1]
                raise decode_error(message[1], message[2])
            if cancel_event is not None and cancel_event.is_set():
                raise ProcessCancelled("Cancelled")
            if deadline is not None and time.monotonic() > deadline:
                raise ProcessTimeout(f"Timed out after {timeout:g} s")
            if not process.is_alive() and not parent.poll(0):
                raise ProcessFailed(f"The worker process exited (code {process.exitcode}).")
    finally:
        if process.is_alive():
            await asyncio.to_thread(end_process, process)
        else:
            process.join(0)
        parent.close()


__all__ = [
    "END_GRACE_SECONDS",
    "ProcessCancelled",
    "ProcessFailed",
    "ProcessTimeout",
    "apply_settings",
    "decode_error",
    "encode_error",
    "end_process",
    "resolve",
    "run_in_process",
    "settings_snapshot",
]
