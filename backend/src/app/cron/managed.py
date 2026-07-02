"""Managed internal cron jobs (PRP-0097 task 4).

A *managed* job is created by the system (not a user) and runs a registered INTERNAL
handler (``app.cron.executor.run_internal``) instead of a workspace script. It is marked
``protected`` so the REST API (CTR-0133) and the portal (CTR-0135) refuse to delete it.

The first managed job is the webhook subscription maintenance loop (CAP-010, UDR-0075 D8
as amended): the renewal loop is consolidated INTO the Cron Scheduler so it is visible and
unified there, rather than running on a separate internal scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.cron import store
from app.cron.models import STATE_SCHEDULED
from app.cron.schedule import compute_next_run_at, now_tz


def ensure_managed_internal_job(
    *,
    job_id: str,
    category: str,
    description: str,
    interval_seconds: int,
    internal_action: str,
) -> dict[str, Any]:
    """Create or refresh a managed, protected, interval-scheduled internal cron job.

    Idempotent: a fixed ``job_id`` means a restart refreshes the schedule + metadata
    without duplicating, preserving the operator's enabled flag and run history.
    """
    now_iso = datetime.now(UTC).isoformat()
    schedule = {"type": "interval", "interval_seconds": max(60, int(interval_seconds))}

    existing = store.get_job(job_id)
    if existing is not None:
        existing.update(
            {
                "category": category,
                "description": description,
                "schedule": schedule,
                "repeat": True,
                "kind": "internal",
                "internal_action": internal_action,
                "managed": True,
                "protected": True,
                "updated_at": now_iso,
            }
        )
        if not existing.get("next_run_at"):
            nxt = compute_next_run_at(schedule, after=now_tz())
            existing["next_run_at"] = nxt.isoformat() if nxt else None
        return store.save_job(existing)

    nxt = compute_next_run_at(schedule, after=now_tz())
    record = {
        "id": job_id,
        "category": category,
        "description": description,
        "enabled": True,
        "schedule": schedule,
        "repeat": True,
        "kind": "internal",
        "internal_action": internal_action,
        "managed": True,
        "protected": True,
        "script": {},
        "next_run_at": nxt.isoformat() if nxt else None,
        "last_run_at": None,
        "last_status": None,
        "state": STATE_SCHEDULED,
        "created_by": "system",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    return store.save_job(record)


__all__ = ["ensure_managed_internal_job"]
