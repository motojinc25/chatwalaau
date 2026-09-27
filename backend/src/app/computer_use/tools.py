"""Computer Use Tool Surface (CTR-0229, PRP-0189 Section 2.4, UDR-0171).

Seven plain ``async def`` tools with ``Annotated[..., Field(...)]`` parameters -- the
same plain-callable form every built-in uses, which MAF builds as ``never_require``
(UDR-0161 D1). Nothing here waits for a human: every precondition below FAILS like
any other tool error.

Per call, in order:

1. H7 -- the run came from a loopback peer through the AG-UI endpoint (a run that
   never called ``state.begin_run`` gets ``origin_not_local``);
2. H1-H6 still hold (``unavailable`` otherwise);
3. the run has not been aborted (the latch, D11);
4. the per-turn cycle cap (``cycle_limit``, D9);
5. the desktop holder (``busy``, D6), then H8 (``desktop_locked``).

The work runs on the desktop worker; a ``CancelledError`` here (the Stop button) sets
the abort event so the worker stops at its next check. Each call writes one trace
line (D9). An image goes back as a ``Content`` item tagged with its observation id,
which is what the retention middleware (CTR-0234) keys on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Annotated, Any

from agent_framework import Content
from pydantic import Field

from app.computer_use import captures, dsl, engine, trace, worker
from app.computer_use import state as run_state
from app.computer_use.availability import availability
from app.computer_use.backend import has
from app.computer_use.policy import secret_values
from app.computer_use.retention import TAG

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _dump(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _content(out: engine.OpOutput) -> str | list[Content]:
    text = _dump(out.body)
    if out.image is None:
        return text
    return [
        Content.from_text(text),
        Content.from_data(data=out.image, media_type="image/png", additional_properties={TAG: out.obs_id or ""}),
    ]


def _trace(tool: str, run: run_state.RunState | None, out: engine.OpOutput, model_ms: int | None) -> None:
    record: dict[str, Any] = {
        "tool": tool,
        "status": out.body.get("status", ""),
        "thread": run.thread_id if run else "",
        "run": run.run_id if run else "",
        "model": run.model if run else "",
        "model_ms": model_ms,
        "steps": out.steps,
        "image_sent": out.image is not None,
        "image_bytes": len(out.image) if out.image is not None else 0,
    }
    record.update(out.timings or {})
    trace.write(record)


async def _invoke(
    tool: str,
    op: Callable[..., engine.OpOutput],
    *args: Any,
    prepare: Callable[[engine.Config], None] | None = None,
) -> str | list[Content]:
    run = run_state.current_run()
    if run is None or not run.local_origin:
        return _dump(
            {
                "status": "origin_not_local",
                "reason": "Computer Use runs only for a chat on this machine; never from Teams, the API or a schedule",
            }
        )
    verdict = availability()
    if not verdict.offered:
        return _dump({"status": "unavailable", "reason": verdict.reason})
    if run.aborted_by:
        return _dump({"status": "aborted", "by": run.aborted_by, "reason": "stopped by the user for this turn"})
    cfg = engine.Config.current()
    model_ms = run.model_ms()
    if run.cycles >= cfg.max_cycles:
        out = engine.OpOutput({"status": "cycle_limit", "reason": f"{cfg.max_cycles} computer calls this turn"})
        _trace(tool, run, out, model_ms)
        return _dump(out.body)
    run.cycles += 1
    if prepare is not None:
        try:
            prepare(cfg)
        except dsl.DslError as exc:
            out = engine.OpOutput({"status": exc.code, "reason": str(exc)})
            run.last_return = time.monotonic()
            _trace(tool, run, out, model_ms)
            return _dump(out.body)
    if not run_state.try_hold():
        return _dump({"status": "busy", "reason": "another chat is using the desktop"})
    try:
        if await worker.run(lambda backend: has(backend, "session.lock") and backend.desktop_locked()):
            out = engine.OpOutput({"status": "desktop_locked", "reason": "the Windows session is locked"})
        else:
            out = await worker.run(op, run, cfg, *args)
    except asyncio.CancelledError:
        # The Stop button: the fetch was aborted and this coroutine cancelled. The
        # worker keeps running its job until it checks the event, so set it (D11).
        run_state.request_abort("stop")
        run.aborted_by = "stop"
        raise
    except engine.OpError as exc:
        out = engine.OpOutput({"status": exc.status, "reason": exc.reason})
        if exc.status == "provider_unavailable":
            # The provider process is gone (PRP-0190, CTR-0237): its frame handles and
            # element keys died with it, so no earlier observation may be acted on.
            run.observations.clear()
            run.latest_obs = None
    except Exception as exc:
        logger.exception("computer tool %s failed", tool)
        out = engine.OpOutput({"status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        run_state.release_hold()
        run.last_return = time.monotonic()
    if out.capture is not None:
        # The capture history (amendment A3): every new observation, per chat, for the
        # operator's live viewer -- never for the model, whose context the retention
        # middleware keeps at the newest image.
        obs_id, png, window = out.capture
        await asyncio.to_thread(
            captures.save, thread_id=run.thread_id, run_id=run.run_id, obs_id=obs_id, png=png, window=window
        )
    _trace(tool, run, out, model_ms)
    return _content(out)


# ---- the seven tools ---------------------------------------------------------------------------


async def computer_list_windows() -> str | list[Content]:
    """List the visible top-level windows on all monitors (title, process, monitor, size).

    Windows marked "denied" cannot be targeted. Use this to find the application to
    work in, then call computer_focus_window.
    """

    def op(backend: Any, _run: run_state.RunState, _cfg: engine.Config) -> engine.OpOutput:
        return engine.op_list_windows(backend)

    return await _invoke("computer_list_windows", op)


async def computer_focus_window(
    title: Annotated[str, Field(description="Regex or text matched against window titles (case-insensitive).")] = "",
    process: Annotated[str, Field(description="Exact process name, e.g. 'notepad.exe'.")] = "",
) -> str | list[Content]:
    """Select, restore and bring to the front the window to work in, and lock it as the target.

    Every later action is refused unless this window (or a dialog of the same program)
    is in the foreground. Returns the first observation (screenshot + element list).
    """
    return await _invoke("computer_focus_window", engine.op_focus, title, process)


async def computer_capture_screen(
    region: Annotated[
        list[int] | None,
        Field(description="Optional [x1, y1, x2, y2] in the LATEST image: returns a zoomed view of that area."),
    ] = None,
    elements: Annotated[bool, Field(description="Include the UI element list (ids usable as targets).")] = True,
) -> str | list[Content]:
    """Look at the target window now: a screenshot plus its UI elements as [id, type, name, box]."""
    return await _invoke("computer_capture_screen", engine.op_capture, region, elements)


_ACTIONS_HELP = (
    "Steps executed in order. Each is an object with 'type': "
    "click|double_click|right_click|move {element | x,y}; drag {from:{..}, to:{..}}; "
    "type_text {text | secret}; keypress {keys: ['ctrl','s'] or 'enter'}; "
    "scroll {dy, dx?, element|x,y?} (dy>0 scrolls down); "
    "wait_for_change {timeout_ms?}; wait_until_stable {stable_ms?, timeout_ms?}; "
    "wait_for {cond, timeout_ms}; if {cond, then:[..], else:[..]}; repeat {times<=10, until?:cond, body:[..]}. "
    "Targets: {'element':'e12'} (preferred) or {'x':420,'y':315} in the image of 'obs'. "
    "cond: {'element':{'name':..,'control':'Button'},'present':true} | {'window_title':regex} | "
    "{'dialog':true} | {'changed':true}."
)


async def computer_perform_actions(
    obs: Annotated[str, Field(description="Id of the observation these actions were planned on, e.g. 'o7'.")],
    actions: Annotated[list[dict[str, Any]], Field(description=_ACTIONS_HELP)],
    after: Annotated[
        str, Field(description="wait_until_stable (default) | wait_for_change | none -- run after the steps.")
    ] = "wait_until_stable",
    expect: Annotated[
        list[dict[str, Any]] | None, Field(description="Conditions that should hold afterwards (same form as cond).")
    ] = None,
    observe: Annotated[
        str, Field(description="auto (image only if the screen changed or an expectation failed) | always | never")
    ] = "auto",
    state: Annotated[str, Field(description="Optional short label of the screen state, e.g. 'login_form'.")] = "",
) -> str | list[Content]:
    """Execute ONE batch of UI steps on the target, wait until the screen settles, and return the result.

    Plan the whole next step as one batch (many actions per call). The result reports
    per-step status, the expectations, timings and the resulting observation; a new
    screenshot is attached only when the screen changed or something did not go as
    expected.
    """
    parsed: list[dsl.ActionBatch] = []

    def prepare(cfg: engine.Config) -> None:
        parsed.append(
            dsl.parse_batch(
                obs=obs,
                actions=actions,
                after=after,
                expect=expect,
                observe=observe,
                state=state,
                max_steps=cfg.max_steps,
                secret_values=secret_values(),
            )
        )

    def op(backend: Any, run: run_state.RunState, cfg: engine.Config) -> engine.OpOutput:
        return engine.op_perform(backend, run, cfg, parsed[0])

    return await _invoke("computer_perform_actions", op, prepare=prepare)


async def computer_get_active_window() -> str | list[Content]:
    """Which window is in front, whether it belongs to the target, and whether a modal dialog is open."""

    def op(backend: Any, run: run_state.RunState, _cfg: engine.Config) -> engine.OpOutput:
        return engine.op_active(backend, run)

    return await _invoke("computer_get_active_window", op)


async def computer_wait_for_change(
    timeout_ms: Annotated[
        int | None, Field(description="Maximum wait in ms (default from settings, max 30000).")
    ] = None,
) -> str | list[Content]:
    """Wait, without acting, until the target changes and settles (e.g. a long load). Image only if it changed."""
    bounded = min(max(int(timeout_ms), 100), dsl.MAX_TIMEOUT_MS) if timeout_ms else None
    return await _invoke("computer_wait_for_change", engine.op_wait_change, bounded)


async def computer_abort(
    reason: Annotated[str, Field(description="Why the task ends here (short).")],
) -> str:
    """End the desktop task: give up, or report that it cannot be completed. Releases the target window."""
    run = run_state.current_run()
    if run is None or not run.local_origin:
        return _dump({"status": "origin_not_local"})
    run.target = None
    run.observations.clear()
    run.latest_obs = None
    out = engine.OpOutput({"status": "ok", "ended": True, "reason": (reason or "")[:200]})
    _trace("computer_abort", run, out, run.model_ms())
    run.last_return = time.monotonic()
    return _dump(out.body)


COMPUTER_TOOLS = (
    computer_list_windows,
    computer_focus_window,
    computer_capture_screen,
    computer_perform_actions,
    computer_get_active_window,
    computer_wait_for_change,
    computer_abort,
)
COMPUTER_TOOL_NAMES = tuple(t.__name__ for t in COMPUTER_TOOLS)


__all__ = [
    "COMPUTER_TOOLS",
    "COMPUTER_TOOL_NAMES",
    "computer_abort",
    "computer_capture_screen",
    "computer_focus_window",
    "computer_get_active_window",
    "computer_list_windows",
    "computer_perform_actions",
    "computer_wait_for_change",
]
