"""The one desktop worker thread and the kill-switch hotkey (PRP-0189, UDR-0171 D6 / D11).

* ALL desktop calls of the engine run on ONE worker thread, in order: there is one
  desktop, so two runs must never interleave input.
* The desktop itself is reached ONLY through the stdio MCP desktop provider (PRP-0190
  amendment A1, UDR-0172 D4): the backend is
  :class:`app.computer_use.mcp_backend.McpDesktopBackend`, which starts the native
  ``chatwalaau-computer-use`` executable (PRP-0191, or ``COMPUTER_USE_PROVIDER_COMMAND``) on
  first use. The provider owns the per-monitor-v2 DPI, COM-initialised desktop thread; this
  process never touches UI Automation, the screen or SendInput.
* The global hotkey ``Ctrl+Alt+End`` (fixed, Q6) is registered on a SEPARATE thread
  with its own message loop, so it works while a job is running and whatever window
  is in front. It stays in this process (UDR-0172 D7).

The backend is created lazily on the worker thread. Tests replace it with
:func:`set_backend_factory` (a fake desktop, or the provider server over the in-memory
MCP transport).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import logging
import sys
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.computer_use.backend import DesktopBackend

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_backend: DesktopBackend | None = None
_factory: Callable[[], DesktopBackend] | None = None
_lock = threading.Lock()

_hotkey_thread: threading.Thread | None = None
_hotkey_thread_id: int | None = None

HOTKEY_LABEL = "Ctrl+Alt+End"
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_NOREPEAT = 0x4000
_VK_END = 0x23
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


def _default_backend() -> DesktopBackend:
    from app.computer_use.mcp_backend import McpDesktopBackend, provider_command
    from app.core.config import settings

    return McpDesktopBackend(command=provider_command(settings.computer_use_provider_command))


def provider_status() -> dict[str, Any]:
    """The desktop provider's state for CTR-0233 -- never starts it."""
    status = getattr(_backend, "status", None) if _factory is None else None
    return status() if callable(status) else {"state": "stopped"}


def set_backend_factory(factory: Callable[[], DesktopBackend] | None) -> None:
    """Tests: run the engine against a fake desktop (resets the cached backend)."""
    global _factory, _backend
    with _lock:
        _factory = factory
        _backend = None


def _get_backend() -> DesktopBackend:
    global _backend
    if _backend is None:
        _backend = _factory() if _factory is not None else _default_backend()
    return _backend


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="computer-use")
        return _executor


async def run[T](fn: Callable[..., T], *args: Any) -> T:
    """Run ``fn(backend, *args)`` on the desktop worker and await it."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool(), lambda: fn(_get_backend(), *args))


def submit(fn: Callable[..., Any]) -> None:
    """Fire-and-forget ``fn(backend)`` on the desktop worker, only when a backend exists.

    For cleanup from places that must not await (a stream's ``finally``): it never starts the
    provider and never raises.
    """
    if _backend is None:
        return

    def job() -> None:
        try:
            fn(_get_backend())
        except Exception:
            logger.debug("computer use cleanup failed", exc_info=True)

    with contextlib.suppress(RuntimeError):  # the pool is shutting down
        _pool().submit(job)


# ---- hotkey -----------------------------------------------------------------------------------


def _hotkey_loop(ready: threading.Event) -> None:
    import ctypes
    from ctypes import wintypes

    from app.computer_use.state import request_abort

    global _hotkey_thread_id
    user32 = ctypes.windll.user32
    _hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    ok = user32.RegisterHotKey(None, 1, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, _VK_END)
    ready.set()
    if not ok:
        logger.warning("Computer Use: %s could not be registered (in use by another app)", HOTKEY_LABEL)
        return
    logger.info("Computer Use kill switch registered: %s", HOTKEY_LABEL)
    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY:
                logger.info("Computer Use aborted by %s", HOTKEY_LABEL)
                request_abort("user_hotkey")
    finally:
        user32.UnregisterHotKey(None, 1)


def start_hotkey() -> None:
    global _hotkey_thread
    if sys.platform != "win32" or _hotkey_thread is not None:
        return
    ready = threading.Event()
    _hotkey_thread = threading.Thread(target=_hotkey_loop, args=(ready,), name="computer-use-hotkey", daemon=True)
    _hotkey_thread.start()
    ready.wait(2.0)


def shutdown() -> None:
    """Stop the hotkey loop, the provider process (``mcp``) and the worker (FastAPI lifespan)."""
    global _executor, _hotkey_thread, _backend
    if _hotkey_thread is not None and _hotkey_thread_id is not None and sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.PostThreadMessageW(_hotkey_thread_id, _WM_QUIT, 0, 0)
    _hotkey_thread = None
    close = getattr(_backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.warning("Computer Use backend did not close cleanly", exc_info=True)
    with _lock:
        _backend = None
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None


__all__ = [
    "HOTKEY_LABEL",
    "provider_status",
    "run",
    "set_backend_factory",
    "shutdown",
    "start_hotkey",
]
