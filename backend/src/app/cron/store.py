"""Cron Job Store: per-file JSON records + per-run execution logs (CTR-0131).

The job store is the SSOT (UDR-0067 D2): one JSON file per job in CRON_JOBS_DIR
(default ".cron"), machine-writable with no comment round-trip problem. Each run
writes a nested log set under ``CRON_JOBS_DIR/output/{job_id}/{run}/``
(meta.json + stdout.log + stderr.log, each byte-capped by CRON_OUTPUT_MAX_BYTES).

The reader is tolerant: a malformed/unreadable job file is skipped (never aborts a
tick or a list call). No default jobs ship.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import settings

logger = logging.getLogger(__name__)

_OUTPUT_DIRNAME = "output"
_LOCK_FILENAME = ".tick.lock"


def jobs_dir() -> Path:
    """Return the configured jobs directory, created if missing."""
    d = Path(settings.cron_jobs_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    """Return the run-log output root (nested under the jobs dir)."""
    d = jobs_dir() / _OUTPUT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def lock_path() -> Path:
    """Path of the single-flight tick lock (CTR-0130 D5)."""
    return jobs_dir() / _LOCK_FILENAME


def _job_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


def list_jobs() -> list[dict[str, Any]]:
    """Return all valid job records (malformed files skipped), newest first."""
    jobs: list[dict[str, Any]] = []
    for path in jobs_dir().glob("cron_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id"):
                jobs.append(data)
        except (json.JSONDecodeError, OSError):
            logger.warning("Skipping malformed cron job file: %s", path)
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return one job record, or None if missing/corrupt."""
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt cron job file: %s", path)
        return None
    return data if isinstance(data, dict) else None


def save_job(record: dict[str, Any]) -> dict[str, Any]:
    """Write a job record (create or overwrite) and return it."""
    path = _job_path(record["id"])
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def delete_job(job_id: str) -> bool:
    """Delete a job file. Returns True if it existed. Run logs are left in place."""
    path = _job_path(job_id)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        logger.warning("Failed to delete cron job file: %s", path)
        return False


# ---------------------------------------------------------------------------
# Run logs (CTR-0131 run-log record)
# ---------------------------------------------------------------------------


def start_run(job: dict[str, Any], *, fast_forwarded: bool = False) -> tuple[str, Path]:
    """Create a run directory + initial meta.json. Returns (run_id, run_dir).

    ``run_id`` is the relative path id ``"{job_id}/{folder}"`` so the detail
    endpoint (GET /api/cron/runs/{run_id}) can resolve it without scanning.
    """
    job_id = job["id"]
    started = datetime.now(UTC)
    folder = f"{started.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:6]}"
    run_dir = output_dir() / job_id / folder
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{job_id}/{folder}"
    meta = {
        "run_id": run_id,
        "job_id": job_id,
        "started_at": started.isoformat(),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
        "duration_ms": None,
        "interpreter": "",
        "script": (job.get("script") or {}).get("path", ""),
        "fast_forwarded": fast_forwarded,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_id, run_dir


def finish_run(
    run_dir: Path,
    *,
    status: str,
    exit_code: int | None,
    duration_ms: int | None,
    interpreter: str,
    stdout: str,
    stderr: str,
) -> None:
    """Finalize a run: write capped stdout/stderr + update meta.json."""
    cap = max(0, int(settings.cron_output_max_bytes))
    (run_dir / "stdout.log").write_text(_cap(stdout, cap), encoding="utf-8")
    (run_dir / "stderr.log").write_text(_cap(stderr, cap), encoding="utf-8")
    meta_path = run_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        meta = {}
    meta.update(
        {
            "finished_at": datetime.now(UTC).isoformat(),
            "status": status,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "interpreter": interpreter,
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


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
    """Return a run's meta + captured output, or None.

    ``run_id`` is the "{job_id}/{folder}" relative id; it is jailed under the
    output directory (a traversal attempt resolves outside and is rejected).
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
    stdout = _read_opt(run_dir / "stdout.log")
    stderr = _read_opt(run_dir / "stderr.log")
    return {**meta, "stdout": stdout, "stderr": stderr}


def _read_opt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


__all__ = [
    "delete_job",
    "finish_run",
    "get_job",
    "get_run",
    "jobs_dir",
    "list_jobs",
    "list_runs",
    "lock_path",
    "output_dir",
    "save_job",
    "start_run",
]
