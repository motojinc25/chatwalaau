"""manage_cron MAF Function Tool (CTR-0134 / UDR-0067 D7).

A single agent tool that lets the LLM create / update / delete / list cron jobs in
natural language ("run this script in 30 minutes", "every weekday at 9am"). It
writes through the SAME store + validation path as the REST API (CTR-0133), stamps
``created_by="llm"``, and -- per the operator decision -- creates jobs ENABLED by
default (UDR-0067 D7).

Registered on the shared agent only when CRON_ENABLED (agent_factory). It performs
no subprocess execution itself; the tick engine (CTR-0130) runs jobs later, and the
workspace jail + CODING_ENABLED gate are enforced at run time by CTR-0132.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Annotated

from pydantic import Field, ValidationError

from app.cron import store
from app.cron.models import CronJobCreate, CronJobUpdate, ScheduleSpec, ScriptSpec, apply_update, build_record
from app.cron.schedule import now_tz

logger = logging.getLogger(__name__)

CRON_TOOL_INSTRUCTION = (
    "\n\n## Cron Scheduler\n"
    "You can schedule SCRIPTS (files in the coding workspace) to run later via the "
    "manage_cron tool. Use it when the user asks to run something on a schedule, after "
    "a delay, or repeatedly. Three schedule shapes: a one-shot delay (run once after N "
    "minutes), a recurring interval (every N seconds), or a crontab expression (5+ "
    "fields). Scripts run only inside the workspace and only when coding is enabled. "
    "Confirm the script path with the user before creating a job."
)


def _summary(job: dict) -> dict:
    """A compact job view for tool results (avoid dumping the whole record)."""
    return {
        "id": job.get("id"),
        "category": job.get("category"),
        "description": job.get("description"),
        "enabled": job.get("enabled"),
        "schedule": job.get("schedule"),
        "next_run_at": job.get("next_run_at"),
        "state": job.get("state"),
    }


async def manage_cron(
    action: Annotated[str, Field(description="One of: create, update, delete, list.")],
    description: Annotated[str, Field(description="Human-readable summary of what the job does.")] = "",
    category: Annotated[str, Field(description="Optional category label for grouping.")] = "",
    schedule_type: Annotated[
        str,
        Field(description="For create/update: one of cron, interval, oneshot."),
    ] = "",
    cron_expr: Annotated[str, Field(description="For schedule_type=cron: a 5+ field crontab expression.")] = "",
    interval_seconds: Annotated[int, Field(description="For schedule_type=interval: seconds between runs.")] = 0,
    delay_minutes: Annotated[
        int,
        Field(description="For schedule_type=oneshot: minutes from now to run once (e.g. 30)."),
    ] = 0,
    script_path: Annotated[str, Field(description="Path to the script, relative to the coding workspace.")] = "",
    script_interpreter: Annotated[
        str,
        Field(description="Optional interpreter (e.g. python, sh, pwsh, node). Empty = by file extension."),
    ] = "",
    job_id: Annotated[str, Field(description="For update/delete: the target job id.")] = "",
) -> str:
    """Create, update, delete, or list scheduled script jobs (cron).

    For action="create" provide schedule_type plus the matching fields and a
    script_path. For a one-shot use schedule_type="oneshot" with delay_minutes.
    Jobs are created enabled and run via the in-process scheduler. Never store
    secrets in the job description.
    """
    act = (action or "").strip().lower()

    if act == "list":
        jobs = [_summary(j) for j in store.list_jobs()]
        return json.dumps({"jobs": jobs}, ensure_ascii=False)

    if act == "delete":
        if not job_id:
            return "Error: 'job_id' is required for delete."
        ok = store.delete_job(job_id)
        return json.dumps({"deleted": ok, "id": job_id})

    if act in {"create", "update"}:
        schedule = _build_schedule(schedule_type, cron_expr, interval_seconds, delay_minutes)
        if isinstance(schedule, str):
            return schedule  # an error message

        if act == "create":
            if not script_path:
                return "Error: 'script_path' is required for create."
            try:
                payload = CronJobCreate(
                    category=category,
                    description=description,
                    enabled=True,  # LLM-created jobs default enabled (UDR-0067 D7)
                    schedule=schedule,
                    script=ScriptSpec(path=script_path, interpreter=script_interpreter),
                )
            except ValidationError as exc:
                return f"Error: invalid job: {exc.errors()[0].get('msg', 'validation failed')}"
            record = build_record(payload, created_by="llm")
            store.save_job(record)
            logger.info("manage_cron created job %s (%s)", record["id"], record["schedule"].get("type"))
            return json.dumps({"created": _summary(record)}, ensure_ascii=False)

        # update
        if not job_id:
            return "Error: 'job_id' is required for update."
        existing = store.get_job(job_id)
        if existing is None:
            return f"Error: no cron job with id {job_id}."
        try:
            update = CronJobUpdate(
                category=category or None,
                description=description or None,
                schedule=schedule if schedule_type else None,
                script=ScriptSpec(path=script_path, interpreter=script_interpreter) if script_path else None,
            )
        except ValidationError as exc:
            return f"Error: invalid update: {exc.errors()[0].get('msg', 'validation failed')}"
        updated = apply_update(existing, update)
        store.save_job(updated)
        return json.dumps({"updated": _summary(updated)}, ensure_ascii=False)

    return "Error: 'action' must be one of create, update, delete, list."


def _build_schedule(
    schedule_type: str,
    cron_expr: str,
    interval_seconds: int,
    delay_minutes: int,
) -> ScheduleSpec | str:
    """Build a ScheduleSpec from the flat tool args, or return an error string."""
    kind = (schedule_type or "").strip().lower()
    if not kind:
        return "Error: 'schedule_type' is required (cron, interval, or oneshot)."
    try:
        if kind == "cron":
            return ScheduleSpec(type="cron", expr=cron_expr)
        if kind == "interval":
            return ScheduleSpec(type="interval", interval_seconds=interval_seconds)
        if kind == "oneshot":
            if delay_minutes <= 0:
                return "Error: oneshot requires delay_minutes > 0."
            run_at = (now_tz() + timedelta(minutes=delay_minutes)).isoformat()
            return ScheduleSpec(type="oneshot", run_at=run_at)
    except ValidationError as exc:
        return f"Error: invalid schedule: {exc.errors()[0].get('msg', 'validation failed')}"
    return "Error: 'schedule_type' must be cron, interval, or oneshot."


__all__ = ["CRON_TOOL_INSTRUCTION", "manage_cron"]
