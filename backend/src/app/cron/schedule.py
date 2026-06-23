"""Cron schedule math: timezone resolution + next_run_at computation (CTR-0130).

Three schedule shapes (UDR-0067 D3):

- ``cron``     : a 5+ field crontab expression, evaluated by ``croniter`` in the
                 configured timezone (CRON_TIMEZONE, default = system local).
- ``interval`` : ``interval_seconds`` after the reference time.
- ``oneshot``  : a single absolute ``run_at``; never reschedules.

All timestamps are ISO-8601 with timezone (UDR-0067 D6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from app.core.config import settings

logger = logging.getLogger(__name__)


def resolve_tz() -> tzinfo:
    """Return the timezone cron expressions evaluate in (CRON_TIMEZONE).

    Empty / unset -> the system local timezone. An unknown IANA name falls back
    to local with a warning (never raises; UDR-0067 D6).
    """
    name = (settings.cron_timezone or "").strip()
    if not name:
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("CRON_TIMEZONE %r is not a valid IANA name; using system local", name)
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def now_tz() -> datetime:
    """Current time as a timezone-aware datetime in the configured cron timezone."""
    return datetime.now(resolve_tz())


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware datetime; None if absent/invalid.

    A naive timestamp is interpreted in the configured cron timezone.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=resolve_tz())
    return dt


def validate_cron_expr(expr: str) -> bool:
    """Return True for a 5+ field crontab expression croniter accepts."""
    expr = (expr or "").strip()
    if len(expr.split()) < 5:
        return False
    return bool(croniter.is_valid(expr))


def compute_next_run_at(schedule: dict, *, after: datetime) -> datetime | None:
    """Return the first occurrence STRICTLY after ``after``, or None.

    ``after`` must be tz-aware. For ``oneshot`` the single ``run_at`` is returned
    only when it is still in the future relative to ``after`` (a past one-shot has
    no next run).
    """
    kind = schedule.get("type")
    if kind == "cron":
        expr = (schedule.get("expr") or "").strip()
        if not validate_cron_expr(expr):
            return None
        itr = croniter(expr, after)
        nxt = itr.get_next(datetime)
        # croniter returns a tz-aware datetime in after's tz when after is aware.
        return nxt
    if kind == "interval":
        seconds = int(schedule.get("interval_seconds") or 0)
        if seconds <= 0:
            return None
        return after + timedelta(seconds=seconds)
    if kind == "oneshot":
        run_at = parse_iso(schedule.get("run_at"))
        if run_at is None:
            return None
        return run_at if run_at > after else None
    return None


__all__ = [
    "compute_next_run_at",
    "now_tz",
    "parse_iso",
    "resolve_tz",
    "validate_cron_expr",
]
