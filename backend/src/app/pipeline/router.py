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


@router.get("/types", dependencies=[Depends(verify_api_key)])
async def list_types() -> dict:
    """List registered job types and their parameter schema (drives the submit form)."""
    _require_enabled()
    return {"types": job_types_info()}


@router.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs() -> dict:
    """List all pipeline jobs (newest first). 404 when the feature is disabled."""
    _require_enabled()
    return {"jobs": [j.model_dump() for j in queue.list_jobs()]}


@router.post("/jobs", dependencies=[Depends(verify_api_key)])
async def create_job(body: PipelineJobCreate) -> dict:
    """Submit a job for async execution."""
    _require_enabled()
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
