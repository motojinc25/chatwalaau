"""Cron job request models + record construction (CTR-0131 / CTR-0133 / CTR-0134).

Pydantic models validate the untrusted input from the REST API (CTR-0133) and the
``manage_cron`` agent tool (CTR-0134). The persisted record is a plain dict (the
project convention for machine-managed JSON stores -- sessions / templates), built
here so the API and the tool share one validation + construction path.

Job record schema and states are defined by CTR-0131 / UDR-0067 D3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.cron.schedule import compute_next_run_at, now_tz, validate_cron_expr

# Job lifecycle states (CTR-0131). ``state`` is the current snapshot; ``last_status``
# is the outcome of the most recent run.
STATE_SCHEDULED = "scheduled"
STATE_RUNNING = "running"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_DISABLED = "disabled"
STATE_COMPLETED = "completed"

# Run outcomes (CTR-0132 terminal statuses).
RUN_SUCCESS = "success"
RUN_FAILED = "failed"
RUN_TIMEOUT = "timeout"
RUN_REFUSED = "refused"


class ScriptSpec(BaseModel):
    """The script a job runs (script-only model; UDR-0067 D8)."""

    path: str = Field(..., min_length=1, max_length=1024)
    interpreter: str = Field(default="", max_length=256)
    args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bound_args(self) -> ScriptSpec:
        if len(self.args) > 64:
            msg = "too many script args (max 64)"
            raise ValueError(msg)
        return self


class ScheduleSpec(BaseModel):
    """A job schedule: cron | interval | oneshot (UDR-0067 D3)."""

    type: Literal["cron", "interval", "oneshot"]
    expr: str = Field(default="", max_length=256)
    interval_seconds: int = Field(default=0, ge=0)
    run_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _check_shape(self) -> ScheduleSpec:
        if self.type == "cron":
            if not validate_cron_expr(self.expr):
                msg = "cron schedule requires a valid 5+ field crontab 'expr'"
                raise ValueError(msg)
        elif self.type == "interval":
            if self.interval_seconds <= 0:
                msg = "interval schedule requires 'interval_seconds' > 0"
                raise ValueError(msg)
        elif self.type == "oneshot" and not self.run_at.strip():
            msg = "oneshot schedule requires an absolute ISO 'run_at'"
            raise ValueError(msg)
        return self

    def as_dict(self) -> dict[str, Any]:
        if self.type == "cron":
            return {"type": "cron", "expr": self.expr.strip()}
        if self.type == "interval":
            return {"type": "interval", "interval_seconds": self.interval_seconds}
        return {"type": "oneshot", "run_at": self.run_at.strip()}


class CronJobCreate(BaseModel):
    """Create payload shared by the REST API and the manage_cron tool."""

    category: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=1024)
    enabled: bool = True
    schedule: ScheduleSpec
    script: ScriptSpec


class CronJobUpdate(BaseModel):
    """Partial update payload; only provided fields change."""

    category: Annotated[str | None, Field(default=None, max_length=128)] = None
    description: Annotated[str | None, Field(default=None, max_length=1024)] = None
    enabled: bool | None = None
    schedule: ScheduleSpec | None = None
    script: ScriptSpec | None = None


def _repeat_for(schedule: dict[str, Any]) -> bool:
    """Recurring schedules repeat; a one-shot fires once (UDR-0067 D3)."""
    return schedule.get("type") != "oneshot"


def build_record(payload: CronJobCreate, *, created_by: str) -> dict[str, Any]:
    """Construct a full persisted job record with a computed next_run_at.

    ``created_by`` is "user" (REST API) or "llm" (manage_cron tool) for
    auditability (UDR-0067 D7).
    """
    now = now_tz()
    now_iso = datetime.now(UTC).isoformat()
    schedule = payload.schedule.as_dict()
    repeat = _repeat_for(schedule)
    nxt = compute_next_run_at(schedule, after=now)
    enabled = payload.enabled
    record: dict[str, Any] = {
        "id": f"cron_{uuid4().hex}",
        "category": payload.category.strip(),
        "description": payload.description.strip(),
        "enabled": enabled,
        "schedule": schedule,
        "repeat": repeat,
        "script": payload.script.model_dump(),
        "next_run_at": nxt.isoformat() if nxt else None,
        "last_run_at": None,
        "last_status": None,
        "state": STATE_SCHEDULED if enabled else STATE_DISABLED,
        "created_by": created_by,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    return record


def apply_update(record: dict[str, Any], update: CronJobUpdate) -> dict[str, Any]:
    """Return a new record with the update applied + next_run_at recomputed.

    Any change to the schedule (or re-enabling) recomputes next_run_at from now so
    the change takes effect at the next tick with a correct schedule (CTR-0133).
    """
    out = dict(record)
    if update.category is not None:
        out["category"] = update.category.strip()
    if update.description is not None:
        out["description"] = update.description.strip()
    if update.script is not None:
        out["script"] = update.script.model_dump()

    schedule_changed = update.schedule is not None
    if schedule_changed:
        out["schedule"] = update.schedule.as_dict()
        out["repeat"] = _repeat_for(out["schedule"])

    enabled_changed = update.enabled is not None and update.enabled != record.get("enabled")
    if update.enabled is not None:
        out["enabled"] = update.enabled

    if schedule_changed or (enabled_changed and out["enabled"]):
        nxt = compute_next_run_at(out["schedule"], after=now_tz())
        out["next_run_at"] = nxt.isoformat() if nxt else None
        out["state"] = STATE_SCHEDULED if out["enabled"] else STATE_DISABLED
    elif enabled_changed and not out["enabled"]:
        out["state"] = STATE_DISABLED

    out["updated_at"] = datetime.now(UTC).isoformat()
    return out


__all__ = [
    "RUN_FAILED",
    "RUN_REFUSED",
    "RUN_SUCCESS",
    "RUN_TIMEOUT",
    "STATE_COMPLETED",
    "STATE_DISABLED",
    "STATE_FAILED",
    "STATE_RUNNING",
    "STATE_SCHEDULED",
    "STATE_SUCCESS",
    "CronJobCreate",
    "CronJobUpdate",
    "ScheduleSpec",
    "ScriptSpec",
    "apply_update",
    "build_record",
]
