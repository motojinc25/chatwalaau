"""Ledger aggregation (CTR-0201, PRP-0158, UDR-0136).

Reads only the monthly files a date range touches, groups them on one axis, and
sums the token fields.

Two rules do the real work here:

* **Absent is not zero.** A sum adds only the keys a record actually carries. A
  provider that reports no cache figures must not appear to have used zero of them,
  because a false zero is indistinguishable from a real one once summed
  (UDR-0135 D7).
* **A corrupt line costs its line.** An unparseable record is skipped and counted,
  never allowed to fail the month -- which is the whole reason the store is JSONL
  rather than one JSON array.

No cost, price or currency is computed anywhere (UDR-0136 D3). Token counts have
one meaning; a price has a date, a currency and a contract.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import json
import logging
from typing import Any

from app.usage.ledger import TOKEN_FIELDS, ledger_dir

logger = logging.getLogger(__name__)

GROUP_BY = ("day", "month", "chat", "model", "lane")

# The chat view's bucket for Temporary Chat records, which carry no thread id by
# design (UDR-0136 D9). Collected under an explicit key rather than dropped, so the
# grouped rows still reconcile with the totals.
TEMPORARY_KEY = "(temporary)"

# A record whose grouping field is missing entirely (an older or partial record).
UNKNOWN_KEY = "(unknown)"


def _months_in_range(start: date, end: date) -> list[str]:
    """The YYYY-MM stems the range covers, inclusive."""
    stems: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        stems.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return stems


def _record_date(record: dict[str, Any]) -> date | None:
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(UTC).date()
    except ValueError:
        return None


def iter_records(start: date, end: date) -> tuple[list[dict[str, Any]], int]:
    """Return (records in range, skipped line count).

    Skipped lines are counted rather than silently dropped: a month that is quietly
    losing lines should be visible to whoever reads the summary.
    """
    records: list[dict[str, Any]] = []
    skipped = 0
    root = ledger_dir()
    for stem in _months_in_range(start, end):
        path = root / f"{stem}.jsonl"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("usage ledger month unreadable: %s", path, exc_info=True)
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(record, dict):
                skipped += 1
                continue
            when = _record_date(record)
            if when is None or when < start or when > end:
                continue
            records.append(record)
    return records, skipped


def _group_key(record: dict[str, Any], group_by: str) -> str:
    if group_by == "day":
        when = _record_date(record)
        return when.isoformat() if when else UNKNOWN_KEY
    if group_by == "month":
        when = _record_date(record)
        return f"{when:%Y-%m}" if when else UNKNOWN_KEY
    if group_by == "chat":
        if record.get("temporary"):
            return TEMPORARY_KEY
        thread_id = record.get("thread_id")
        return str(thread_id) if thread_id else UNKNOWN_KEY
    value = record.get(group_by)
    return str(value) if value else UNKNOWN_KEY


def _blank_bucket() -> dict[str, int]:
    return {"records": 0, "model_calls": 0}


def _accumulate(bucket: dict[str, int], record: dict[str, Any]) -> None:
    bucket["records"] += 1
    calls = record.get("model_calls")
    if isinstance(calls, int) and not isinstance(calls, bool):
        bucket["model_calls"] += calls
    for field in TOKEN_FIELDS:
        value = record.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            # Absent is not zero: the key is created only when a record reported it,
            # so a group that never saw a measurement omits it rather than claiming 0.
            bucket[field] = bucket.get(field, 0) + value


def summarize(
    *,
    start: date,
    end: date,
    group_by: str = "day",
    lane: str | None = None,
) -> dict[str, Any]:
    """Aggregate the ledger over ``[start, end]`` (inclusive, UTC dates)."""
    records, skipped = iter_records(start, end)
    if lane:
        records = [r for r in records if r.get("lane") == lane]

    buckets: dict[str, dict[str, int]] = {}
    totals = _blank_bucket()
    for record in records:
        key = _group_key(record, group_by)
        bucket = buckets.setdefault(key, _blank_bucket())
        _accumulate(bucket, record)
        _accumulate(totals, record)

    groups = [{"key": key, **buckets[key]} for key in sorted(buckets)]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "group_by": group_by,
        "lane": lane,
        "groups": groups,
        "totals": totals,
        "skipped_lines": skipped,
    }


__all__ = ["GROUP_BY", "TEMPORARY_KEY", "UNKNOWN_KEY", "iter_records", "summarize"]
