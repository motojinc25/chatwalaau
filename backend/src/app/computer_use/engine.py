"""The synchronous operations behind the ``computer_*`` tools (CTR-0229 / 0231 / 0232).

Each ``op_*`` runs ON the desktop worker thread and returns plain data: a result
dict for the model's text part, optionally the PNG to attach, and phase timings for
the trace. The async tools in :mod:`app.computer_use.tools` do the gating, the
abort wiring, the trace and the MAF ``Content`` assembly.

One decision cycle is ONE ``op_perform`` (UDR-0171 D7): validate-stale, execute,
the ``after`` wait, the ``expect`` checks and the capture of the resulting screen.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import TYPE_CHECKING, Any

from app.computer_use.backend import OPTIONAL_FEATURES, has
from app.computer_use.executor import ExecConfig, Executor, evaluate_expectations
from app.computer_use.perception import (
    Aborted,
    build_observation,
    changed_fraction,
    dhash,
    differs,
    mask_blocks,
    wait_for_change,
    wait_until_stable,
)
from app.computer_use.policy import denial_reason
from app.core.config import settings

if TYPE_CHECKING:
    from app.computer_use import dsl
    from app.computer_use.backend import DesktopBackend, WindowInfo
    from app.computer_use.perception import Observation
    from app.computer_use.state import RunState


class OpError(Exception):
    """A tool-level refusal with a result status (``denied_window``, ``no_target``...)."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class Config:
    """The ``computer_use`` settings group, read per call (SCOPE_RUNTIME)."""

    window_size: tuple[int, int] | None
    max_edge: int
    elements_max: int
    threshold: float
    stable_s: float
    timeout_s: float
    max_steps: int
    max_cycles: int
    keep_images: int

    @classmethod
    def current(cls) -> Config:
        return cls(
            window_size=parse_size(settings.computer_use_window_size),
            max_edge=int(settings.computer_use_image_max_edge),
            elements_max=int(settings.computer_use_ui_elements_max),
            threshold=int(settings.computer_use_change_threshold_bp) / 10_000,
            stable_s=int(settings.computer_use_stable_ms) / 1000,
            timeout_s=int(settings.computer_use_change_timeout_ms) / 1000,
            max_steps=int(settings.computer_use_max_steps_per_batch),
            max_cycles=int(settings.computer_use_max_cycles_per_turn),
            keep_images=int(settings.computer_use_keep_images),
        )

    def exec_config(self) -> ExecConfig:
        return ExecConfig(threshold=self.threshold, stable_s=self.stable_s, timeout_s=self.timeout_s)


_SIZE = re.compile(r"^\s*(\d{3,5})\s*[xX]\s*(\d{3,5})\s*$")


def parse_size(raw: str) -> tuple[int, int] | None:
    match = _SIZE.match(raw or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


@dataclass
class OpOutput:
    body: dict[str, Any]
    image: bytes | None = None  # sent to the model
    obs_id: str | None = None
    timings: dict[str, int] | None = None
    steps: int = 0
    # Every NEW observation is kept in the capture history (amendment A3), whether or
    # not its image is sent to the model: (obs_id, png, window title).
    capture: tuple[str, bytes, str] | None = None


def _captured(obs: Observation, png: bytes) -> tuple[str, bytes, str]:
    return (obs.id, png, obs.window.title)


# ---- helpers ---------------------------------------------------------------------------------


def _window_row(w: WindowInfo) -> dict[str, Any]:
    row: dict[str, Any] = {
        "title": w.title,
        "process": w.process,
        "monitor": w.monitor,
        "size": [w.rect.width, w.rect.height],
    }
    if w.minimized:
        row["minimized"] = True
    reason = denial_reason(w)
    if reason:
        row["denied"] = True
    return row


def _require_target(backend: DesktopBackend, run: RunState) -> WindowInfo:
    if run.target is None:
        raise OpError("no_target", "call computer_focus_window first")
    fresh = backend.window(run.target.hwnd)
    if fresh is None:
        run.target = None
        raise OpError("no_target", "the target window was closed; call computer_focus_window again")
    run.target = fresh
    return fresh


def _observe(
    backend: DesktopBackend,
    run: RunState,
    cfg: Config,
    target: WindowInfo,
    *,
    elements: bool = True,
    region: tuple[int, int, int, int] | None = None,
) -> tuple[Observation, dict[str, int]]:
    t0 = time.monotonic()
    base = run.observations.get(run.latest_obs or "") if region is not None else None
    if region is not None and base is None:
        raise OpError("no_observation", "capture the screen before asking for a region")
    obs = build_observation(
        backend,
        obs_id=run.next_obs_id(),
        target=target,
        max_edge=cfg.max_edge,
        provider=run.provider,
        elements_max=cfg.elements_max if elements else 0,
        region=base.mapping if base is not None else None,
        region_image=region,
    )
    capture_ms = _ms(t0)
    run.remember(obs)
    return obs, {"capture_ms": capture_ms}


def _encode(obs: Observation) -> tuple[bytes, int]:
    t0 = time.monotonic()
    png = obs.frame.encode(obs.mapping.image_w, obs.mapping.image_h)
    return png, _ms(t0)


# ---- operations ------------------------------------------------------------------------------


def op_list_windows(backend: DesktopBackend) -> OpOutput:
    rows = [_window_row(w) for w in backend.list_windows()]
    return OpOutput({"status": "ok", "windows": rows})


def op_focus(backend: DesktopBackend, run: RunState, cfg: Config, title: str, process: str) -> OpOutput:
    title_re: re.Pattern[str] | None = None
    if title:
        try:
            title_re = re.compile(title, re.IGNORECASE)
        except re.error:
            title_re = re.compile(re.escape(title), re.IGNORECASE)
    wanted_process = process.strip().lower()
    matches = [
        w
        for w in backend.list_windows()
        if (title_re is None or title_re.search(w.title))
        and (not wanted_process or w.process.lower() == wanted_process)
    ]
    if not matches:
        raise OpError("not_found", "no visible window matches; call computer_list_windows")
    denied = [(w, denial_reason(w)) for w in matches]
    allowed = [w for w, reason in denied if reason is None]
    if not allowed:
        raise OpError("denied_window", denied[0][1] or "denied")
    target = backend.focus(allowed[0].hwnd, cfg.window_size if has(backend, "windows.resize") else None)
    if target is None:
        raise OpError("failed", "the window could not be brought to the foreground")
    run.target = target
    run.last_cursor = None
    obs, timings = _observe(backend, run, cfg, target)
    png, timings["encode_ms"] = _encode(obs)
    body: dict[str, Any] = {"status": "ok", "target": _window_row(target), "observation": obs.public()}
    missing = sorted(f for f in OPTIONAL_FEATURES if not has(backend, f))
    if missing:
        # A minimal backend (RES-0007): tell the model what it cannot use, e.g. element targets.
        body["unsupported"] = missing
    return OpOutput(body, png, obs.id, timings, capture=_captured(obs, png))


def op_capture(
    backend: DesktopBackend, run: RunState, cfg: Config, region: list[int] | None, elements: bool
) -> OpOutput:
    target = _require_target(backend, run)
    box = None
    if region is not None:
        if len(region) != 4:
            raise OpError("invalid", "region is [x1, y1, x2, y2] in the latest observation's image")
        box = (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
    obs, timings = _observe(backend, run, cfg, target, elements=elements, region=box)
    png, timings["encode_ms"] = _encode(obs)
    return OpOutput({"status": "ok", "observation": obs.public()}, png, obs.id, timings, capture=_captured(obs, png))


def op_active(backend: DesktopBackend, run: RunState) -> OpOutput:
    fg = backend.foreground()
    if fg is None:
        return OpOutput({"status": "ok", "foreground": None})
    body: dict[str, Any] = {"status": "ok", "foreground": _window_row(fg)}
    if run.target is not None:
        body["is_target"] = fg.pid == run.target.pid
        if has(backend, "windows.dialog"):
            body["dialog"] = backend.has_modal_dialog(run.target)
    return OpOutput(body)


def op_wait_change(backend: DesktopBackend, run: RunState, cfg: Config, timeout_ms: int | None) -> OpOutput:
    from app.computer_use.executor import abort_check

    target = _require_target(backend, run)
    latest = run.observations.get(run.latest_obs or "")
    t0 = time.monotonic()
    base = latest.frame.gray if latest is not None else backend.capture(target.rect).gray
    timeout_s = (timeout_ms / 1000) if timeout_ms else cfg.timeout_s

    def check(wait_s: float) -> str | None:
        return abort_check(run, wait_s)

    try:
        changed, _ = wait_for_change(
            backend, target.rect, base, timeout_s=timeout_s, threshold=cfg.threshold, abort_check=check
        )
        settled = True
        if changed:
            settled, _, _ = wait_until_stable(
                backend,
                target.rect,
                stable_s=cfg.stable_s,
                timeout_s=cfg.timeout_s,
                threshold=cfg.threshold,
                abort_check=check,
            )
    except Aborted as exc:
        run.aborted_by = exc.by
        return OpOutput({"status": "aborted", "by": exc.by})
    settle_ms = _ms(t0)
    if not changed:
        return OpOutput(
            {"status": "ok", "screen": f"unchanged since {run.latest_obs}", "obs": run.latest_obs},
            timings={"settle_ms": settle_ms},
        )
    obs, timings = _observe(backend, run, cfg, target)
    png, timings["encode_ms"] = _encode(obs)
    timings["settle_ms"] = settle_ms
    body = {"status": "ok", "observation": obs.public(changed=True, settled=settled)}
    return OpOutput(body, png, obs.id, timings, capture=_captured(obs, png))


def _is_stale(backend: DesktopBackend, run: RunState, obs: Observation, cfg: Config) -> bool:
    if obs.id != run.latest_obs:
        return True
    now = backend.capture(obs.mapping.rect).gray
    if dhash(now) == dhash(obs.frame.gray) and now == obs.frame.gray:
        return False
    caret = backend.caret_rect() if has(backend, "ui.caret") else None
    masked = mask_blocks(obs.mapping.rect, [caret]) if caret else set()
    return changed_fraction(obs.frame.gray, now, masked) > cfg.threshold


def op_perform(backend: DesktopBackend, run: RunState, cfg: Config, batch: dsl.ActionBatch) -> OpOutput:
    target = _require_target(backend, run)
    obs = run.observations.get(batch.obs)
    if obs is None:
        raise OpError("stale_observation", f"{batch.obs} is not the latest observation ({run.latest_obs})")
    if _is_stale(backend, run, obs, cfg):
        raise OpError("stale_observation", f"the screen changed since {batch.obs}; look at it again")

    # -- act ------------------------------------------------------------------------------------
    t0 = time.monotonic()
    result = Executor(backend, run, obs, cfg.exec_config()).execute(batch)
    act_ms = _ms(t0)
    timings: dict[str, int] = {"act_ms": act_ms}
    body: dict[str, Any] = result.public()
    if result.status == "aborted":
        return OpOutput(body, timings=timings, steps=result.executed)

    # -- after --------------------------------------------------------------------------------
    t1 = time.monotonic()
    settled: bool | None = None
    target = backend.window(target.hwnd) or target
    run.target = target

    def check(wait_s: float) -> str | None:
        from app.computer_use.executor import abort_check

        return abort_check(run, wait_s)

    try:
        if result.status == "ok" and batch.after == "wait_for_change":
            changed_now, _ = wait_for_change(
                backend,
                target.rect,
                obs.frame.gray,
                timeout_s=cfg.timeout_s,
                threshold=cfg.threshold,
                abort_check=check,
            )
            if changed_now:
                settled, _, _ = wait_until_stable(
                    backend,
                    target.rect,
                    stable_s=cfg.stable_s,
                    timeout_s=cfg.timeout_s,
                    threshold=cfg.threshold,
                    abort_check=check,
                )
            else:
                settled = False
        elif result.status == "ok" and batch.after == "wait_until_stable":
            settled, _, _ = wait_until_stable(
                backend,
                target.rect,
                stable_s=cfg.stable_s,
                timeout_s=cfg.timeout_s,
                threshold=cfg.threshold,
                abort_check=check,
            )
    except Aborted as exc:
        run.aborted_by = exc.by
        body.update(status="aborted", by=exc.by)
        return OpOutput(body, timings={**timings, "settle_ms": _ms(t1)}, steps=result.executed)
    timings["settle_ms"] = _ms(t1)

    # -- expect + capture -------------------------------------------------------------------------
    expect_ok: list[bool] = []
    if batch.expect:
        expect_ok = evaluate_expectations(backend, run, obs, batch.expect, obs.frame.gray)
        body["expect"] = expect_ok

    t2 = time.monotonic()
    probe = backend.capture(target.rect)
    changed = differs(obs.frame.gray, probe.gray)
    want_image = batch.observe == "always" or (
        batch.observe == "auto" and (changed or not all(expect_ok) or result.status != "ok")
    )
    if not changed and batch.observe != "always":
        timings["capture_ms"] = _ms(t2)
        body["screen"] = f"unchanged since {obs.id}"
        body["obs"] = obs.id
        if not want_image:
            return OpOutput(body, timings=timings, steps=result.executed)
    new_obs, _ = _observe(backend, run, cfg, target)
    timings["capture_ms"] = _ms(t2)
    body["observation"] = new_obs.public(changed=changed, settled=settled)
    body.pop("obs", None)
    png, timings["encode_ms"] = _encode(new_obs)
    if not want_image:
        # Not sent to the model, but still part of the capture history (A3).
        return OpOutput(body, None, new_obs.id, timings, result.executed, capture=_captured(new_obs, png))
    return OpOutput(body, png, new_obs.id, timings, result.executed, capture=_captured(new_obs, png))


__all__ = [
    "Config",
    "OpError",
    "OpOutput",
    "op_active",
    "op_capture",
    "op_focus",
    "op_list_windows",
    "op_perform",
    "op_wait_change",
    "parse_size",
]
