"""Job queue with asyncio task-based workers (CTR-0073).

Manages job lifecycle: submit -> run -> complete/fail/cancel.
Cooperative cancellation via asyncio.Event checked at each progress update.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING
import uuid

from app.mcp_batch.jobs import JOB_REGISTRY, get_available_types
from app.mcp_batch.models import Job, JobStatus

if TYPE_CHECKING:
    from app.mcp_batch.storage import JobStorage

logger = logging.getLogger(__name__)


class JobQueue:
    """Asyncio-based job queue with file-backed persistence."""

    def __init__(self, storage: JobStorage) -> None:
        self._storage = storage
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def submit(self, job_type: str, params: dict | None = None) -> Job:
        """Submit a new job for async execution."""
        if job_type not in JOB_REGISTRY:
            available = ", ".join(get_available_types())
            msg = f"Unknown job type '{job_type}'. Available: {available}"
            raise ValueError(msg)

        job = Job(
            id=f"job-{uuid.uuid4().hex[:8]}",
            type=job_type,
            params=params or {},
            created_at=datetime.now(UTC).isoformat(),
        )
        self._storage.save(job)

        cancel_event = asyncio.Event()
        self._cancel_events[job.id] = cancel_event

        task = asyncio.create_task(self._run_job(job, cancel_event))
        self._running_tasks[job.id] = task

        return job

    async def _run_job(self, job: Job, cancel_event: asyncio.Event) -> None:
        """Execute a job function and handle lifecycle transitions."""
        job.status = JobStatus.running
        job.started_at = datetime.now(UTC).isoformat()
        self._storage.save(job)

        job_fn = JOB_REGISTRY[job.type]
        try:
            await job_fn(job, self._storage, cancel_event)
        except Exception:
            logger.exception("Job %s failed", job.id)
            job.status = JobStatus.failed
            job.error = "Job execution failed unexpectedly"
            job.completed_at = datetime.now(UTC).isoformat()
            self._storage.save(job)
        finally:
            self._running_tasks.pop(job.id, None)
            self._cancel_events.pop(job.id, None)

    async def cancel(self, job_id: str) -> Job:
        """Request cancellation of a running or pending job."""
        job = self._storage.load(job_id)
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
                    await asyncio.wait_for(task, timeout=10)
                except TimeoutError:
                    logger.warning("Job %s cancel timed out", job_id)
        else:
            job.status = JobStatus.cancelled
            job.completed_at = datetime.now(UTC).isoformat()
            self._storage.save(job)

        return self._storage.load(job_id) or job

    def get_status(self, job_id: str) -> Job | None:
        """Get current job status from persistent storage."""
        return self._storage.load(job_id)

    def list_jobs(self, status: str | None = None) -> list[Job]:
        """List all jobs, optionally filtered by status."""
        return self._storage.list_all(status)

    def delete_job(self, job_id: str) -> bool:
        """Delete a non-running job record and file."""
        job = self._storage.load(job_id)
        if not job:
            msg = f"Job not found: {job_id}"
            raise ValueError(msg)

        if job.status == JobStatus.running:
            msg = "Cannot delete a running job. Cancel it first."
            raise ValueError(msg)

        return self._storage.delete(job_id)
