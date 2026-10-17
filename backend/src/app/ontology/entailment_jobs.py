"""Entailment computed after every write, as a Pipeline Job (CTR-0170 v9, PRP-0211, UDR-0193 D7, D9, D12).

The ``ontology-entailment`` job type (contributed to the Pipeline Job engine like
``teams-meeting`` and ``workflow``, UDR-0074 D7) computes the stored entailment results
of ``entailment_results``. Each mode runs in a CHILD PROCESS (``app.pipeline.process``),
so a superseded, cancelled or timed-out build really stops. The type has its own slot
(``max_concurrent=1``: builds never take the slots of rag-ingest or workflows) and the
time limit ``ontology_entailment_build_timeout_seconds``.

- After every write of an ontology (``store.notify_written``) a build of its STANDARD
  key (scope ``all``, imports as the App Settings default) is queued for RDFS then OWL 2
  RL -- and for every ontology that imports it, since their import signature changed.
  Writes are debounced (``DEBOUNCE_SECONDS``); a newer write cancels the jobs of older
  versions. ``ontology_entailment_precompute`` switches this off.
- A search whose stored result is missing (another scope, imports off, precompute off,
  or not built yet) queues a build of ITS key and waits for it within its own time limit
  (``wait_ready``), else answers ``entailment_pending``.
- Only the newest ``KEEP_FINISHED_JOBS`` finished job files per ontology are kept.

Active only while started from the app's lifespan with the Pipeline enabled; without it
searches compute entailment themselves, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from app.core.config import settings

if TYPE_CHECKING:
    from app.pipeline.models import Job
    from app.pipeline.store import PipelineStore

logger = logging.getLogger(__name__)

JOB_TYPE = "ontology-entailment"
DEBOUNCE_SECONDS = 2.0
KEEP_FINISHED_JOBS = 3
POLL_SECONDS = 0.25
LIVE = ("pending", "running")

_loop: asyncio.AbstractEventLoop | None = None
_timers: dict[str, asyncio.TimerHandle] = {}
# (key hash, mode) -> the job computing it; ontology id -> its job ids.
_key_jobs: dict[tuple[str, str], str] = {}
_ontology_jobs: dict[str, list[str]] = {}
_tasks: set[asyncio.Task[Any]] = set()


def build_modes() -> tuple[str, ...]:
    from app.ontology import entailment

    return (entailment.RDFS, entailment.OWL_RL)


def active() -> bool:
    """True when builds run as Pipeline Jobs (started, Pipeline enabled)."""
    return _loop is not None and bool(settings.pipeline_enabled)


# ---- Job type ---------------------------------------------------------------------


def _build_timeout() -> float:
    return float(max(0, int(settings.ontology_entailment_build_timeout_seconds)))


def job_type() -> Any:
    from app.pipeline.registry import JobType, ParamSpec

    return JobType(
        name=JOB_TYPE,
        label="Ontology entailment",
        description=(
            "Compute the RDFS / OWL 2 RL entailment of one ontology version and store it beside "
            "the ontology, so searches with entailment answer at once. Queued automatically "
            "after every save."
        ),
        runner=run_job,
        params=[
            ParamSpec(name="ontology_id", label="Ontology id", type="string", required=True),
            ParamSpec(name="modes", label="Modes", type="string", help="Comma-separated: rdfs, owl-rl."),
        ],
        timeout_seconds=_build_timeout,
        max_concurrent=1,
    )


def _modes_of(params: dict[str, Any]) -> list[str]:
    from app.ontology import entailment

    raw = params.get("modes") or list(build_modes())
    if isinstance(raw, str):
        raw = [part for part in raw.replace(" ", "").split(",") if part]
    return [entailment.parse_mode(mode) for mode in raw if entailment.parse_mode(mode) != entailment.SIMPLE]


async def run_job(job: Job, job_store: PipelineStore, cancel_event: asyncio.Event) -> None:
    """The ``ontology-entailment`` runner: each mode in a child process."""
    from datetime import UTC, datetime

    from app.ontology import entailment, store
    from app.ontology import entailment_results as results
    from app.pipeline.engine import queue
    from app.pipeline.models import JobStatus
    from app.pipeline.process import ProcessCancelled, run_in_process, settings_snapshot

    params = dict(job.params)
    ontology_id = str(params.get("ontology_id") or "")
    try:
        modes = _modes_of(params)
    except entailment.EntailmentError as exc:
        modes = []
        job.error = str(exc)
    snapshot = settings_snapshot()
    done: list[dict[str, Any]] = []

    def finish(status: Any, error: str | None = None) -> None:
        job.status = status
        job.error = error
        job.completed_at = datetime.now(UTC).isoformat()
        job_store.save(job)

    if not modes:
        finish(JobStatus.failed, job.error or "No entailment mode to compute.")
        return
    for position, mode in enumerate(modes):
        job.progress = int(position * 100 / len(modes))
        job.progress_message = f"Computing {entailment.MODE_LABELS[mode]}"
        job_store.save(job)
        try:
            summary = await run_in_process(
                "app.ontology.entailment_results:build_from_job",
                {**params, "mode": mode},
                cancel_event=cancel_event,
                settings=snapshot,
            )
        except ProcessCancelled:
            if queue.timed_out(job.id):
                with contextlib.suppress(Exception):
                    key = await asyncio.to_thread(
                        store.query_key, ontology_id, params.get("scope"), params.get("imports")
                    )
                    await asyncio.to_thread(results.save_timeout, key, mode, limit_seconds=_build_timeout())
                return  # the engine records the job as failed (timeout)
            finish(JobStatus.cancelled)
            return
        except KeyError:
            finish(JobStatus.failed, f"Ontology not found: {ontology_id}")
            return
        except Exception as exc:
            logger.warning("ontology entailment build failed for %s (%s)", ontology_id, mode, exc_info=True)
            finish(JobStatus.failed, f"{type(exc).__name__}: {exc}")
            return
        done.append(summary)
    job.progress = 100
    job.progress_message = "Done"
    job.result = {"ontology_id": ontology_id, "modes": done}
    finish(JobStatus.completed)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(_retain_jobs, ontology_id, job.id)


def _retain_jobs(ontology_id: str, current: str) -> None:
    """Delete finished jobs of the ontology beyond the newest ``KEEP_FINISHED_JOBS``."""
    from app.pipeline.engine import queue

    finished = [
        job
        for job in queue.list_jobs()
        if job.type == JOB_TYPE
        and job.params.get("ontology_id") == ontology_id
        and job.status.value not in LIVE
        and job.id != current
    ]
    finished.sort(key=lambda job: job.created_at, reverse=True)
    for job in finished[max(0, KEEP_FINISHED_JOBS - 1) :]:
        with contextlib.suppress(Exception):
            queue.delete_job(job.id)
    ids = _ontology_jobs.get(ontology_id)
    if ids:
        _ontology_jobs[ontology_id] = [job_id for job_id in ids if queue.get_status(job_id) is not None]


# ---- Submitting ---------------------------------------------------------------------


async def submit(
    ontology_id: str,
    *,
    scope: str | None = None,
    imports: bool | None = None,
    modes: list[str] | tuple[str, ...] | None = None,
    reason: str = "save",
) -> Job:
    """Queue one ``ontology-entailment`` job and remember which keys it computes."""
    from app.ontology import entailment_results as results
    from app.ontology import store
    from app.pipeline.engine import queue

    chosen = list(modes or build_modes())
    job = await queue.submit(
        JOB_TYPE,
        {"ontology_id": ontology_id, "scope": scope, "imports": imports, "modes": chosen, "reason": reason},
    )
    _ontology_jobs.setdefault(ontology_id, []).append(job.id)
    try:
        key = await asyncio.to_thread(store.query_key, ontology_id, scope, imports)
    except (KeyError, ValueError):
        return job
    for mode in chosen:
        _key_jobs[(results.key_hash(key), mode)] = job.id
    return job


async def cancel_live(ontology_id: str) -> None:
    """Cancel the pending and running jobs of one ontology (a newer version superseded them)."""
    from app.pipeline.engine import queue

    for job_id in list(_ontology_jobs.get(ontology_id, [])):
        job = queue.get_status(job_id)
        if job is not None and job.status.value in LIVE:
            with contextlib.suppress(ValueError):
                await queue.cancel(job_id)


def _importers_closure(ontology_id: str) -> list[str]:
    """Every ontology that imports ``ontology_id``, directly or through others."""
    from app.ontology.imports import importers_of

    found: list[str] = []
    todo = [ontology_id]
    while todo:
        current = todo.pop()
        for item in importers_of(current):
            if item["id"] not in found and item["id"] != ontology_id:
                found.append(item["id"])
                todo.append(item["id"])
    return found


async def _after_write(ontology_id: str) -> None:
    from app.ontology import store

    _timers.pop(ontology_id, None)
    try:
        importers = await asyncio.to_thread(_importers_closure, ontology_id)
    except Exception:
        logger.debug("importers of %s could not be listed", ontology_id, exc_info=True)
        importers = []
    for target in [ontology_id, *importers]:
        if await asyncio.to_thread(store.get_entry, target) is None:
            continue
        await cancel_live(target)
        try:
            await submit(target, reason="save")
        except Exception:
            logger.warning("could not queue the entailment build of %s", target, exc_info=True)


async def _after_delete(ontology_id: str) -> None:
    from app.ontology import entailment_results as results

    timer = _timers.pop(ontology_id, None)
    if timer is not None:
        timer.cancel()
    await cancel_live(ontology_id)
    await asyncio.to_thread(results.remove, ontology_id)
    if settings.ontology_entailment_precompute:
        try:
            importers = await asyncio.to_thread(_importers_closure, ontology_id)
        except Exception:
            importers = []
        for target in importers:
            _schedule(target, "write")


def _spawn(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _schedule(ontology_id: str, kind: str) -> None:
    """On the event loop: debounce a write, or clean up after a delete."""
    if kind == "delete":
        _spawn(_after_delete(ontology_id))
        return
    if not settings.ontology_entailment_precompute:
        return
    timer = _timers.pop(ontology_id, None)
    if timer is not None:
        timer.cancel()
    loop = asyncio.get_running_loop()
    _timers[ontology_id] = loop.call_later(DEBOUNCE_SECONDS, lambda: _spawn(_after_write(ontology_id)))


def _on_write(ontology_id: str, kind: str) -> None:
    """``store`` write listener (any thread)."""
    loop = _loop
    if loop is None or not settings.pipeline_enabled or kind == "shacl":
        return  # a changed SHACL block (PRP-0212) changes no entailment
    with contextlib.suppress(RuntimeError):  # the loop is closing
        loop.call_soon_threadsafe(_schedule, ontology_id, kind)


def start(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Register the job type and the write listener (app lifespan, Pipeline enabled)."""
    global _loop
    from app.ontology import store
    from app.pipeline.registry import register_job_type

    register_job_type(job_type(), replace=True)
    _loop = loop or asyncio.get_running_loop()
    store.add_write_listener(_on_write)


def stop() -> None:
    global _loop
    from app.ontology import store

    store.remove_write_listener(_on_write)
    for timer in _timers.values():
        timer.cancel()
    _timers.clear()
    _loop = None


# ---- Searches waiting for a result (UDR-0193 D9) ------------------------------------


async def wait_ready(
    ontology_id: str, scope: Any, imports: bool | None, mode: str, deadline: float | None
) -> dict[str, Any]:
    """The stored record of the search's key and mode, building it when missing.

    Waits until ``deadline`` (``time.monotonic()``; None = no limit), then raises
    ``EntailmentPending``. A ``ready`` record or a refusal record is returned as it is
    (the worker raises the refusal).
    """
    from app.ontology import entailment, store
    from app.ontology import entailment_results as results
    from app.pipeline.engine import queue

    key = await asyncio.to_thread(store.query_key, ontology_id, scope, imports)
    slot = (results.key_hash(key), mode)
    started = time.monotonic()
    resubmitted = False
    while True:
        record = await asyncio.to_thread(results.lookup, key, mode)
        if record is not None:
            return record
        job_id = _key_jobs.get(slot)
        job = queue.get_status(job_id) if job_id else None
        if job is None or job.status.value not in LIVE:
            if job is not None and job.status.value in ("failed", "completed") and resubmitted:
                raise entailment.EntailmentFailed(
                    f"{entailment.MODE_LABELS[mode]} could not be computed: {job.error or 'the build failed'}.",
                    mode=mode,
                )
            job = await submit(
                ontology_id, scope=results.scope_text(key[3]), imports=imports, modes=[mode], reason="search"
            )
            _key_jobs[slot] = job.id
            resubmitted = True
        if deadline is not None and time.monotonic() >= deadline:
            state = "running" if job.status.value == "running" else "queued"
            raise entailment.EntailmentPending(
                f"{entailment.MODE_LABELS[mode]} of this ontology is still being computed "
                f"({state}, {time.monotonic() - started:.0f} s); try again shortly.",
                mode=mode,
                state=state,
                job_id=job.id,
            )
        await asyncio.sleep(POLL_SECONDS)


def status(ontology_id: str, scope: Any = None, imports: bool | None = None) -> dict[str, Any]:
    """Per mode, the state of the stored result of a key (UDR-0193 D12); blocking (files)."""
    from app.ontology import entailment, store
    from app.ontology import entailment_results as results
    from app.pipeline.engine import queue

    key = store.query_key(ontology_id, scope, imports)
    modes: dict[str, Any] = {}
    for mode in build_modes():
        record = results.lookup(key, mode)
        item: dict[str, Any] = {
            "state": "missing",
            "label": entailment.MODE_LABELS[mode],
            "job_id": None,
        }
        if record is not None:
            item.update(
                state=record.get("state"),
                stated=record.get("stated"),
                inferred=record.get("inferred"),
                elapsed_ms=record.get("elapsed_ms"),
                built_at=record.get("built_at"),
                findings_count=record.get("findings_count", 0),
                same_as_pairs=record.get("same_as_pairs", 0),
                message=record.get("message"),
                detail=record.get("detail"),
            )
        else:
            job_id = _key_jobs.get((results.key_hash(key), mode))
            job = queue.get_status(job_id) if job_id and settings.pipeline_enabled else None
            if job is not None and job.status.value in LIVE:
                item.update(state="queued" if job.status.value == "pending" else "running", job_id=job.id)
                if job.started_at:
                    from datetime import datetime

                    with contextlib.suppress(ValueError):
                        item["running_seconds"] = round(
                            time.time() - datetime.fromisoformat(job.started_at).timestamp(), 1
                        )
        modes[mode] = item
    return {
        "ontology_id": ontology_id,
        "revision": key[1],
        "scope": results.scope_text(key[3]),
        "precompute": bool(settings.ontology_entailment_precompute) and active(),
        "modes": modes,
    }


__all__ = [
    "DEBOUNCE_SECONDS",
    "JOB_TYPE",
    "KEEP_FINISHED_JOBS",
    "active",
    "cancel_live",
    "job_type",
    "run_job",
    "start",
    "status",
    "stop",
    "submit",
    "wait_ready",
]
