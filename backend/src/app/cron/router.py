"""Cron Management API (CTR-0133, PRP-0089, UDR-0067).

REST endpoints for job CRUD plus run history / run detail, so the portal UI
(CTR-0135) and external clients can manage the scheduler:

    GET    /api/cron/jobs            -- list jobs (with computed schedule + state)
    POST   /api/cron/jobs            -- create a job (computes next_run_at)
    GET    /api/cron/jobs/{id}       -- get one job
    PUT    /api/cron/jobs/{id}       -- update a job (recomputes next_run_at)
    DELETE /api/cron/jobs/{id}       -- delete a job
    GET    /api/cron/jobs/{id}/runs  -- a job's run history (timeline)
    GET    /api/cron/runs/{run_id}   -- run detail (meta + stdout/stderr)

Every mutating endpoint (POST/PUT/DELETE) consumes CTR-0083 (``verify_api_key``);
loopback bypass keeps localhost zero-config. The whole surface returns 404 when
CRON_ENABLED is false, so the SPA can gate the launcher icon by probing the list
endpoint (UDR-0067 D10).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.auth import verify_api_key
from app.core.config import settings
from app.cron import store
from app.cron.models import CronJobCreate, CronJobUpdate, apply_update, build_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["Cron"])


def _require_enabled() -> None:
    if not settings.cron_enabled:
        raise HTTPException(status_code=404, detail={"error": "cron_disabled"})


@router.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs() -> dict:
    """List all cron jobs. 404 when the feature is disabled."""
    _require_enabled()
    return {"jobs": store.list_jobs()}


@router.post("/jobs", dependencies=[Depends(verify_api_key)])
async def create_job(body: CronJobCreate) -> dict:
    """Create a job (created_by=user, enabled per request) with a computed next_run_at."""
    _require_enabled()
    record = build_record(body, created_by="user")
    store.save_job(record)
    logger.info("cron job created: %s (%s)", record["id"], record["schedule"].get("type"))
    return record


@router.get("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_job(job_id: str) -> dict:
    """Get one job."""
    _require_enabled()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    return job


@router.put("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def update_job(job_id: str, body: CronJobUpdate) -> dict:
    """Update a job; recomputes next_run_at when the schedule or enabled changes."""
    _require_enabled()
    existing = store.get_job(job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    updated = apply_update(existing, body)
    store.save_job(updated)
    return updated


@router.delete("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def delete_job(job_id: str) -> dict:
    """Delete a job (run logs are left in place)."""
    _require_enabled()
    ok = store.delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    return {"deleted": True, "id": job_id}


@router.get("/jobs/{job_id}/runs", dependencies=[Depends(verify_api_key)])
async def list_runs(job_id: str) -> dict:
    """Return a job's run history (newest first) for the timeline view."""
    _require_enabled()
    return {"runs": store.list_runs(job_id)}


@router.get("/runs/{run_id:path}", dependencies=[Depends(verify_api_key)])
async def get_run(run_id: str) -> dict:
    """Return a run's detail: meta + captured stdout/stderr."""
    _require_enabled()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    return run


__all__ = ["router"]
