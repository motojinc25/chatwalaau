"""Pipeline Management API (CTR-0146, PRP-0096, UDR-0074 D5).

REST endpoints for pipeline job management, so the portal UI (CTR-0148) and external
clients can submit/monitor/cancel data-processing jobs without going through the agent:

    GET    /api/pipeline/types               -- available job types + params schema
    GET    /api/pipeline/jobs                -- list jobs (newest first)
    POST   /api/pipeline/jobs                -- submit { type, params }
    GET    /api/pipeline/jobs/{id}           -- get one job (incl. live progress)
    DELETE /api/pipeline/jobs/{id}           -- delete a non-running job
    POST   /api/pipeline/jobs/{id}/cancel    -- cooperative cancel of a running job
    GET    /api/pipeline/jobs/{id}/runs      -- run history (timeline)
    GET    /api/pipeline/runs/{run_id}       -- run detail (meta + captured log)

Every mutating endpoint (POST/DELETE) consumes CTR-0083 (``verify_api_key``); loopback
bypass keeps localhost zero-config. The whole surface returns 404 when PIPELINE_ENABLED
is false, so the SPA can gate the launcher icon by probing the list endpoint
(UDR-0074 D5).

Under DEMO_MODE the three WRITE endpoints refuse with 409 ``demo_mode`` and the portal
renders read-only (PRP-0138 / UDR-0122; same shape as CTR-0174 / UDR-0090 D7). Reads
are deliberately untouched.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.core.config import settings
from app.pipeline import store
from app.pipeline.engine import queue
from app.pipeline.registry import job_types_info

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["Pipeline"])


class PipelineJobCreate(BaseModel):
    """Submit a job of a registered type with type-specific params."""

    type: str = Field(description="A registered job type, e.g. 'rag-ingest'.")
    params: dict = Field(default_factory=dict)


def _require_enabled() -> None:
    if not settings.pipeline_enabled:
        raise HTTPException(status_code=404, detail={"error": "pipeline_disabled"})


def _guard_demo() -> None:
    """Refuse a WRITE under DEMO_MODE (PRP-0138 / UDR-0122; UDR-0090 D7 shape).

    Reads stay open on purpose: a demo that 404s the whole surface looks like a
    product without the feature. What is closed is the single write path, because
    every submitted job lands in the SAME ChromaDB collection the demo corpus is
    seeded into (app.demo.bootstrap), so one visitor's upload becomes another
    visitor's `rag_search` citation. FEAT-0022 already forbids mixing demo and
    live vectors; this makes that a guarantee rather than an operator instruction.
    """
    if settings.demo_mode:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "demo_mode",
                "message": "Submitting, cancelling and deleting pipeline jobs is disabled under DEMO_MODE.",
            },
        )


@router.get("/types", dependencies=[Depends(verify_api_key)])
async def list_types() -> dict:
    """List registered job types and their parameter schema (drives the submit form).

    Carries ``demo_mode`` so the portal can render the read-only notice from the
    SAME payload it already fetches, with no second request and no client-side
    inference (the CTR-0174 / AppSettings pattern).
    """
    _require_enabled()
    return {"types": job_types_info(), "demo_mode": bool(settings.demo_mode)}


@router.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs() -> dict:
    """List all pipeline jobs (newest first). 404 when the feature is disabled."""
    _require_enabled()
    return {"jobs": [j.model_dump() for j in queue.list_jobs()]}


@router.post("/jobs", dependencies=[Depends(verify_api_key)])
async def create_job(body: PipelineJobCreate) -> dict:
    """Submit a job for async execution."""
    _require_enabled()
    _guard_demo()
    try:
        job = await queue.submit(body.type, body.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    logger.info("pipeline job submitted: %s (%s)", job.id, job.type)
    return job.model_dump()


@router.get("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_job(job_id: str) -> dict:
    """Get one job including live progress."""
    _require_enabled()
    job = queue.get_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    return job.model_dump()


@router.delete("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def delete_job(job_id: str) -> dict:
    """Delete a non-running job (run logs are left in place)."""
    _require_enabled()
    _guard_demo()
    try:
        ok = queue.delete_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    return {"deleted": True, "id": job_id}


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str) -> dict:
    """Cooperatively cancel a running or pending job."""
    _require_enabled()
    _guard_demo()
    try:
        job = await queue.cancel(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
    return job.model_dump()


@router.get("/jobs/{job_id}/runs", dependencies=[Depends(verify_api_key)])
async def list_runs(job_id: str) -> dict:
    """Return a job's run history (newest first) for the timeline view."""
    _require_enabled()
    return {"runs": store.list_runs(job_id)}


@router.get("/runs/{run_id:path}", dependencies=[Depends(verify_api_key)])
async def get_run(run_id: str) -> dict:
    """Return a run's detail: meta + captured log."""
    _require_enabled()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    return run


__all__ = ["router"]
