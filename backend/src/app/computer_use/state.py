"""Run-scoped Computer Use state and the one abort event (PRP-0189, UDR-0171 D5 / D6 / D11).

* ``begin_run`` is called by the AG-UI endpoint (CTR-0009) for every SPA run, next to
  ``set_temporary_run``. It records whether the request came from a loopback peer
  (H7) and which provider lane the selected model is on (the Anthropic image clamp).
  A run that never called it -- Teams, the OpenAI-compatible API, Live delegation,
  workflow nodes, cron -- has no state, and every ``computer_*`` call fails with
  ``origin_not_local``.
* ONE abort event serves all four kill switches (D11): the global hotkey, mouse
  takeover, the Stop button (``CancelledError`` in a tool's wrapper) and
  ``POST /api/computer-use/abort``. The executor checks it at every step boundary and
  on every wait poll. Once a run has been aborted it stays aborted (a latch), and the
  next run starts clean.
* ONE desktop, ONE holder (D6): a tool call takes the holder lock without waiting; a
  second run that tries while another run is on the desktop gets ``busy``.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
import threading
import time
from typing import TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    from app.computer_use.backend import WindowInfo
    from app.computer_use.perception import Observation


@dataclass
class RunState:
    run_id: str
    thread_id: str
    local_origin: bool
    provider: str = ""
    model: str = ""
    cycles: int = 0
    aborted_by: str | None = None
    target: WindowInfo | None = None
    observations: dict[str, Observation] = field(default_factory=dict)
    latest_obs: str | None = None
    obs_seq: int = 0
    last_cursor: tuple[int, int] | None = None
    last_return: float | None = None
    # The glow around the target (PRP-0192, UDR-0174 D4): the window it is shown on, and when.
    glow_hwnd: int | None = None
    glow_at: float = 0.0

    def next_obs_id(self) -> str:
        self.obs_seq += 1
        return f"o{self.obs_seq}"

    def remember(self, obs: Observation, keep: int = 4) -> None:
        self.observations[obs.id] = obs
        self.latest_obs = obs.id
        # Only the newest few are kept: an older one can only be refused as stale.
        for key in list(self.observations)[:-keep]:
            del self.observations[key]

    def model_ms(self) -> int | None:
        """Time since the previous computer_* call returned: upload + inference (R15)."""
        if self.last_return is None:
            return None
        return int((time.monotonic() - self.last_return) * 1000)


_run: contextvars.ContextVar[RunState | None] = contextvars.ContextVar("computer_use_run", default=None)

_abort = threading.Event()
_abort_by: list[str] = [""]
_abort_lock = threading.Lock()
_holder = threading.Lock()


def begin_run(*, thread_id: str, local_origin: bool, model: str = "", provider: str = "") -> RunState:
    """Start a fresh Computer Use state for this AG-UI run (CTR-0009)."""
    clear_abort()
    state = RunState(
        run_id=uuid.uuid4().hex[:12],
        thread_id=thread_id,
        local_origin=local_origin,
        provider=provider,
        model=model,
    )
    _run.set(state)
    return state


def current_run() -> RunState | None:
    return _run.get()


def request_abort(by: str) -> None:
    """Any kill switch (D11). Safe from any thread."""
    with _abort_lock:
        _abort_by[0] = by
        _abort.set()


def clear_abort() -> None:
    with _abort_lock:
        _abort_by[0] = ""
        _abort.clear()


def abort_reason(wait_s: float = 0.0) -> str | None:
    """The pending abort source, waiting up to ``wait_s`` for one (a poll tick)."""
    fired = _abort.wait(wait_s) if wait_s > 0 else _abort.is_set()
    if not fired:
        return None
    with _abort_lock:
        return _abort_by[0] or "api"


def try_hold() -> bool:
    return _holder.acquire(blocking=False)


def release_hold() -> None:
    if _holder.locked():
        _holder.release()


def is_held() -> bool:
    return _holder.locked()


__all__ = [
    "RunState",
    "abort_reason",
    "begin_run",
    "clear_abort",
    "current_run",
    "is_held",
    "release_hold",
    "request_abort",
    "try_hold",
]
