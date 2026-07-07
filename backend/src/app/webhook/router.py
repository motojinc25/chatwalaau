"""Webhook Management API + capability mounting (CTR-0154, PRP-0097, UDR-0075 D10).

REST endpoints under ``/api/webhooks/*`` so the portal UI (CTR-0157) and operators can
manage sources, receipts, and (for Microsoft Graph) subscriptions:

    GET    /api/webhooks/sources                          list sources + health
    GET    /api/webhooks/sources/{source}                 one source detail
    PUT    /api/webhooks/sources/{source}                 enable/disable a source
    GET    /api/webhooks/sources/{source}/receipts        receipt records (newest first)
    GET    /api/webhooks/sources/{source}/receipts/{id}   one receipt (raw + outcome)
    GET    /api/webhooks/msgraph/subscriptions            list subscriptions
    POST   /api/webhooks/msgraph/subscriptions            subscribe (replace? -> 409 on limit)
    POST   /api/webhooks/msgraph/subscriptions/{id}/renew renew
    DELETE /api/webhooks/msgraph/subscriptions/{id}       delete
    POST   /api/webhooks/msgraph/subscriptions/maintain   renew all due
    GET    /api/webhooks/msgraph/subscriptions/maintenance-schedule       managed job state
    POST   /api/webhooks/msgraph/subscriptions/maintenance-schedule/sync  re-sync managed job
    GET    /api/webhooks/msgraph/token-health             app-only creds probe
    POST   /api/webhooks/msgraph/validate                 validation-handshake self-test
    POST   /api/webhooks/msgraph/fetch                    manual meeting-pipeline trigger

Every endpoint consumes CTR-0083 (``verify_api_key``); loopback bypass preserved. Even
though CAP-010 contracts are outside the CTR-0083 invariant's CAP-002 scope, the
management API is operator state so it is gated (defense in depth, UDR-0075 D10). The
whole surface 404s when WEBHOOK_ENABLED is false. The PUBLIC ingress (CTR-0149) is the
only un-gated surface.

This module also owns capability mounting: ``register_webhook(app)`` includes the
routers and registers the source + the teams-meeting job type when enabled;
``initialize_webhook`` / ``shutdown_webhook`` drive the maintenance scheduler.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.core.config import settings
from app.webhook import store
from app.webhook.registry import get_source, list_sources

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["Webhook Management"])

_MSGRAPH = "msgraph"


def _require_enabled() -> None:
    if not settings.webhook_enabled:
        raise HTTPException(status_code=404, detail={"error": "webhook_disabled"})


class SourceToggle(BaseModel):
    enabled: bool = Field(description="Enable or disable the source.")


class SubscribeRequest(BaseModel):
    resource: str = Field(default="", description="Graph resource; empty = MSGRAPH_WEBHOOK_RESOURCE.")
    notification_url: str = Field(default="", description="Public URL; empty = MSGRAPH_WEBHOOK_NOTIFICATION_URL.")
    replace: bool = Field(
        default=False,
        description="Delete any existing subscription for this resource first, then create "
        "(recover from the Graph per-app subscription limit; PRP-0107).",
    )


class FetchRequest(BaseModel):
    organizer_id: str = Field(
        default="",
        description="Meeting organizer's AAD object id or UPN (required; app-only access is organizer-scoped).",
    )
    meeting_id: str = Field(default="", description="Graph onlineMeeting id.")
    join_web_url: str = Field(default="", description="Meeting joinWebUrl.")


# ---------------------------------------------------------------------------
# Generic source endpoints (registry + store driven)
# ---------------------------------------------------------------------------


@router.get("/sources", dependencies=[Depends(verify_api_key)])
async def list_webhook_sources() -> dict:
    """List registered sources with enabled flag + receipt count."""
    _require_enabled()
    out = [
        {
            **src.to_dict(),
            "enabled": store.is_source_enabled(src.name),
            "receipt_count": store.count_receipts(src.name),
        }
        for src in list_sources()
    ]
    return {"sources": out}


@router.get("/sources/{source}", dependencies=[Depends(verify_api_key)])
async def get_webhook_source(source: str) -> dict:
    """One source's detail (config summary + latest receipts)."""
    _require_enabled()
    src = get_source(source)
    if src is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_source"})
    return {
        **src.to_dict(),
        "enabled": store.is_source_enabled(source),
        "receipt_count": store.count_receipts(source),
        "recent_receipts": store.list_receipts(source, limit=20),
    }


@router.put("/sources/{source}", dependencies=[Depends(verify_api_key)])
async def set_webhook_source(source: str, body: SourceToggle) -> dict:
    """Enable or disable a source (persisted; governs live subscriptions)."""
    _require_enabled()
    if get_source(source) is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_source"})
    state = store.set_source_enabled(source, body.enabled)
    return {"source": source, **state}


@router.get("/sources/{source}/receipts", dependencies=[Depends(verify_api_key)])
async def list_source_receipts(source: str) -> dict:
    """A source's receipt records (newest first)."""
    _require_enabled()
    return {"receipts": store.list_receipts(source)}


@router.get("/sources/{source}/receipts/{receipt_id}", dependencies=[Depends(verify_api_key)])
async def get_source_receipt(source: str, receipt_id: str) -> dict:
    """One receipt record (raw notification + outcome)."""
    _require_enabled()
    receipt = store.get_receipt(source, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail={"error": "receipt_not_found"})
    return receipt


# ---------------------------------------------------------------------------
# Microsoft Graph subscription endpoints (CTR-0152)
# ---------------------------------------------------------------------------


@router.get("/msgraph/subscriptions", dependencies=[Depends(verify_api_key)])
async def msgraph_list_subscriptions() -> dict:
    """List persisted + live Graph subscriptions."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    return await subscriptions.list_all()


@router.post("/msgraph/subscriptions", dependencies=[Depends(verify_api_key)])
async def msgraph_subscribe(body: SubscribeRequest) -> dict:
    """Create a Graph subscription (optionally replacing an existing one, PRP-0107).

    When the resource is already at its Graph per-app subscription limit and ``replace`` is
    false, returns 409 ``{code: "subscription_limit", resource, existing}`` (the live
    subscription(s) blocking creation) so the portal can offer a delete-and-resubscribe
    flow (UDR-0075 D14). With ``replace`` true, the matching subscription is deleted first.
    """
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    try:
        return await subscriptions.subscribe(
            resource=body.resource, notification_url=body.notification_url, replace=body.replace
        )
    except subscriptions.SubscriptionLimitError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(exc),
                "code": "subscription_limit",
                "resource": exc.resource,
                "existing": exc.existing,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"graph_error: {exc}"}) from exc


@router.post("/msgraph/subscriptions/maintain", dependencies=[Depends(verify_api_key)])
async def msgraph_maintain() -> dict:
    """Renew every subscription whose expiry is within the renewal window."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    return await subscriptions.maintain()


@router.get("/msgraph/subscriptions/maintenance-schedule", dependencies=[Depends(verify_api_key)])
async def msgraph_maintenance_schedule() -> dict:
    """Report the managed auto-renewal Cron job state (PRP-0107, UDR-0075 D15)."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    return subscriptions.maintenance_schedule()


@router.post("/msgraph/subscriptions/maintenance-schedule/sync", dependencies=[Depends(verify_api_key)])
async def msgraph_maintenance_schedule_sync() -> dict:
    """Remove then recreate the managed auto-renewal Cron job (idempotent, PRP-0107)."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    return subscriptions.resync_maintenance_job()


@router.post("/msgraph/subscriptions/{sub_id}/renew", dependencies=[Depends(verify_api_key)])
async def msgraph_renew(sub_id: str) -> dict:
    """Renew one subscription."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    try:
        return await subscriptions.renew(sub_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"graph_error: {exc}"}) from exc


@router.delete("/msgraph/subscriptions/{sub_id}", dependencies=[Depends(verify_api_key)])
async def msgraph_delete(sub_id: str) -> dict:
    """Delete a subscription."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    try:
        ok = await subscriptions.delete(sub_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"graph_error: {exc}"}) from exc
    return {"deleted": ok, "id": sub_id}


@router.get("/msgraph/token-health", dependencies=[Depends(verify_api_key)])
async def msgraph_token_health() -> dict:
    """Probe app-only credentials + Graph reachability."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    return await subscriptions.token_health()


@router.post("/msgraph/validate", dependencies=[Depends(verify_api_key)])
async def msgraph_validate() -> dict:
    """Validation-handshake self-test."""
    _require_enabled()
    from app.webhook.msgraph import subscriptions

    return await subscriptions.validate()


@router.post("/msgraph/fetch", dependencies=[Depends(verify_api_key)])
async def msgraph_fetch(body: FetchRequest) -> dict:
    """Manually trigger the Teams meeting pipeline (Fetch --meeting-id | --join-web-url)."""
    _require_enabled()
    if not settings.pipeline_enabled:
        raise HTTPException(status_code=409, detail={"error": "pipeline_disabled"})
    from app.webhook.msgraph import meeting_pipeline

    try:
        return await meeting_pipeline.fetch(
            organizer_id=body.organizer_id, meeting_id=body.meeting_id, join_web_url=body.join_web_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc


# ---------------------------------------------------------------------------
# Capability mounting + lifecycle
# ---------------------------------------------------------------------------


def register_sources() -> None:
    """Register the built-in webhook sources + contributed pipeline job type.

    Called only when WEBHOOK_ENABLED (UDR-0075 D11). Registers the msgraph source's
    ingress handler (CTR-0150) and the teams-meeting job type into the pipeline engine
    (CTR-0156, UDR-0076 D2).
    """
    from app.webhook.msgraph import SOURCE_NAME, adapter, meeting_pipeline
    from app.webhook.registry import WebhookSource, register_source

    register_source(
        WebhookSource(
            name=SOURCE_NAME,
            label="Microsoft Graph",
            description="Microsoft Graph change notifications (Teams meeting transcripts).",
            handle=adapter.handle,
            metadata={"kind": "msgraph", "resource": settings.msgraph_webhook_resource},
        )
    )
    if settings.pipeline_enabled:
        meeting_pipeline.register_meeting_job_type()
    logger.info("Webhook gateway sources registered (msgraph)")


def register_webhook(app) -> None:
    """Mount the ingress + management routers and register sources (PRP-0097).

    Always includes the routers (they 404 internally when disabled, so the SPA can probe
    them). Registers sources + the job type ONLY when WEBHOOK_ENABLED (UDR-0075 D11).
    """
    from app.webhook.dedicated import router as dedicated_router
    from app.webhook.ingress import router as ingress_router

    app.include_router(ingress_router)
    app.include_router(router)
    # User-delegated Dedicated Fetch API (CTR-0159, PRP-0098). Always included; it 404s
    # internally when WEBHOOK_ENABLED is false, so the SPA can probe it.
    app.include_router(dedicated_router)
    if settings.webhook_enabled:
        register_sources()


async def initialize_webhook() -> None:
    """Consolidate subscription maintenance into the Cron Scheduler (PRP-0097 task 4).

    Supersedes the standalone internal scheduler (UDR-0075 D8 as amended): the renewal
    loop is registered as a CRON internal handler and exposed as a managed, protected,
    interval-scheduled Cron job so it is visible and unified in the Cron portal. The job
    runs only when CRON_ENABLED (the Cron tick engine); without it, subscriptions are not
    auto-renewed and must be renewed manually (Maintain).
    """
    if not settings.webhook_enabled:
        return
    from app.cron.executor import register_internal_handler
    from app.webhook.msgraph import subscriptions

    async def _maintain() -> str:
        result = await subscriptions.maintain()
        return f"renewed={result.get('renewed')} failed={result.get('failed')} checked={result.get('checked')}"

    register_internal_handler(subscriptions.MAINTENANCE_ACTION, _maintain)

    # Create the managed maintenance Cron job iff subscriptions already exist (and cron is
    # on); otherwise remove any stale one. The job is kept in sync as subscriptions are
    # added/removed (subscriptions.sync_maintenance_job).
    subscriptions.sync_maintenance_job()
    if settings.webhook_enabled and not settings.cron_enabled:
        logger.warning(
            "WEBHOOK_ENABLED but CRON_ENABLED is false: Microsoft Graph subscriptions will "
            "NOT be auto-renewed. Enable CRON_ENABLED, or renew manually (Maintain)."
        )


async def shutdown_webhook() -> None:
    """No-op (maintenance now runs as a Cron job; nothing standalone to stop)."""
    return


__all__ = ["initialize_webhook", "register_webhook", "router", "shutdown_webhook"]
