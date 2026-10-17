"""SHACL validation after every write, as a Pipeline Job (CTR-0170 v10, PRP-0212, UDR-0194 D5).

The ``ontology-shacl`` job type (contributed to the Pipeline Job engine like
``ontology-entailment``) validates the CURRENT version of one ontology whose catalog
entry has a ``shacl`` block and stores the result (``shacl_results``). The work runs in
a CHILD PROCESS (``app.pipeline.process``), so cancel, supersede and the time limit
``ontology_shacl_build_timeout_seconds`` really stop it; the type has its own slot.

A job is queued -- debounced ``DEBOUNCE_SECONDS`` per ontology, a newer one cancelling
the older -- after:

1. every write of an ontology that has a ``shacl`` block;
2. a write of an ontology another one names in ``shapes_ontologies`` (for each user);
3. a write of an ontology another one imports (for each importer with a block);
4. a change of the block itself (``store.set_shacl``).

With an inference mode the job first waits (within its time limit) for the stored
entailment the ``ontology-entailment`` job is computing for the same version, so the
reasoner does not run twice.
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

JOB_TYPE = "ontology-shacl"
DEBOUNCE_SECONDS = 2.0
KEEP_FINISHED_JOBS = 3
POLL_SECONDS = 0.25
LIVE = ("pending", "running")

_loop: asyncio.AbstractEventLoop | None = None
_timers: dict[str, asyncio.TimerHandle] = {}
_ontology_jobs: dict[str, list[str]] = {}
_tasks: set[asyncio.Task[Any]] = set()


def active() -> bool:
    """True when validations run as Pipeline Jobs (started, Pipeline enabled)."""
    return _loop is not None and bool(settings.pipeline_enabled)


def _build_timeout() -> float:
    return float(max(0, int(settings.ontology_shacl_build_timeout_seconds)))


def job_type() -> Any:
    from app.pipeline.registry import JobType, ParamSpec

    return JobType(
        name=JOB_TYPE,
        label="Ontology validation (SHACL)",
        description=(
            "Validate the data of one ontology version against its SHACL shapes and store the "
            "result beside the ontology, for the Checks pane and the assistant. Queued "
            "automatically after every save of an ontology with shapes."
        ),
        runner=run_job,
        params=[ParamSpec(name="ontology_id", label="Ontology id", type="string", required=True)],
        timeout_seconds=_build_timeout,
        max_concurrent=1,
    )


async def _wait_for_entailment(ontology_id: str, config: dict[str, Any], deadline: float | None) -> None:
    """Let the entailment job of this version finish first (no second reasoner run)."""
    from app.ontology import entailment_jobs

    inference = config.get("inference") or "none"
    if inference == "none" or config.get("shapes_graphs") or not entailment_jobs.active():
        return
    with contextlib.suppress(Exception):
        await entailment_jobs.wait_ready(ontology_id, "all", None, inference, deadline)


async def run_job(job: Job, job_store: PipelineStore, cancel_event: asyncio.Event) -> None:
    """The ``ontology-shacl`` runner: one validation in a child process."""
    from datetime import UTC, datetime

    from app.ontology import shacl_results, store
    from app.pipeline.engine import queue
    from app.pipeline.models import JobStatus
    from app.pipeline.process import ProcessCancelled, run_in_process, settings_snapshot

    ontology_id = str(job.params.get("ontology_id") or "")

    def finish(status: Any, error: str | None = None) -> None:
        job.status = status
        job.error = error
        job.completed_at = datetime.now(UTC).isoformat()
        job_store.save(job)

    entry = await asyncio.to_thread(store.get_entry, ontology_id)
    if entry is None:
        finish(JobStatus.failed, f"Ontology not found: {ontology_id}")
        return
    config = store.shacl_config(entry)
    if config is not None:
        job.progress_message = "Waiting for entailment"
        job_store.save(job)
        limit = _build_timeout()
        await _wait_for_entailment(ontology_id, config, time.monotonic() + limit / 2 if limit else None)
    job.progress = 10
    job.progress_message = "Validating"
    job_store.save(job)
    try:
        summary = await run_in_process(
            "app.ontology.shacl_results:build_from_job",
            {"ontology_id": ontology_id},
            cancel_event=cancel_event,
            settings=settings_snapshot(),
        )
    except ProcessCancelled:
        if queue.timed_out(job.id) and config is not None:
            with contextlib.suppress(Exception):
                key = await asyncio.to_thread(shacl_results.key_for, ontology_id, config)
                await asyncio.to_thread(shacl_results.save_timeout, key, limit_seconds=_build_timeout())
            return  # the engine records the job as failed (timeout)
        finish(JobStatus.cancelled)
        return
    except KeyError:
        finish(JobStatus.failed, f"Ontology not found: {ontology_id}")
        return
    except Exception as exc:
        logger.warning("SHACL validation job failed for %s", ontology_id, exc_info=True)
        finish(JobStatus.failed, f"{type(exc).__name__}: {exc}")
        return
    job.progress = 100
    job.progress_message = "Done"
    job.result = {"ontology_id": ontology_id, **summary}
    finish(JobStatus.completed)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(_retain_jobs, ontology_id, job.id)


def _retain_jobs(ontology_id: str, current: str) -> None:
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


async def submit(ontology_id: str, *, reason: str = "save") -> Job:
    from app.pipeline.engine import queue

    job = await queue.submit(JOB_TYPE, {"ontology_id": ontology_id, "reason": reason})
    _ontology_jobs.setdefault(ontology_id, []).append(job.id)
    return job


async def cancel_live(ontology_id: str) -> None:
    from app.pipeline.engine import queue

    for job_id in list(_ontology_jobs.get(ontology_id, [])):
        job = queue.get_status(job_id)
        if job is not None and job.status.value in LIVE:
            with contextlib.suppress(ValueError):
                await queue.cancel(job_id)


def live_job(ontology_id: str) -> Any:
    """The pending or running job of the ontology, or None."""
    from app.pipeline.engine import queue

    for job_id in reversed(_ontology_jobs.get(ontology_id, [])):
        job = queue.get_status(job_id)
        if job is not None and job.status.value in LIVE:
            return job
    return None


def targets_of(ontology_id: str, kind: str) -> list[str]:
    """The ontologies to validate again after ``ontology_id`` was written (blocking)."""
    from app.ontology import entailment_jobs, store

    entries = store.read_catalog()
    configured = {entry["id"] for entry in entries if store.shacl_config(entry) is not None}
    found: list[str] = []
    if ontology_id in configured or kind == "shacl":
        found.append(ontology_id)
    if kind == "shacl":
        return found
    for entry in entries:
        config = store.shacl_config(entry)
        if config is not None and ontology_id in (config.get("shapes_ontologies") or []) and entry["id"] not in found:
            found.append(entry["id"])
    with contextlib.suppress(Exception):
        for importer in entailment_jobs._importers_closure(ontology_id):
            if importer in configured and importer not in found:
                found.append(importer)
    return found


async def _after_write(ontology_id: str, kind: str) -> None:
    from app.ontology import shacl_results, store

    _timers.pop(f"{ontology_id}:{kind}", None)
    try:
        targets = await asyncio.to_thread(targets_of, ontology_id, kind)
    except Exception:
        logger.debug("validation targets of %s could not be listed", ontology_id, exc_info=True)
        targets = []
    for target in targets:
        entry = await asyncio.to_thread(store.get_entry, target)
        if entry is None:
            continue
        await cancel_live(target)
        if store.shacl_config(entry) is None:
            await asyncio.to_thread(shacl_results.prune, target, None)  # validation turned off
            continue
        try:
            await submit(target, reason=kind)
        except Exception:
            logger.warning("could not queue the SHACL validation of %s", target, exc_info=True)


def _spawn(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _schedule(ontology_id: str, kind: str) -> None:
    """On the event loop: debounce a write (a delete revalidates the users of its shapes)."""
    if kind == "delete":
        _spawn(cancel_live(ontology_id))
        kind = "write"  # the users of a deleted shapes ontology report it missing
    slot = f"{ontology_id}:{kind}"
    timer = _timers.pop(slot, None)
    if timer is not None:
        timer.cancel()
    loop = asyncio.get_running_loop()
    _timers[slot] = loop.call_later(DEBOUNCE_SECONDS, lambda: _spawn(_after_write(ontology_id, kind)))


def _on_write(ontology_id: str, kind: str) -> None:
    """``store`` write listener (any thread)."""
    loop = _loop
    if loop is None or not settings.pipeline_enabled:
        return
    with contextlib.suppress(RuntimeError):
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


# ---- State and waiting (UDR-0194 D9, D14) --------------------------------------------


def status(ontology_id: str) -> dict[str, Any]:
    """The validation state of the ontology's current version (blocking: files)."""
    from app.ontology import shacl_results, store

    entry = store.get_entry(ontology_id)
    if entry is None:
        raise KeyError(ontology_id)
    block = entry.get("shacl") if isinstance(entry.get("shacl"), dict) else None
    config = store.shacl_config(entry)
    out: dict[str, Any] = {
        "ontology_id": ontology_id,
        "configured": config is not None,
        "settings": block,
        "state": "not_configured" if config is None else "missing",
        "revision": None,
        "inference": (block or {}).get("inference", "none"),
        "strict": bool((block or {}).get("strict")),
        "shapes": [],
        "conforms": None,
        "counts": None,
        "unsupported": [],
        "groups": [],
        "focus_counts": {},
        "results_count": 0,
        "truncated": False,
        "elapsed_ms": None,
        "built_at": None,
        "job_id": None,
        "message": None,
        "detail": None,
        "precompute": active(),
    }
    if config is None:
        return out
    key = shacl_results.key_for(ontology_id, config)
    out["revision"] = key["revision"]
    out["shapes"] = [
        {"kind": kind, "ref": ref, "revision": revision, "missing": revision is None and kind == "ontology"}
        for kind, ref, revision in key["shapes"]
    ]
    record = shacl_results.lookup(key)
    if record is not None:
        out.update(
            state=record.get("state"),
            shapes=record.get("sources") or out["shapes"],
            conforms=record.get("conforms"),
            counts=record.get("counts"),
            unsupported=record.get("unsupported") or [],
            groups=record.get("groups") or [],
            focus_counts=record.get("focus_counts") or {},
            results_count=record.get("results_count", 0),
            truncated=bool(record.get("truncated")),
            elapsed_ms=(record.get("elapsed_ms") or {}).get("total"),
            built_at=record.get("built_at"),
            message=record.get("message"),
            detail=record.get("detail"),
            data_statements=record.get("data_statements"),
            inferred_statements=record.get("inferred_statements"),
        )
        return out
    job = live_job(ontology_id) if settings.pipeline_enabled else None
    if job is not None:
        out.update(state="queued" if job.status.value == "pending" else "running", job_id=job.id)
    elif scheduled(ontology_id):
        out.update(state="queued")  # a save's validation waits for its debounce
    return out


def scheduled(ontology_id: str) -> bool:
    """True while a write of the ontology waits for its debounce (its job is about to be queued)."""
    return any(slot.split(":", 1)[0] == ontology_id for slot in _timers)


async def wait_ready(ontology_id: str, deadline: float | None) -> dict[str, Any]:
    """The status once a result is stored, submitting a job when none runs (agent lane).

    Returns the last status at ``deadline`` (``state`` queued / running / missing).
    """
    resubmitted = False
    while True:
        current = await asyncio.to_thread(status, ontology_id)
        if current["state"] not in ("missing", "queued", "running"):
            return current
        if current["state"] == "missing" and active() and not resubmitted:
            await cancel_live(ontology_id)
            job = await submit(ontology_id, reason="request")
            current.update(state="queued", job_id=job.id)
            resubmitted = True
        if not active() or (deadline is not None and time.monotonic() >= deadline):
            return current
        await asyncio.sleep(POLL_SECONDS)


__all__ = [
    "DEBOUNCE_SECONDS",
    "JOB_TYPE",
    "active",
    "job_type",
    "run_job",
    "scheduled",
    "start",
    "status",
    "stop",
    "submit",
    "targets_of",
    "wait_ready",
]
