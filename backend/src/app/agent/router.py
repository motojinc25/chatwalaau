"""Tool Approval REST router (CTR-0099 part 2, PRP-0067).

Single mutating endpoint ``POST /api/tool-approval`` that the SPA uses
to release a parked tool call from the AG-UI streaming endpoint.

Lifecycle:

1. AG-UI ``_stream_with_reasoning`` sees a ``function_approval_request``
   content, registers an ``ApprovalRecord`` in
   ``app.agent.approval.approval_store``, emits a ``CUSTOM`` event
   ``tool_approval_request`` to the SPA, and parks on the record's
   ``asyncio.Event``.
2. SPA renders the ``ToolApprovalCard``; the operator clicks
   Approve / Reject / Approve-for-session. The SPA posts to this
   endpoint with the record id.
3. This endpoint resolves the matching record, optionally caches the
   decision under ``(thread_id, tool_name)``, and unblocks the parked
   waiter. A duplicate POST returns ``410 Gone`` with
   ``resolved_by`` so the SPA can collapse the card appropriately.

Auth: ``Depends(verify_api_key)`` -- per UDR-0043 D4 the skip mode does
NOT relax CTR-0083; the endpoint behaves like every other write
endpoint regardless of ``TOOL_APPROVAL_MODE``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.approval import approval_store
from app.auth import verify_api_key

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tool-approval", tags=["Tool Approval"])


class ToolApprovalRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    approved: bool
    remember_for_session: bool = False


class ToolApprovalResponse(BaseModel):
    id: str
    released: bool
    # PRP-0103 / UDR-0082 D4: number of sibling pending approvals released by
    # a remember_for_session cascade (0 when none / not a session grant).
    cascaded: int = 0


@router.post("", status_code=200, dependencies=[Depends(verify_api_key)])
async def submit_tool_approval(body: ToolApprovalRequest) -> ToolApprovalResponse:
    """Release a parked tool call with the operator's decision.

    Returns:
        200 + ``ToolApprovalResponse`` on first-time release.

    Raises:
        404 when the approval record id is unknown (never existed or
        was garbage-collected).
        410 when the record exists but was already resolved (timeout,
        abort, or a prior POST). The detail carries ``resolved_by``
        ("timeout" | "abort" | "user" | "session-cache" | "api-auto").
    """
    outcome, record = await approval_store.resolve(body.id, approved=body.approved, source="user")

    if outcome == "missing":
        raise HTTPException(
            status_code=404,
            detail={"error": "approval_request_not_found", "id": body.id},
        )
    if outcome == "already-resolved":
        assert record is not None
        prior = record.resolution.source if record.resolution else "user"
        raise HTTPException(
            status_code=410,
            detail={
                "error": "approval_request_already_resolved",
                "id": body.id,
                "resolved_by": prior,
            },
        )

    assert record is not None
    cascaded = 0
    if body.remember_for_session:
        # UDR-0043 D8 -- (thread_id, tool_name) only, no argument hash,
        # cleared on session abort / delete / process restart.
        await approval_store.cache_decision(
            thread_id=record.thread_id,
            tool_name=record.tool_name,
            approved=body.approved,
        )
        # PRP-0103 / UDR-0082 D4 -- cascade the session grant onto every
        # other approval already parked for the same (thread_id, tool_name)
        # so the sibling cards already on screen collapse with the same
        # decision (source="session-cache"), instead of waiting for the
        # operator to act on each one.
        siblings = await approval_store.resolve_session_matches(
            thread_id=record.thread_id,
            tool_name=record.tool_name,
            approved=body.approved,
            exclude_id=record.id,
        )
        cascaded = len(siblings)

    _logger.info(
        "approval %s by user for tool=%s (call_id=%s, thread_id=%s, remember=%s, cascaded=%d)",
        "approved" if body.approved else "rejected",
        record.tool_name,
        record.call_id,
        record.thread_id,
        body.remember_for_session,
        cascaded,
    )

    return ToolApprovalResponse(id=body.id, released=True, cascaded=cascaded)


__all__ = ["router"]
