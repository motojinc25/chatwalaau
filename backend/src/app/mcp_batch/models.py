"""Job data model for Batch Processing MCP Server (CTR-0073)."""

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
    """Batch job record persisted as individual JSON file."""

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
