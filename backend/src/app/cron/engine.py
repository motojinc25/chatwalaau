"""Cron Tick Engine (CTR-0130 / UDR-0067 D3-D5).

A FastAPI-lifespan asyncio task wakes every CRON_TICK_SECONDS and:

1. Acquires the single-flight ``.tick.lock`` (CTR-0130 D5); skips the tick if held.
2. Loads enabled jobs and selects due ones (``next_run_at <= now``).
3. Per due job, decides run-vs-fast-forward by the grace window (UDR-0067 D4):
   - within grace  -> ADVANCE next_run_at to the next occurrence, persist, then run.
   - beyond grace  -> FAST-FORWARD to the first future occurrence, persist, no run.
4. Advances next_run_at and persists it BEFORE dispatching the run (UDR-0067 D3),
   so a crash mid-run does not re-fire the job on restart (no crash loop).
5. Dispatches the run OFF the tick path (a separate asyncio task) and releases the
   lock.

The loop never starts when CRON_ENABLED is false (UDR-0067 D10).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
import logging

from app.core.config import settings
from app.cron import store
from app.cron.executor import RunResult, run_internal, run_script
from app.cron.lock import acquire, release
from app.cron.models import (
    RUN_FAILED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_SCHEDULED,
    STATE_SUCCESS,
)
from app.cron.schedule import compute_next_run_at, now_tz, parse_iso

logger = logging.getLogger(__name__)

# Jobs whose run task is currently in flight -- guards against an interval shorter
# than the run from overlapping itself.
_running_jobs: set[str] = set()
_run_tasks: set[asyncio.Task] = set()
_loop_task: asyncio.Task | None = None


def _lock_ttl() -> float:
    """A lock is stale after a few ticks (>= 2 minutes) so a dead holder frees it."""
    return max(float(settings.cron_tick_seconds) * 3.0, 120.0)


async def tick_once() -> None:
    """Run a single tick under the single-flight lock. Never raises."""
    lock = store.lock_path()
    if not acquire(lock, ttl_seconds=_lock_ttl()):
        logger.debug("cron tick skipped; lock held")
        return
    try:
        now = now_tz()
        grace = min(7200.0, max(120.0, float(settings.cron_grace_window_seconds)))
        for job in store.list_jobs():
            if not job.get("enabled"):
                continue
            nxt = parse_iso(job.get("next_run_at"))
            if nxt is None or nxt > now:
                continue
            lateness = (now - nxt).total_seconds()
            if lateness > grace:
                _fast_forward(job, now)
                continue
            _advance_and_dispatch(job, now)
    finally:
        release(lock)


def _fast_forward(job: dict, now: datetime) -> None:
    """Reschedule a badly-late job to its next future occurrence without running."""
    nxt = compute_next_run_at(job["schedule"], after=now)
    job["next_run_at"] = nxt.isoformat() if nxt else None
    job["state"] = STATE_SCHEDULED if nxt else STATE_COMPLETED
    if nxt is None:
        job["enabled"] = False
    job["updated_at"] = datetime.now(UTC).isoformat()
    store.save_job(job)
    logger.info("cron job %s fast-forwarded past grace window (lateness exceeded)", job["id"])


def _advance_and_dispatch(job: dict, now: datetime) -> None:
    """Advance next_run_at, persist BEFORE running, then dispatch the run."""
    job_id = job["id"]
    if job_id in _running_jobs:
        logger.debug("cron job %s still running; skipping overlapping dispatch", job_id)
        return

    is_oneshot = job["schedule"].get("type") == "oneshot"
    if is_oneshot:
        job["next_run_at"] = None
        job["enabled"] = False  # never re-fire (advance-before-execute, UDR-0067 D3)
    else:
        nxt = compute_next_run_at(job["schedule"], after=now)
        job["next_run_at"] = nxt.isoformat() if nxt else None
        if nxt is None:
            job["enabled"] = False
    job["state"] = STATE_RUNNING
    job["last_run_at"] = datetime.now(UTC).isoformat()
    job["updated_at"] = job["last_run_at"]
    store.save_job(job)

    _run_id, run_dir = store.start_run(job)
    _running_jobs.add(job_id)
    task = asyncio.create_task(_execute(job_id, dict(job), run_dir, is_oneshot))
    _run_tasks.add(task)
    task.add_done_callback(_run_tasks.discard)


async def _execute(job_id: str, job: dict, run_dir, is_oneshot: bool) -> None:
    """Run the script, write the run log, and record the outcome on the job."""
    started = datetime.now(UTC)
    try:
        # Managed jobs (kind="internal") run a registered in-process handler (no script,
        # no CODING_ENABLED); ordinary jobs run their workspace script (PRP-0097 task 4).
        if job.get("kind") == "internal":
            result = await run_internal(str(job.get("internal_action", "")))
        else:
            result = await run_script(job.get("script") or {})
    except Exception:  # the harness shouldn't raise, but never let a run crash the loop
        logger.exception("cron run crashed for job %s", job_id)
        result = RunResult(RUN_FAILED, None, "", "", "internal error")
    finally:
        _running_jobs.discard(job_id)

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    store.finish_run(
        run_dir,
        status=result.status,
        exit_code=result.exit_code,
        duration_ms=duration_ms,
        interpreter=result.interpreter,
        stdout=result.stdout,
        stderr=result.stderr,
    )

    # Record the outcome on the live job (it may have been edited/deleted mid-run).
    current = store.get_job(job_id)
    if current is None:
        return
    current["last_run_at"] = started.isoformat()
    current["last_status"] = result.status
    if is_oneshot:
        current["state"] = STATE_COMPLETED
    else:
        current["state"] = STATE_SUCCESS if result.status == "success" else STATE_FAILED
    current["updated_at"] = datetime.now(UTC).isoformat()
    store.save_job(current)
    logger.info("cron job %s run finished: %s", job_id, result.status)


async def _run_loop() -> None:
    """The lifespan tick loop. Cancelled on shutdown."""
    interval = min(3600, max(5, int(settings.cron_tick_seconds)))
    logger.info("Cron scheduler started (tick=%ds, dir=%s)", interval, settings.cron_jobs_dir)
    try:
        while True:
            try:
                await tick_once()
            except Exception:
                logger.exception("cron tick failed")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Cron scheduler stopped")
        raise


def start_scheduler() -> None:
    """Start the tick loop (idempotent). Call from the FastAPI lifespan startup."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(_run_loop())


async def stop_scheduler(timeout: float = 5.0) -> None:
    """Cancel the tick loop and drain in-flight runs (best-effort) at shutdown."""
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _loop_task
        _loop_task = None
    if _run_tasks:
        pending = list(_run_tasks)
        _, still = await asyncio.wait(pending, timeout=timeout)
        for t in still:
            t.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)


__all__ = ["start_scheduler", "stop_scheduler", "tick_once"]
