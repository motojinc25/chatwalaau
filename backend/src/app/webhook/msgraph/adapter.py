"""Microsoft Graph Webhook Adapter (CTR-0150, PRP-0097, UDR-0075 D3/D4).

The ingress handler for the ``msgraph`` source. It runs at the public boundary
(CTR-0149) and:

1. Answers the Graph VALIDATION HANDSHAKE: a request carrying ``validationToken`` is
   echoed back as text/plain 200 within the handshake window (subscribe fails
   otherwise) (UDR-0075 D3).
2. For a notification batch: per notification, verifies ``clientState`` (HMAC-safe
   constant-time compare), optional source CIDR allowlist, optional resource
   allowlist; de-duplicates via a bounded PROCESS-LOCAL store; records a RECEIPT; and
   hands an accepted notification to the Teams Meeting Pipeline (CTR-0156).
3. ACKs the whole batch with 202 (accepted OR duplicate) (UDR-0075 D3).

Durable idempotency across restart is the deterministic pipeline job id (UDR-0076 D8);
this in-memory dedupe only collapses rapid redeliveries within one process lifetime
(UDR-0075 D4, mirroring the CAP-009 dedup store).
"""

from __future__ import annotations

from collections import OrderedDict
import hmac
import ipaddress
import json
import logging
from typing import Any

from app.core.config import settings
from app.webhook import store
from app.webhook.msgraph import SOURCE_NAME, meeting_pipeline
from app.webhook.registry import IngressContext, IngressResponse

logger = logging.getLogger(__name__)

# Bounded process-local dedup of recently-seen notification identities (UDR-0075 D4).
_seen: OrderedDict[str, bool] = OrderedDict()
_SEEN_MAX = 2000


def _mark_seen(identity: str) -> bool:
    """Return True if this identity is NEW (record it); False if already seen."""
    if identity in _seen:
        _seen.move_to_end(identity)
        return False
    _seen[identity] = True
    if len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return True


def _identity(n: dict[str, Any]) -> str:
    rd = n.get("resourceData") or {}
    rd_id = rd.get("id", "") if isinstance(rd, dict) else ""
    return "|".join(
        [str(n.get("subscriptionId", "")), str(n.get("resource", "")), str(n.get("changeType", "")), str(rd_id)]
    )


def _client_state_ok(received: str) -> bool:
    expected = settings.msgraph_webhook_client_state or ""
    if not expected:
        # No secret configured -> cannot validate; treat as not-ok (fail closed).
        return False
    return hmac.compare_digest(str(received or ""), expected)


def _cidr_ok(client_ip: str) -> bool:
    cidrs = settings.msgraph_webhook_allowed_cidr_list
    if not cidrs:
        return True
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _resource_ok(resource: str) -> bool:
    allow = settings.msgraph_webhook_allowed_resource_list
    if not allow:
        return True
    return any(resource.startswith(a) for a in allow)


async def handle(ctx: IngressContext) -> IngressResponse:
    """Ingress handler for the msgraph source (CTR-0149 dispatch target)."""
    # 1. Validation handshake (UDR-0075 D3): echo the token as text/plain.
    token = ctx.query.get("validationToken") or ctx.query.get("validationtoken")
    if token is not None:
        logger.info("msgraph webhook validation handshake answered")
        return IngressResponse(status_code=200, media_type="text/plain", body=token)

    # 2. Parse the notification batch.
    try:
        payload = json.loads(ctx.raw_body.decode("utf-8")) if ctx.raw_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        store.save_receipt(SOURCE_NAME, outcome="rejected", summary="malformed JSON body")
        return IngressResponse(status_code=202, body=json.dumps({"accepted": 0, "rejected": 1}))

    notifications = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(notifications, list):
        store.save_receipt(SOURCE_NAME, outcome="rejected", summary="no notification array")
        return IngressResponse(status_code=202, body=json.dumps({"accepted": 0, "rejected": 1}))

    accepted = duplicate = rejected = lifecycle = 0
    if not _cidr_ok(ctx.client_ip):
        for n in notifications:
            store.save_receipt(
                SOURCE_NAME, outcome="rejected", summary=f"source IP not allowed: {ctx.client_ip}", detail=n
            )
        return IngressResponse(status_code=202, body=json.dumps({"accepted": 0, "rejected": len(notifications)}))

    for n in notifications:
        if not isinstance(n, dict):
            rejected += 1
            continue
        resource = str(n.get("resource", ""))
        if not _client_state_ok(n.get("clientState", "")):
            rejected += 1
            store.save_receipt(SOURCE_NAME, outcome="rejected", summary="clientState mismatch", detail=n)
            continue
        # Lifecycle events (reauthorizationRequired / subscriptionRemoved / missed) carry a
        # lifecycleEvent field and no resourceData; handle them before the transcript path
        # (UDR-0075 D3). reauthorizationRequired triggers a best-effort renew.
        event = str(n.get("lifecycleEvent", ""))
        if event:
            await _handle_lifecycle(event, n)
            lifecycle += 1
            store.save_receipt(SOURCE_NAME, outcome="lifecycle", summary=f"lifecycle:{event}", detail=n)
            continue
        if not _resource_ok(resource):
            rejected += 1
            store.save_receipt(SOURCE_NAME, outcome="rejected", summary=f"resource not allowed: {resource}", detail=n)
            continue
        if not _mark_seen(_identity(n)):
            duplicate += 1
            store.save_receipt(SOURCE_NAME, outcome="duplicate", summary=resource, detail=n)
            continue
        # Accepted: hand off asynchronously to the pipeline (UDR-0075 D3).
        job_id = None
        try:
            handoff = await meeting_pipeline.handle_notification(n)
            if handoff and handoff.get("deduped"):
                duplicate += 1
                store.save_receipt(SOURCE_NAME, outcome="duplicate", summary=resource, detail=n)
                continue
            job_id = handoff.get("job_id") if handoff else None
        except Exception:
            logger.exception("msgraph notification handoff failed")
        accepted += 1
        store.save_receipt(SOURCE_NAME, outcome="accepted", summary=resource, detail=n, job_id=job_id)

    return IngressResponse(
        status_code=202,
        body=json.dumps({"accepted": accepted, "duplicate": duplicate, "rejected": rejected, "lifecycle": lifecycle}),
    )


async def _handle_lifecycle(event: str, n: dict[str, Any]) -> None:
    """Act on a Graph subscription lifecycle event (best-effort; never raises)."""
    sub_id = str(n.get("subscriptionId", ""))
    try:
        if event == "reauthorizationRequired" and sub_id:
            from app.webhook.msgraph import subscriptions  # lazy: avoid import cycle

            await subscriptions.renew(sub_id)
            logger.info("msgraph subscription %s reauthorized (renewed)", sub_id)
        elif event == "subscriptionRemoved" and sub_id:
            store.delete_subscription(SOURCE_NAME, sub_id)
            from app.webhook.msgraph import subscriptions  # lazy: avoid import cycle

            subscriptions.sync_maintenance_job()
            logger.info("msgraph subscription %s removed by Graph; dropped from store", sub_id)
        else:
            logger.info("msgraph lifecycle event '%s' for subscription %s", event, sub_id)
    except Exception:
        logger.warning("msgraph lifecycle handling failed for %s (%s)", sub_id, event, exc_info=True)


def _reset_seen_for_test() -> None:
    _seen.clear()


__all__ = ["handle"]
