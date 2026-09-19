"""Ledger aggregation (CTR-0201, PRP-0158, UDR-0136, PRP-0173, UDR-0155).

Reads only the monthly files a date range touches, groups them on one axis (and,
since PRP-0173, optionally splits each group on a second ``series`` axis), and sums
the token fields.

Dates are calendar dates in an explicit IANA zone (UDR-0155 D1). The default zone is
UTC, which reproduces the pre-PRP-0173 behaviour exactly; the ledger itself stays UTC
and zone-free -- a viewer's calendar is applied on read, never written.

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

from datetime import UTC, date, datetime, timedelta, tzinfo
import json
import logging
from typing import Any

from app.usage.ledger import TOKEN_FIELDS, ledger_dir

logger = logging.getLogger(__name__)

GROUP_BY = (
    "day",
    "month",
    "chat",
    "model",
    "lane",
    "run_target",
    "node",
    "agent",
    # PRP-0173 (UDR-0155 D3): fields every record already carries.
    "provider",
    "outcome",
    "kind",
)

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


def record_datetime(record: dict[str, Any], tz: tzinfo = UTC) -> datetime | None:
    """The record's ``ts`` as an aware datetime in ``tz``, or None when unusable."""
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        # The writer always stores UTC; a naive value can only mean UTC.
        when = when.replace(tzinfo=UTC)
    return when.astimezone(tz)


# Private key carrying the record's ts, parsed once and converted to the requested zone
# by iter_records. It never leaves this package: buckets are built from named fields and
# the export writes EXPORT_COLUMNS only.
LOCAL_TS_KEY = "_local_ts"


def _local(record: dict[str, Any], tz: tzinfo = UTC) -> datetime | None:
    when = record.get(LOCAL_TS_KEY)
    if isinstance(when, datetime):
        return when
    return record_datetime(record, tz)


def iter_records(start: date, end: date, tz: tzinfo = UTC) -> tuple[list[dict[str, Any]], int]:
    """Return (records in range, skipped line count).

    ``start`` / ``end`` are calendar dates in ``tz``. A local date range maps to a UTC
    range that can reach up to one day past either end, so the month files are chosen
    with that margin and the records then filtered by their LOCAL date (UDR-0155 D1).

    Skipped lines are counted rather than silently dropped: a month that is quietly
    losing lines should be visible to whoever reads the summary.
    """
    records: list[dict[str, Any]] = []
    skipped = 0
    root = ledger_dir()
    for stem in _months_in_range(start - timedelta(days=1), end + timedelta(days=1)):
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
            when = record_datetime(record, tz)
            if when is None or not start <= when.date() <= end:
                continue
            record[LOCAL_TS_KEY] = when
            records.append(record)
    return records, skipped


def _group_key(record: dict[str, Any], group_by: str, tz: tzinfo = UTC) -> str:
    if group_by == "day":
        when = _local(record, tz)
        return when.date().isoformat() if when else UNKNOWN_KEY
    if group_by == "month":
        when = _local(record, tz)
        return f"{when:%Y-%m}" if when else UNKNOWN_KEY
    if group_by == "chat":
        if record.get("temporary"):
            return TEMPORARY_KEY
        thread_id = record.get("thread_id")
        return str(thread_id) if thread_id else UNKNOWN_KEY
    if group_by == "node":
        # An action id is only unique within its workflow (PRP-0170).
        node = record.get("node")
        if not node:
            return UNKNOWN_KEY
        return f"{record.get('run_target') or UNKNOWN_KEY} / {node}"
    value = record.get(group_by)
    return str(value) if value else UNKNOWN_KEY


def _blank_bucket() -> dict[str, Any]:
    return {"records": 0, "model_calls": 0}


def _accumulate(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    bucket["records"] += 1
    # First / last activity (PRP-0173): the stored UTC strings, compared as instants
    # (aware datetimes compare by instant whatever their zone).
    when = _local(record)
    if when is not None:
        raw = record["ts"]
        first = bucket.get("_first")
        if first is None or when < first:
            bucket["_first"], bucket["first_ts"] = when, raw
        last = bucket.get("_last")
        if last is None or when > last:
            bucket["_last"], bucket["last_ts"] = when, raw
    calls = record.get("model_calls")
    if isinstance(calls, int) and not isinstance(calls, bool):
        bucket["model_calls"] += calls
    for field in TOKEN_FIELDS:
        value = record.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            # Absent is not zero: the key is created only when a record reported it,
            # so a group that never saw a measurement omits it rather than claiming 0.
            bucket[field] = bucket.get(field, 0) + value


def _public(bucket: dict[str, Any]) -> dict[str, Any]:
    """Drop the private comparison keys before a bucket leaves the module."""
    return {k: v for k, v in bucket.items() if not k.startswith("_")}


def summarize(
    *,
    start: date,
    end: date,
    group_by: str = "day",
    lane: str | None = None,
    series: str | None = None,
    tz: tzinfo = UTC,
    tz_name: str = "UTC",
) -> dict[str, Any]:
    """Aggregate the ledger over ``[start, end]`` (inclusive dates in ``tz``).

    With ``series``, each group also carries ``series`` rows splitting it on a second
    axis. The group's own sums are computed from the same records, so they always
    equal the sum of its series rows, and a token key appears in a series row only
    when a record in that row reported it (UDR-0155 D3, UDR-0135 D7).
    """
    records, skipped = iter_records(start, end, tz)
    if lane:
        records = [r for r in records if r.get("lane") == lane]

    buckets: dict[str, dict[str, Any]] = {}
    splits: dict[str, dict[str, dict[str, Any]]] = {}
    totals = _blank_bucket()
    for record in records:
        key = _group_key(record, group_by, tz)
        bucket = buckets.setdefault(key, _blank_bucket())
        _accumulate(bucket, record)
        _accumulate(totals, record)
        if series:
            sub_key = _group_key(record, series, tz)
            sub = splits.setdefault(key, {}).setdefault(sub_key, _blank_bucket())
            _accumulate(sub, record)

    groups: list[dict[str, Any]] = []
    for key in sorted(buckets):
        group = {"key": key, **_public(buckets[key])}
        if series:
            rows = splits.get(key, {})
            group["series"] = [{"key": sub_key, **_public(rows[sub_key])} for sub_key in sorted(rows)]
        groups.append(group)

    result: dict[str, Any] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "tz": tz_name,
        "group_by": group_by,
        "lane": lane,
        "groups": groups,
        "totals": _public(totals),
        "skipped_lines": skipped,
    }
    if series:
        # Only when asked: without it the response keeps its pre-PRP-0173 shape.
        result["series"] = series
    return result


__all__ = ["GROUP_BY", "LOCAL_TS_KEY", "TEMPORARY_KEY", "UNKNOWN_KEY", "iter_records", "record_datetime", "summarize"]
