"""Batch Processing MCP Server entry point (CTR-0072, CTR-0073, CTR-0074).

Core MCP Server providing asynchronous batch job management.
Run via: python -m app.mcp_batch.server

Tools:  submit_job, list_jobs, get_job, cancel_job, delete_job
Resources: batch://jobs, batch://jobs/{job_id}
UI Resource: ui://batch/dashboard (MCP App View)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from app.mcp_batch.jobs import get_available_types
from app.mcp_batch.queue import JobQueue
from app.mcp_batch.storage import JobStorage

# Load .env to inherit main server config (AZURE_OPENAI_ENDPOINT, etc.)
# Batch MCP Server runs as a separate process via stdio, so it does not
# automatically inherit the main server's environment variables.
# mcp_servers.json env overrides take precedence (already set in os.environ).
load_dotenv(override=False)

BATCH_JOBS_DIR = os.environ.get("BATCH_JOBS_DIR", ".jobs")

storage = JobStorage(BATCH_JOBS_DIR)
queue = JobQueue(storage)

mcp = FastMCP(
    "batch",
    instructions=(
        "Batch Processing Server. Manages asynchronous batch jobs.\n"
        f"Available job types: {', '.join(get_available_types())}.\n"
        "Use submit_job to start jobs, list_jobs to see status, "
        "get_job for details, cancel_job to stop, delete_job to remove."
    ),
)


# ---- MCP Tools ----


@mcp.tool()
async def submit_job(job_type: str, params: dict | None = None) -> str:
    """Submit a new batch job for async execution.

    Args:
        job_type: Type of job to run. Core types: rag-ingest. Sample
            types (e.g. sleep) are registered only when the operator
            sets BATCH_ENABLE_SAMPLE_JOBS=true (PRP-0046).
        params: Job-specific parameters.
            rag-ingest: {"file_path": ".uploads/thread/file.pdf",
                         "collection": "default",
                         "chunk_size": 800, "chunk_overlap": 200,
                         "chunk_min_size": 200}
            sleep (sample): {"duration": 60}

    Returns:
        JSON with job id, type, and status.
    """
    try:
        job = await queue.submit(job_type, params or {})
        return job.model_dump_json(indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


@mcp.tool(
    annotations={
        "_meta": {
            "ui": {
                "resourceUri": "ui://batch/dashboard",
                "visibility": ["model", "app"],
            },
        },
    },
)
async def list_jobs(status: str = "all") -> str:
    """List batch jobs with optional status filter.

    Args:
        status: Filter by status (all/pending/running/completed/failed/cancelled).

    Returns:
        JSON array of job summaries with interactive dashboard.
    """
    jobs = queue.list_jobs(status if status != "all" else None)
    return json.dumps(
        {
            "jobs": [j.model_dump() for j in jobs],
            "total": len(jobs),
            "summary": _build_summary(jobs),
        },
        indent=2,
    )


@mcp.tool()
async def get_job(job_id: str) -> str:
    """Get detailed status and progress of a batch job.

    Args:
        job_id: The job ID (e.g., "job-550e8400").

    Returns:
        JSON with full job details including progress.
    """
    job = queue.get_status(job_id)
    if not job:
        return json.dumps({"error": f"Job not found: {job_id}"})
    return job.model_dump_json(indent=2)


@mcp.tool()
async def cancel_job(job_id: str) -> str:
    """Cancel a running or pending batch job.

    Args:
        job_id: The job ID to cancel.

    Returns:
        JSON with updated job status.
    """
    try:
        job = await queue.cancel(job_id)
        return job.model_dump_json(indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def delete_job(job_id: str) -> str:
    """Delete a completed/failed/cancelled batch job record and file.

    Args:
        job_id: The job ID to delete.

    Returns:
        Confirmation message.
    """
    try:
        queue.delete_job(job_id)
        return json.dumps({"message": f"Job {job_id} deleted"})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ---- MCP Resources ----


@mcp.resource("batch://jobs")
def job_list_resource() -> str:
    """Current job list as structured JSON for dashboard."""
    jobs = queue.list_jobs()
    return json.dumps(
        {
            "jobs": [j.model_dump() for j in jobs],
            "total": len(jobs),
            "summary": _build_summary(jobs),
        },
        indent=2,
    )


@mcp.resource("batch://jobs/{job_id}")
def job_detail_resource(job_id: str) -> str:
    """Detailed job status as structured JSON."""
    job = queue.get_status(job_id)
    if not job:
        return json.dumps({"error": f"Job not found: {job_id}"})
    return job.model_dump_json(indent=2)


# ---- MCP App UI Resource ----


@mcp.resource(
    "ui://batch/dashboard",
    mime_type="text/html;profile=mcp-app",
)
def dashboard_html() -> str:
    """Interactive batch job monitoring dashboard (MCP App View)."""
    html_path = Path(__file__).parent / "ui" / "dashboard.html"
    return html_path.read_text(encoding="utf-8")


# ---- Helpers ----


def _build_summary(jobs: list) -> dict:
    """Build status summary counts."""
    summary: dict[str, int] = {}
    for job in jobs:
        status = job.status if isinstance(job.status, str) else job.status.value
        summary[status] = summary.get(status, 0) + 1
    return summary


if __name__ == "__main__":
    # stdio MCP servers cannot use stdout/stderr for logging because
    # MAF SDK uses them for JSON-RPC communication. Log to a file instead.
    # Log file is placed next to BATCH_JOBS_DIR for easy access.
    import logging

    log_file = Path(BATCH_JOBS_DIR).parent / "batch-server.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
        force=True,
    )
    logging.getLogger(__name__).info("Batch MCP Server starting (log: %s)", log_file)
    mcp.run()
