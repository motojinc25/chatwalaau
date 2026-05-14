"""Sample sleep job for Phase 1 batch processing infrastructure validation.

Sleeps for a configurable duration with progress updates every 5 seconds.
Supports cooperative cancellation via asyncio.Event.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.mcp_batch.models import Job, JobStatus

if TYPE_CHECKING:
    from app.mcp_batch.storage import JobStorage


async def run_sleep_job(
    job: Job,
    storage: JobStorage,
    cancel_event: asyncio.Event,
) -> None:
    """Sleep for `duration` seconds with periodic progress updates.

    Args:
        job: The job record to update.
        storage: Persistence layer for saving progress.
        cancel_event: Set by the queue to request cancellation.
    """
    duration: int = job.params.get("duration", 60)
    interval = 5
    elapsed = 0

    while elapsed < duration:
        if cancel_event.is_set():
            job.status = JobStatus.cancelled
            job.completed_at = datetime.now(UTC).isoformat()
            storage.save(job)
            return

        job.progress = min(int(elapsed / duration * 100), 99)
        job.progress_message = f"{elapsed}s / {duration}s elapsed"
        storage.save(job)

        wait = min(interval, duration - elapsed)
        await asyncio.sleep(wait)
        elapsed += wait

    job.status = JobStatus.completed
    job.progress = 100
    job.progress_message = f"Completed ({duration}s)"
    job.result = {"duration": duration, "message": "Sleep job finished successfully"}
    job.completed_at = datetime.now(UTC).isoformat()
    storage.save(job)
