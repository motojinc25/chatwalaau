"""The glow around the controlled window -- its lifetime (PRP-0192 Part A, UDR-0174 D4).

The provider draws the glow (a capture-excluded band, CTR-0236 ``ui.overlay``); the HOST decides
when it is up:

* after every computer tool that leaves a target (``refresh``): shown on the target, moved when
  the target changes, renewed at most every RENEW_S (the provider hides it after TTL_S without
  a renewal -- a safety net only);
* hidden after an abort and when the run has no target;
* hidden when the chat run ends (``end_run``, from the AG-UI stream's ``finally`` and the end
  of a Live delegation);
* never shown when App Settings > Computer Use > "Glow around the controlled window" is off.

A glow failure never fails a tool: the glow is a signal, not part of the work.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from app.computer_use.backend import has
from app.core.config import settings

if TYPE_CHECKING:
    from app.computer_use.backend import DesktopBackend
    from app.computer_use.state import RunState

logger = logging.getLogger(__name__)

#: The provider hides the glow after this long without a renewal (a safety net).
TTL_S = 300.0
#: A glow already shown on the same window is renewed at most this often.
RENEW_S = 60.0


def hide(backend: DesktopBackend, run: RunState) -> None:
    if run.glow_hwnd is None:
        return
    run.glow_hwnd = None
    try:
        backend.overlay_hide()
    except Exception:
        logger.debug("hiding the Computer Use glow failed", exc_info=True)


def refresh(backend: DesktopBackend, run: RunState) -> None:
    """After a computer tool: show, move, renew or hide the glow for this run."""
    target = run.target
    wanted = bool(settings.computer_use_overlay) and has(backend, "ui.overlay")
    if not wanted or target is None or run.aborted_by:
        hide(backend, run)
        return
    now = time.monotonic()
    if run.glow_hwnd == target.hwnd and now - run.glow_at < RENEW_S:
        return
    try:
        shown = backend.overlay_show(target.hwnd, TTL_S)
    except Exception:
        logger.debug("showing the Computer Use glow failed", exc_info=True)
        return
    run.glow_hwnd = target.hwnd if shown else None
    run.glow_at = now


def end_run() -> None:
    """The chat run is over: hide its glow. Never awaits, never raises, never starts a provider."""
    from app.computer_use import state, worker

    run = state.current_run()
    if run is None or run.glow_hwnd is None:
        return
    worker.submit(lambda backend: hide(backend, run))


__all__ = ["RENEW_S", "TTL_S", "end_run", "hide", "refresh"]
