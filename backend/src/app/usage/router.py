"""Usage Statistics API (CTR-0201, PRP-0158, FEAT-0066, UDR-0136).

    GET /api/usage/summary?from&to&tz&group_by&series&lane
    GET /api/usage/export?from&to&tz&lane&format=csv       (PRP-0173)

Read-only aggregation over the CTR-0200 ledger. The endpoint exists in this
proposal because a ledger with no reader can be verified by nothing except its own
writer.

It consumes ``verify_api_key`` (CTR-0083) although the invariant only compels the
dependency for mutating methods: usage history is operational data.

Returns token counts only -- there is no cost, price or currency field anywhere in
this module (UDR-0136 D3), and no claim that the numbers are a bill: ``coverage``
states in every response what the ledger observes (UDR-0136 D11). Since v0.154.0
(PRP-0170, UDR-0152) that includes Declarative Workflow runs, recorded per node.

The v0.145.0 coverage text also named the framework's compaction calls. That was
wrong and is corrected here (PRP-0159, UDR-0137 D1): no compaction strategy this
application constructs takes a client, so none calls a model. The condition is kept
rather than deleted (D2), because a summarizing strategy would -- and an invariant
test fails the moment one is built.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth import verify_api_key
from app.usage.aggregate import GROUP_BY, iter_records, summarize
from app.usage.export import iter_csv

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/usage", tags=["usage"])

# Stated in every response (UDR-0136 D11). A record that looks authoritative but is
# incomplete is more dangerous than no record, so the gap travels with the numbers
# instead of living only in a document someone may not read.
COVERAGE_NOTE = (
    "Observable model calls in every lane, including Declarative Workflow runs "
    "(recorded per node since v0.154.0). A model call that fails before its provider "
    "reports usage records nothing, so these totals are what the observable work "
    "consumed -- not what the account was charged. Context compaction does not call a "
    "model in any configuration this application builds, so it consumes no tokens to "
    "record; a summarizing compaction strategy would, and none is constructed."
)


EXPORT_FORMATS = ("csv",)


def _parse_date(value: str | None, *, field: str, default: date) -> date:
    if not value:
        return default
    try:
        # A calendar date, not an instant: the zone is applied by the caller.
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD") from None


def _parse_tz(value: str | None) -> tuple[tzinfo, str]:
    """Resolve an IANA zone name (UDR-0155 D1). Absent means UTC -- the old behaviour."""
    name = (value or "").strip() or "UTC"
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=400, detail="tz must be an IANA time zone name") from None


def _parse_range(from_: str | None, to: str | None, zone: tzinfo) -> tuple[date, date]:
    """Inclusive local dates; defaults to the last 30 days ending today in ``zone``."""
    today = datetime.now(zone).date()
    end = _parse_date(to, field="to", default=today)
    start = _parse_date(from_, field="from", default=end - timedelta(days=29))
    if start > end:
        raise HTTPException(status_code=400, detail="from must not be after to")
    return start, end


@router.get("/summary", dependencies=[Depends(verify_api_key)])
def usage_summary(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    tz: str | None = Query(default=None),
    group_by: str = Query(default="day"),
    series: str | None = Query(default=None),
    lane: str | None = Query(default=None),
) -> dict[str, Any]:
    """Aggregate the ledger over an inclusive date range in ``tz`` (default UTC).

    Defaults to the last 30 days, which is the range an operator asking "what did
    this month cost" almost always means, and bounds the work when no range is given.
    A plain def (PRP-0173): reading month files is blocking work, so FastAPI runs it
    in the threadpool instead of on the event loop -- the dashboard issues several of
    these at once.
    ``series`` splits each group on a second axis (PRP-0173, UDR-0155 D3).
    """
    zone, zone_name = _parse_tz(tz)
    start, end = _parse_range(from_, to, zone)
    choices = ", ".join(GROUP_BY)
    if group_by not in GROUP_BY:
        raise HTTPException(status_code=400, detail=f"group_by must be one of {choices}")
    series = series or None
    if series is not None:
        if series not in GROUP_BY:
            raise HTTPException(status_code=400, detail=f"series must be one of {choices}")
        if series == group_by:
            raise HTTPException(status_code=400, detail="series must differ from group_by")

    result = summarize(start=start, end=end, group_by=group_by, lane=lane, series=series, tz=zone, tz_name=zone_name)
    result["coverage"] = COVERAGE_NOTE
    return result


def _filename_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-") or "UTC"


@router.get("/export", dependencies=[Depends(verify_api_key)])
def usage_export(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    tz: str | None = Query(default=None),
    lane: str | None = Query(default=None),
    export_format: str = Query(default="csv", alias="format"),
) -> StreamingResponse:
    """Export the RAW ledger records in range as CSV (PRP-0173, UDR-0155 D5).

    The records are read first (as the summary does), so the skipped-line count is
    known before the first byte is sent; the CSV itself is rendered row by row.
    """
    if export_format not in EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"format must be one of {', '.join(EXPORT_FORMATS)}")
    zone, zone_name = _parse_tz(tz)
    start, end = _parse_range(from_, to, zone)

    records, skipped = iter_records(start, end, zone)
    if lane:
        records = [r for r in records if r.get("lane") == lane]

    filename = f"token-usage_{start.isoformat()}_{end.isoformat()}_{_filename_slug(zone_name)}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Usage-Skipped-Lines": str(skipped),
        "X-Usage-Coverage": COVERAGE_NOTE,
        "Cache-Control": "no-store",
    }
    return StreamingResponse(iter_csv(records, zone), media_type="text/csv; charset=utf-8", headers=headers)


__all__ = ["COVERAGE_NOTE", "EXPORT_FORMATS", "router"]
