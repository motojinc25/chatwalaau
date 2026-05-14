"""File-based job persistence for Batch Processing MCP Server (CTR-0073).

Each job is stored as an individual JSON file: {BATCH_JOBS_DIR}/job-{uuid}.json
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.mcp_batch.models import Job

logger = logging.getLogger(__name__)


class JobStorage:
    """File-based storage for batch jobs. One JSON file per job."""

    def __init__(self, jobs_dir: str) -> None:
        self._dir = Path(jobs_dir)

    def ensure_dir(self) -> None:
        """Create jobs directory if it does not exist."""
        self._dir.mkdir(parents=True, exist_ok=True)

    def _job_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.json"

    def save(self, job: Job) -> None:
        """Save or update a job file."""
        self.ensure_dir()
        self._job_path(job.id).write_text(
            job.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def load(self, job_id: str) -> Job | None:
        """Load a job by ID. Returns None if not found."""
        path = self._job_path(job_id)
        if not path.is_file():
            return None
        try:
            return Job.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse job file: %s", path)
            return None

    def list_all(self, status: str | None = None) -> list[Job]:
        """List all jobs, optionally filtered by status."""
        self.ensure_dir()
        jobs: list[Job] = []
        for path in sorted(self._dir.glob("job-*.json")):
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
                if status and status != "all" and job.status.value != status:
                    continue
                jobs.append(job)
            except Exception:
                logger.warning("Skipping malformed job file: %s", path)
        return jobs

    def delete(self, job_id: str) -> bool:
        """Delete a job file. Returns True if deleted."""
        path = self._job_path(job_id)
        if path.is_file():
            path.unlink()
            return True
        return False
