# Job type registry
"""Batch MCP Server job type registry (CTR-0073, PRP-0046).

``rag-ingest`` is always available. ``sleep`` is a Phase-1 sample job
kept for integration tests and documentation; it is gated behind
``BATCH_ENABLE_SAMPLE_JOBS`` (default ``false``) so operators running
in production do not see an unused tool type.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from app.mcp_batch.models import Job
    from app.mcp_batch.storage import JobStorage

from app.mcp_batch.jobs.rag_ingest import run_rag_ingest_job
from app.mcp_batch.jobs.sleep import run_sleep_job


def _sample_jobs_enabled() -> bool:
    """Return True when BATCH_ENABLE_SAMPLE_JOBS is truthy in the env."""
    raw = os.environ.get("BATCH_ENABLE_SAMPLE_JOBS", "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Map job_type string -> async job function. The sleep job is only
# registered when BATCH_ENABLE_SAMPLE_JOBS is enabled.
JOB_REGISTRY: dict[str, object] = {
    "rag-ingest": run_rag_ingest_job,
}
if _sample_jobs_enabled():
    JOB_REGISTRY["sleep"] = run_sleep_job


def get_available_types() -> list[str]:
    """Return list of registered job type names."""
    return list(JOB_REGISTRY.keys())
