"""User-Delegated Dedicated Fetch API (CTR-0159, PRP-0098, UDR-0077).

The interactive device-code surface for the manual, user-delegated ("Dedicated") Teams
meeting pipeline (FEAT-0054). Endpoints under ``/api/webhooks/msgraph/dedicated/*``:

    POST /api/webhooks/msgraph/dedicated/start          start a device-code flow for a meeting
    GET  /api/webhooks/msgraph/dedicated/status/{flow}  poll flow status
    POST /api/webhooks/msgraph/dedicated/cancel/{flow}  cancel a pending flow (best-effort)

The flow:

1. ``start`` begins the OAuth 2.0 Device Authorization Grant (CTR-0158) and returns the
   ``user_code`` + ``verification_uri`` for the operator to complete in a browser, plus an
   opaque ``flow_id``. A background task then polls Entra for the token.
2. On authorization, the background task submits a ``teams-meeting`` job in ``delegated``
   mode (CTR-0156) with the access token stashed in memory; ``status`` reports the
   resulting ``job_id``.

Both the ``flow_id`` and any in-flight delegated token are PROCESS-LOCAL and NEVER
persisted (UDR-0077 D3) -- the status payload never carries the token. Every endpoint
consumes CTR-0083 (``verify_api_key``; loopback bypass preserved, UDR-0077 D6); the whole
surface 404s when ``WEBHOOK_ENABLED`` is false.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks/msgraph/dedicated", tags=["Webhook Dedicated"])

# Flow lifecycle states surfaced to the portal.
STATE_PENDING = "pending_auth"
STATE_AUTHORIZED = "authorized"
STATE_ENQUEUED = "job_enqueued"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

_TERMINAL = {STATE_ENQUEUED, STATE_FAILED, STATE_CANCELLED}
_MAX_FLOWS = 64


@dataclass
class _Flow:
    """Process-local device-code flow state. Never persisted (UDR-0077 D3)."""

    id: str
    state: str = STATE_PENDING
    job_id: str | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_status(self) -> dict:
        return {"flow_id": self.id, "state": self.state, "job_id": self.job_id, "error": self.error}


# Process-local flow registry. Holds no secrets (the token lives only in the delegated
# client's run-token map, addressed by ref); flows are pruned on each start.
_flows: dict[str, _Flow] = {}


class StartRequest(BaseModel):
    meeting_id: str = Field(default="", description="Graph onlineMeeting id (opaque string).")
    join_web_url: str = Field(default="", description="Meeting joinWebUrl (recommended).")


def _require_enabled() -> None:
    if not settings.webhook_enabled:
        raise HTTPException(status_code=404, detail={"error": "webhook_disabled"})


def _prune() -> None:
    """Drop terminal flows when the registry grows past the cap (oldest-first by insertion)."""
    if len(_flows) <= _MAX_FLOWS:
        return
    for fid in [fid for fid, fl in _flows.items() if fl.state in _TERMINAL]:
        _flows.pop(fid, None)
        if len(_flows) <= _MAX_FLOWS:
            break


async def _drive_flow(flow: _Flow, *, device_code: str, interval: int, expires_in: int, meeting_id: str, join_web_url: str) -> None:
    """Background task: poll for the delegated token, then enqueue the meeting job."""
    from app.webhook.msgraph import graph_delegated_client, meeting_pipeline

    try:
        token = await graph_delegated_client.poll_for_token(device_code, interval=interval, expires_in=expires_in)
        flow.state = STATE_AUTHORIZED
        result = await meeting_pipeline.submit_dedicated_job(token, meeting_id=meeting_id, join_web_url=join_web_url)
        # Drop the local reference to the token as soon as the job owns it (it is consumed
        # from the run-token map by the runner; never persisted).
        del token
        flow.job_id = result.get("job_id")
        flow.state = STATE_ENQUEUED
        logger.info("Dedicated meeting flow %s enqueued job %s", flow.id, flow.job_id)
    except asyncio.CancelledError:
        flow.state = STATE_CANCELLED
        raise
    except Exception as exc:  # surfaced to the operator via the flow status
        flow.state = STATE_FAILED
        flow.error = str(exc)
        logger.warning("Dedicated meeting flow %s failed: %s", flow.id, exc)


@router.post("/start", dependencies=[Depends(verify_api_key)])
async def dedicated_start(body: StartRequest) -> dict:
    """Begin a device-code login for a meeting and return the user prompt + flow handle."""
    _require_enabled()
    if not settings.pipeline_enabled:
        raise HTTPException(status_code=409, detail={"error": "pipeline_disabled"})
    from app.webhook.msgraph import graph_client, graph_delegated_client

    if not graph_delegated_client.is_configured():
        raise HTTPException(
            status_code=409,
            detail={"error": "graph_not_configured", "message": "GRAPH_TENANT_ID / GRAPH_CLIENT_ID are required."},
        )
    meeting_id = body.meeting_id.strip()
    join_web_url = body.join_web_url.strip()
    if not (meeting_id or join_web_url):
        raise HTTPException(status_code=400, detail={"error": "missing_meeting", "message": "Provide a meeting id or join URL."})
    if meeting_id and meeting_id.replace(" ", "").isdigit():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "numeric_meeting_id",
                "message": "That is the numeric Teams 'Meeting ID' (dial-in id), which Graph cannot resolve. "
                "Use the Join URL or the Graph onlineMeeting id.",
            },
        )
    try:
        code = await graph_delegated_client.start_device_code()
    except graph_client.GraphConfigError as exc:
        raise HTTPException(status_code=409, detail={"error": "graph_not_configured", "message": str(exc)}) from exc
    except graph_client.GraphApiError as exc:
        raise HTTPException(status_code=502, detail={"error": f"graph_error: {exc}"}) from exc

    _prune()
    flow = _Flow(id=uuid.uuid4().hex)
    _flows[flow.id] = flow
    flow.task = asyncio.create_task(
        _drive_flow(
            flow,
            device_code=code["device_code"],
            interval=int(code.get("interval", 5)),
            expires_in=int(code.get("expires_in", 900)),
            meeting_id=meeting_id,
            join_web_url=join_web_url,
        )
    )
    return {
        "flow_id": flow.id,
        "user_code": code.get("user_code"),
        "verification_uri": code.get("verification_uri"),
        "expires_in": code.get("expires_in"),
        "interval": code.get("interval"),
        "message": code.get("message"),
    }


@router.get("/status/{flow_id}", dependencies=[Depends(verify_api_key)])
async def dedicated_status(flow_id: str) -> dict:
    """Report a flow's state (pending_auth / authorized / job_enqueued / failed / cancelled)."""
    _require_enabled()
    flow = _flows.get(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_flow"})
    return flow.to_status()


@router.post("/cancel/{flow_id}", dependencies=[Depends(verify_api_key)])
async def dedicated_cancel(flow_id: str) -> dict:
    """Cancel a pending device-code flow (best-effort)."""
    _require_enabled()
    flow = _flows.get(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_flow"})
    if flow.state not in _TERMINAL and flow.task is not None:
        flow.task.cancel()
        flow.state = STATE_CANCELLED
    return flow.to_status()


def _reset_for_test() -> None:
    _flows.clear()


__all__ = ["router"]
