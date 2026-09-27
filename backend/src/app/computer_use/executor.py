"""Desktop Input Executor (CTR-0232, PRP-0189 Sections 2.5 / 2.6 / 2.10).

Runs ONE validated :class:`~app.computer_use.dsl.ActionBatch` against the locked
target window. Before EVERY input step it checks, in this order:

1. the abort event (hotkey, Stop, API) -- ``aborted``;
2. human takeover: the cursor moved more than ``TAKEOVER_PX`` away from where the
   executor last put it -- ``aborted`` by ``user_mouse``;
3. the target lock: the foreground window belongs to the target's process (a
   same-process dialog is part of the target) -- otherwise ``focus_lost``.

Waits and ``if`` / ``repeat`` conditions are evaluated LOCALLY: the model is called
again only when the batch ends (R5). There is no fixed sleep: waits poll and end as
soon as their condition holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
import time
from typing import TYPE_CHECKING, Any

from app.computer_use import dsl
from app.computer_use.backend import has
from app.computer_use.perception import Aborted, differs, wait_for_change, wait_until_stable
from app.computer_use.policy import SecretUnavailable, secret_value

if TYPE_CHECKING:
    from app.computer_use.backend import DesktopBackend, Rect, WindowInfo
    from app.computer_use.perception import Observation
    from app.computer_use.state import RunState

#: Cursor displacement (physical px) that means the user took the mouse (D11).
TAKEOVER_PX = 40


class StepFailed(Exception):
    def __init__(self, reason: str, status: str = "failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class ExecConfig:
    threshold: float
    stable_s: float
    timeout_s: float


@dataclass
class ExecResult:
    status: str = "ok"
    executed: int = 0
    failed_step: list[Any] | None = None
    reason: str = ""
    aborted_by: str | None = None
    typed: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "executed": self.executed}
        if self.failed_step is not None:
            out["failed_step"] = self.failed_step
        if self.reason:
            out["reason"] = self.reason
        if self.aborted_by:
            out["by"] = self.aborted_by
        if self.typed:
            out["typed"] = self.typed
        return out


def abort_check(run: RunState, wait_s: float) -> str | None:
    from app.computer_use.state import abort_reason

    if run.aborted_by:
        return run.aborted_by
    return abort_reason(wait_s)


class Executor:
    def __init__(self, backend: DesktopBackend, run: RunState, obs: Observation, cfg: ExecConfig) -> None:
        self.backend = backend
        self.run = run
        self.obs = obs
        self.cfg = cfg
        self.result = ExecResult()
        self.deadline = time.monotonic() + dsl.BATCH_WALL_CLOCK_S
        assert run.target is not None
        self.target: WindowInfo = run.target

    # -- entry -------------------------------------------------------------------------

    def execute(self, batch: dsl.ActionBatch) -> ExecResult:
        try:
            self._steps(batch.actions, [])
        except Aborted as exc:
            self.result.status = "aborted"
            self.result.aborted_by = exc.by
            self.run.aborted_by = exc.by
        except StepFailed as exc:
            self.result.status = exc.status
            self.result.reason = exc.reason
        return self.result

    # -- guards -----------------------------------------------------------------------------

    def _abort_check(self, wait_s: float) -> str | None:
        return abort_check(self.run, wait_s)

    def _guard_input(self) -> None:
        by = self._abort_check(0)
        if by:
            raise Aborted(by)
        if time.monotonic() > self.deadline:
            raise StepFailed(f"the batch ran longer than {int(dsl.BATCH_WALL_CLOCK_S)} s", "failed")
        if self.run.last_cursor is not None:
            cx, cy = self.backend.cursor_pos()
            lx, ly = self.run.last_cursor
            if math.hypot(cx - lx, cy - ly) > TAKEOVER_PX:
                raise Aborted("user_mouse")
        fg = self.backend.foreground()
        if fg is None or fg.pid != self.target.pid:
            title = fg.title if fg is not None else "nothing"
            raise StepFailed(f"the foreground window is {title!r}, not the target", "focus_lost")

    def _window_rect(self) -> Rect:
        fresh = self.backend.window(self.target.hwnd)
        return (fresh or self.target).rect

    # -- targets ----------------------------------------------------------------------------

    def _resolve(self, point: dsl.Point) -> tuple[int, int]:
        if point.element is not None:
            if not has(self.backend, "ui.elements"):
                raise StepFailed("element targets are not supported by this desktop backend; use x / y", "unsupported")
            el = self.obs.element(point.element)
            if el is None:
                raise StepFailed(f"element {point.element!r} is not in observation {self.obs.id}")
            rect = self.backend.element_rect(el) or el.rect
            x, y = rect.center()
        else:
            assert point.x is not None and point.y is not None
            if point.x >= self.obs.mapping.image_w or point.y >= self.obs.mapping.image_h:
                raise StepFailed(
                    f"({point.x}, {point.y}) is outside the {self.obs.mapping.image_w}x{self.obs.mapping.image_h} image"
                )
            x, y = self.obs.mapping.to_screen(point.x, point.y)
        # The point must be on the target (or on a same-process window in front of it).
        fg = self.backend.foreground()
        on_target = self._window_rect().contains(x, y)
        on_dialog = fg is not None and fg.pid == self.target.pid and fg.rect.contains(x, y)
        if not (on_target or on_dialog):
            raise StepFailed("the target point is outside the target window")
        return x, y

    def _pointer(self, x: int, y: int) -> None:
        self.run.last_cursor = (x, y)

    # -- conditions --------------------------------------------------------------------------

    def _condition(self, cond: dsl.Condition, base: bytes | None) -> bool:
        if cond.element is not None:
            if not has(self.backend, "ui.elements"):
                raise StepFailed("element conditions are not supported by this desktop backend", "unsupported")
            fg = self.backend.foreground()
            hwnds = [self.target.hwnd]
            if fg is not None and fg.pid == self.target.pid and fg.hwnd != self.target.hwnd:
                hwnds.insert(0, fg.hwnd)
            found = any(self.backend.find_element(h, cond.element.name, cond.element.control) for h in hwnds)
            return found == cond.present
        if cond.window_title is not None:
            fg = self.backend.foreground()
            title = fg.title if fg is not None else ""
            try:
                return re.search(cond.window_title, title, re.IGNORECASE) is not None
            except re.error:
                return cond.window_title.lower() in title.lower()
        if cond.dialog is not None:
            if not has(self.backend, "windows.dialog"):
                raise StepFailed("dialog conditions are not supported by this desktop backend", "unsupported")
            return self.backend.has_modal_dialog(self.target) == cond.dialog
        if cond.changed is not None:
            if base is None:
                return not cond.changed
            now = self.backend.capture(self._window_rect()).gray
            return differs(base, now) == cond.changed
        return False

    # -- steps ---------------------------------------------------------------------------------

    def _steps(self, actions: list[Any], path: list[Any]) -> None:
        for index, step in enumerate(actions):
            here = [*path, index]
            try:
                self._step(step, here)
            except StepFailed:
                if self.result.failed_step is None:
                    self.result.failed_step = here
                raise

    def _step(self, step: Any, path: list[Any]) -> None:
        base: bytes | None = None
        if isinstance(step, dsl.Click | dsl.DoubleClick | dsl.RightClick | dsl.Move):
            self._guard_input()
            x, y = self._resolve(step.point())
            if isinstance(step, dsl.Move):
                self.backend.move(x, y)
            elif isinstance(step, dsl.DoubleClick):
                self.backend.click(x, y, "left", 2)
            elif isinstance(step, dsl.RightClick):
                self.backend.click(x, y, "right", 1)
            else:
                self.backend.click(x, y, step.button, 1)
            self._pointer(x, y)
        elif isinstance(step, dsl.Drag):
            self._guard_input()
            x1, y1 = self._resolve(step.from_)
            x2, y2 = self._resolve(step.to)
            self.backend.drag(x1, y1, x2, y2)
            self._pointer(x2, y2)
        elif isinstance(step, dsl.TypeText):
            self._guard_input()
            if step.secret is not None:
                try:
                    text = secret_value(step.secret)
                except SecretUnavailable as exc:
                    raise StepFailed(str(exc), "secret_unavailable") from None
                self.result.typed.append(f"secret:{step.secret}")
            else:
                text = step.text or ""
                self.result.typed.append(f"{len(text)} chars")
            # OPTIONAL input.clipboard is preferred (IME-safe, one event); REQUIRED
            # input.text (Unicode key events) is the fallback every backend has.
            if has(self.backend, "input.clipboard"):
                self.backend.paste_text(text)
            else:
                self.backend.type_text(text)
        elif isinstance(step, dsl.Keypress):
            self._guard_input()
            self.backend.keys(step.chord())
        elif isinstance(step, dsl.Scroll):
            self._guard_input()
            point = step.point()
            x, y = self._resolve(point) if point is not None else self._window_rect().center()
            self.backend.scroll(x, y, step.dy, step.dx)
            self._pointer(x, y)
        elif isinstance(step, dsl.WaitForChange):
            rect = self._window_rect()
            base = self.backend.capture(rect).gray
            timeout = (step.timeout_ms or self.cfg.timeout_s * 1000) / 1000
            changed, _ = wait_for_change(
                self.backend,
                rect,
                base,
                timeout_s=timeout,
                threshold=self.cfg.threshold,
                abort_check=self._abort_check,
            )
            if not changed:
                raise StepFailed(f"the screen did not change within {timeout:.1f} s", "failed")
        elif isinstance(step, dsl.WaitUntilStable):
            settled, _, _ = wait_until_stable(
                self.backend,
                self._window_rect(),
                stable_s=(step.stable_ms / 1000) if step.stable_ms else self.cfg.stable_s,
                timeout_s=(step.timeout_ms / 1000) if step.timeout_ms else self.cfg.timeout_s,
                threshold=self.cfg.threshold,
                abort_check=self._abort_check,
            )
            if not settled:
                raise StepFailed("the screen did not settle before the timeout", "failed")
        elif isinstance(step, dsl.WaitFor):
            base = self.backend.capture(self._window_rect()).gray if step.cond.changed is not None else None
            deadline = time.monotonic() + step.timeout_ms / 1000
            while not self._condition(step.cond, base):
                if time.monotonic() >= deadline:
                    raise StepFailed(f"condition not met within {step.timeout_ms} ms")
                by = self._abort_check(0.12)
                if by:
                    raise Aborted(by)
        elif isinstance(step, dsl.If):
            branch = "then" if self._condition(step.cond, None) else "else"
            self.result.executed += 1
            self._steps(step.then if branch == "then" else step.else_, [*path, branch])
            return
        elif isinstance(step, dsl.Repeat):
            self.result.executed += 1
            for _ in range(step.times):
                self._steps(step.body, [*path, "body"])
                if step.until is not None and self._condition(step.until, None):
                    break
            return
        self.result.executed += 1


def evaluate_expectations(
    backend: DesktopBackend, run: RunState, obs: Observation, expect: list[dsl.Condition], base: bytes
) -> list[bool]:
    """``expect`` postconditions, checked locally after the ``after`` wait (D7)."""
    ex = Executor(backend, run, obs, ExecConfig(threshold=0.0, stable_s=0.0, timeout_s=0.0))
    return [ex._condition(cond, base) for cond in expect]


__all__ = ["TAKEOVER_PX", "ExecConfig", "ExecResult", "Executor", "StepFailed", "evaluate_expectations"]
