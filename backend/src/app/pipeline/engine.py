"""Pipeline Job Engine: in-process asyncio queue + worker pool (CTR-0073, PRP-0096).

Relocated and reshaped from the former app.mcp_batch.queue (UDR-0074 D1/D3): the
queue now runs IN the main backend process (no MCP stdio subprocess), is bounded by
PIPELINE_MAX_CONCURRENT_JOBS via an asyncio.Semaphore, dispatches by job type through
the registry (CTR-0073), and wraps each execution in a run-history record (CTR-0145).
Cooperative cancellation via asyncio.Event is preserved byte-for-byte from the batch
queue. Blocking job stages (e.g. PyMuPDF parsing) run via asyncio.to_thread inside the
runners (UDR-0074 D10).

Lifecycle is owned by the FastAPI lifespan (start_pipeline / stop_pipeline), mirroring
the cron tick engine.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import time
import uuid

from app.core.config import settings
from app.pipeline import store as store_mod
from app.pipeline.models import Job, JobStatus
from app.pipeline.registry import get_available_types, get_runner
from app.pipeline.store import store

logger = logging.getLogger(__name__)


class JobQueue:
    """Asyncio-based job queue with file-backed persistence and bounded concurrency."""

    def __init__(self) -> None:
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        # Created lazily on the running loop. Bound from settings (>=1).
        if self._sem is None:
            limit = max(1, int(settings.pipeline_max_concurrent_jobs))
            self._sem = asyncio.Semaphore(limit)
        return self._sem

    async def submit(self, job_type: str, params: dict | None = None) -> Job:
        """Submit a new job. Raises ValueError on unknown type."""
        if get_runner(job_type) is None:
            available = ", ".join(get_available_types())
            msg = f"Unknown job type '{job_type}'. Available: {available}"
            raise ValueError(msg)

        job = Job(
            id=f"job-{uuid.uuid4().hex[:8]}",
            type=job_type,
            params=params or {},
            created_at=datetime.now(UTC).isoformat(),
        )
        store.save(job)

        cancel_event = asyncio.Event()
        self._cancel_events[job.id] = cancel_event
        task = asyncio.create_task(self._run_job(job, cancel_event))
        self._running_tasks[job.id] = task
        return job

    async def _run_job(self, job: Job, cancel_event: asyncio.Event) -> None:
        """Execute one job under the concurrency semaphore with a run-history record."""
        runner = get_runner(job.type)
        if runner is None:  # defensive; submit() already validated
            return
        run_dir = None
        started = time.monotonic()
        try:
            async with self._semaphore():
                if cancel_event.is_set():
                    job.status = JobStatus.cancelled
                    job.completed_at = datetime.now(UTC).isoformat()
                    store.save(job)
                    return
                job.status = JobStatus.running
                job.started_at = datetime.now(UTC).isoformat()
                store.save(job)
                run_id, run_dir = store_mod.start_run(job)
                job.last_run_id = run_id
                store.save(job)
                await runner(job, store, cancel_event)
        except Exception:
            logger.exception("Pipeline job %s failed", job.id)
            job.status = JobStatus.failed
            job.error = "Job execution failed unexpectedly"
            job.completed_at = datetime.now(UTC).isoformat()
            store.save(job)
        finally:
            if run_dir is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
                # Reload terminal state the runner persisted, so the run log/meta match.
                final = store.load(job.id) or job
                try:
                    store_mod.finish_run(run_dir, final, duration_ms=duration_ms)
                except Exception:
                    logger.warning("Failed to finalize run for job %s", job.id, exc_info=True)
            self._running_tasks.pop(job.id, None)
            self._cancel_events.pop(job.id, None)

    async def cancel(self, job_id: str) -> Job:
        """Request cancellation of a running or pending job."""
        job = store.load(job_id)
        if not job:
            msg = f"Job not found: {job_id}"
            raise ValueError(msg)
        if job.status not in (JobStatus.running, JobStatus.pending):
            msg = f"Cannot cancel job in '{job.status.value}' state"
            raise ValueError(msg)

        cancel_event = self._cancel_events.get(job_id)
        if cancel_event:
            cancel_event.set()
            task = self._running_tasks.get(job_id)
            if task:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10)
                except TimeoutError:
                    logger.warning("Pipeline job %s cancel timed out", job_id)
        else:
            job.status = JobStatus.cancelled
            job.completed_at = datetime.now(UTC).isoformat()
            store.save(job)
        return store.load(job_id) or job

    def get_status(self, job_id: str) -> Job | None:
        return store.load(job_id)

    def list_jobs(self, status: str | None = None) -> list[Job]:
        return store.list_all(status)

    def delete_job(self, job_id: str) -> bool:
        """Delete a non-running job record. Run logs are left in place."""
        job = store.load(job_id)
        if not job:
            msg = f"Job not found: {job_id}"
            raise ValueError(msg)
        if job.status == JobStatus.running:
            msg = "Cannot delete a running job. Cancel it first."
            raise ValueError(msg)
        return store.delete(job_id)

    async def drain(self, timeout: float = 5.0) -> None:
        """Cancel in-flight jobs and wait briefly (shutdown)."""
        for event in list(self._cancel_events.values()):
            event.set()
        tasks = list(self._running_tasks.values())
        if not tasks:
            return
        _, still = await asyncio.wait(tasks, timeout=timeout)
        for t in still:
            t.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)


# Module-level singleton shared by the router (CTR-0146) and the agent tool (CTR-0147).
queue = JobQueue()


def start_pipeline() -> None:
    """Initialize the pipeline subsystem at FastAPI lifespan startup (idempotent)."""
    store_mod.jobs_dir()  # ensure the storage dir exists
    logger.info(
        "Pipeline engine started (dir=%s, max_concurrent=%d, types=%s)",
        settings.pipeline_jobs_dir,
        max(1, int(settings.pipeline_max_concurrent_jobs)),
        ", ".join(get_available_types()),
    )


async def stop_pipeline(timeout: float = 5.0) -> None:
    """Drain in-flight pipeline jobs at shutdown (best-effort)."""
    await queue.drain(timeout=timeout)


__all__ = ["JobQueue", "queue", "start_pipeline", "stop_pipeline"]
