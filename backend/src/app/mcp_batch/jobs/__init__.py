"""Batch MCP Server job type registry (CTR-0073, PRP-0060).

The registry maps a ``job_type`` string to its async runner function.
``rag-ingest`` is the only registered job type. Operators add new
types by importing the runner here and inserting into ``JOB_REGISTRY``.
"""

from __future__ import annotations

from app.mcp_batch.jobs.rag_ingest import run_rag_ingest_job

JOB_REGISTRY: dict[str, object] = {
    "rag-ingest": run_rag_ingest_job,
}


def get_available_types() -> list[str]:
    """Return list of registered job type names."""
    return list(JOB_REGISTRY.keys())
