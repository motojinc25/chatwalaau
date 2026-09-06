"""Usage Statistics API (CTR-0201, PRP-0158, FEAT-0066, UDR-0136).

    GET /api/usage/summary?from&to&group_by&lane

Read-only aggregation over the CTR-0200 ledger. The endpoint exists in this
proposal because a ledger with no reader can be verified by nothing except its own
writer.

It consumes ``verify_api_key`` (CTR-0083) although the invariant only compels the
dependency for mutating methods: usage history is operational data.

Returns token counts only -- there is no cost, price or currency field anywhere in
this module (UDR-0136 D3), and no completeness claim: the Declarative Workflow lane
and MAF's internal compaction calls are not in the ledger as of v0.145.0
(UDR-0136 D11), which ``coverage`` states in every response so a consumer cannot
mistake the numbers for a bill.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import verify_api_key
from app.usage.aggregate import GROUP_BY, summarize

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/usage", tags=["usage"])

# Stated in every response (UDR-0136 D11). A record that looks authoritative but is
# incomplete is more dangerous than no record, so the gap travels with the numbers
# instead of living only in a document someone may not read.
COVERAGE_NOTE = (
    "Observable model calls only. Declarative Workflow runs and the framework's "
    "internal compaction calls are not recorded, so these totals are what the "
    "observable work consumed -- not what the account was charged."
)


def _parse_date(value: str | None, *, field: str, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD") from None


@router.get("/summary", dependencies=[Depends(verify_api_key)])
async def usage_summary(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    group_by: str = Query(default="day"),
    lane: str | None = Query(default=None),
) -> dict[str, Any]:
    """Aggregate the ledger over an inclusive UTC date range.

    Defaults to the last 30 days, which is the range an operator asking "what did
    this month cost" almost always means, and bounds the work when no range is given.
    """
    today = datetime.now(UTC).date()
    end = _parse_date(to, field="to", default=today)
    start = _parse_date(from_, field="from", default=end - timedelta(days=29))

    if start > end:
        raise HTTPException(status_code=400, detail="from must not be after to")
    if group_by not in GROUP_BY:
        raise HTTPException(status_code=400, detail=f"group_by must be one of {', '.join(GROUP_BY)}")

    result = summarize(start=start, end=end, group_by=group_by, lane=lane)
    result["coverage"] = COVERAGE_NOTE
    return result


__all__ = ["COVERAGE_NOTE", "router"]
