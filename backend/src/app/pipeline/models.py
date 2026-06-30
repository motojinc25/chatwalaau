"""Job data model for the Pipeline Job Engine (CTR-0073, PRP-0096)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    """Job lifecycle states."""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Job(BaseModel):
    """Pipeline job record persisted as an individual JSON file."""

    id: str
    type: str
    status: JobStatus = JobStatus.pending
    progress: int = Field(default=0, ge=0, le=100)
    progress_message: str = ""
    params: dict = Field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    # PRP-0096 (CTR-0145): id of the latest run-history record for this job, so the
    # SPA can deep-link to the run detail (run logs live under output/{job_id}/{run}/).
    last_run_id: str | None = None
