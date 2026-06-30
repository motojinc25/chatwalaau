"""Pipeline Job Store: per-job JSON records + per-run execution logs (CTR-0145, PRP-0096).

The store is the SSOT (UDR-0074 D4). One JSON file per job in PIPELINE_JOBS_DIR
(default ".pipeline"); each execution writes a nested run record under
``PIPELINE_JOBS_DIR/output/{job_id}/{run}/`` (meta.json + log.txt, the log byte-capped
by PIPELINE_OUTPUT_MAX_BYTES). This mirrors the Cron Job Store (CTR-0131) and closes
the gap where the old batch storage kept only the final result with no run history.

``PipelineStore`` keeps the job-CRUD surface the rag-ingest runner already calls
(``store.save(job)``); the run-history functions are module-level and used by the
engine (CTR-0073), not by the job runners.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.pipeline.models import Job

logger = logging.getLogger(__name__)

_OUTPUT_DIRNAME = "output"


def jobs_dir() -> Path:
    """Return the configured pipeline jobs directory, created if missing."""
    d = Path(settings.pipeline_jobs_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    """Return the run-log output root (nested under the jobs dir)."""
    d = jobs_dir() / _OUTPUT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class PipelineStore:
    """File-based store for pipeline jobs. One JSON file per job."""

    def _job_path(self, job_id: str) -> Path:
        return jobs_dir() / f"{job_id}.json"

    def save(self, job: Job) -> None:
        """Save or update a job file."""
        self._job_path(job.id).write_text(job.model_dump_json(indent=2), encoding="utf-8")

    def load(self, job_id: str) -> Job | None:
        """Load a job by id. Returns None if missing/corrupt."""
        path = self._job_path(job_id)
        if not path.is_file():
            return None
        try:
            return Job.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse pipeline job file: %s", path)
            return None

    def list_all(self, status: str | None = None) -> list[Job]:
        """List all jobs, newest first, optionally filtered by status."""
        jobs: list[Job] = []
        for path in jobs_dir().glob("job-*.json"):
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Skipping malformed pipeline job file: %s", path)
                continue
            if status and status != "all" and job.status.value != status:
                continue
            jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def delete(self, job_id: str) -> bool:
        """Delete a job file. Returns True if it existed."""
        path = self._job_path(job_id)
        if not path.is_file():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            logger.warning("Failed to delete pipeline job file: %s", path)
            return False


# Module-level singleton shared by the engine (CTR-0073) and the router (CTR-0146).
store = PipelineStore()


# ---------------------------------------------------------------------------
# Run history (CTR-0145 run-log record) -- used by the engine, mirrors CTR-0131.
# ---------------------------------------------------------------------------


def start_run(job: Job) -> tuple[str, Path]:
    """Create a run directory + initial meta.json. Returns (run_id, run_dir).

    ``run_id`` is the relative id ``"{job_id}/{folder}"`` so the detail endpoint
    (GET /api/pipeline/runs/{run_id}) can resolve it without scanning.
    """
    started = datetime.now(UTC)
    folder = f"{started.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:6]}"
    run_dir = output_dir() / job.id / folder
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{job.id}/{folder}"
    meta = {
        "run_id": run_id,
        "job_id": job.id,
        "job_type": job.type,
        "started_at": started.isoformat(),
        "finished_at": None,
        "status": "running",
        "progress": job.progress,
        "duration_ms": None,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_id, run_dir


def finish_run(run_dir: Path, job: Job, *, duration_ms: int | None) -> None:
    """Finalize a run: write a captured log from the job's final state + update meta.json."""
    cap = max(0, int(settings.pipeline_output_max_bytes))
    log = _build_log(job)
    (run_dir / "log.txt").write_text(_cap(log, cap), encoding="utf-8")
    meta_path = run_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        meta = {}
    meta.update(
        {
            "finished_at": datetime.now(UTC).isoformat(),
            "status": job.status.value,
            "progress": job.progress,
            "duration_ms": duration_ms,
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_log(job: Job) -> str:
    """Compose a human-readable run log from the job's terminal state."""
    lines = [
        f"job_id:   {job.id}",
        f"type:     {job.type}",
        f"status:   {job.status.value}",
        f"progress: {job.progress}%",
        f"message:  {job.progress_message}",
        "",
        "params:",
        json.dumps(job.params, ensure_ascii=False, indent=2),
    ]
    if job.result is not None:
        lines += ["", "result:", json.dumps(job.result, ensure_ascii=False, indent=2)]
    if job.error:
        lines += ["", "error:", job.error]
    return "\n".join(lines)


def _cap(text: str, cap: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if cap <= 0 or len(encoded) <= cap:
        return text
    clipped = encoded[:cap].decode("utf-8", errors="ignore")
    return f"{clipped}\n... (truncated at {cap} bytes)"


def list_runs(job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent run meta records for a job, newest first."""
    base = output_dir() / job_id
    if not base.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for meta_path in base.glob("*/meta.json"):
        try:
            runs.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs[:limit]


def get_run(run_id: str) -> dict[str, Any] | None:
    """Return a run's meta + captured log, or None.

    ``run_id`` is the "{job_id}/{folder}" relative id; it is jailed under the output
    directory (a traversal attempt resolves outside and is rejected).
    """
    out_root = output_dir().resolve()
    run_dir = (out_root / run_id).resolve()
    if out_root not in run_dir.parents:
        return None
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    log = ""
    log_path = run_dir / "log.txt"
    if log_path.is_file():
        try:
            log = log_path.read_text(encoding="utf-8")
        except OSError:
            log = ""
    return {**meta, "log": log}


__all__ = [
    "PipelineStore",
    "finish_run",
    "get_run",
    "jobs_dir",
    "list_runs",
    "output_dir",
    "start_run",
    "store",
]
