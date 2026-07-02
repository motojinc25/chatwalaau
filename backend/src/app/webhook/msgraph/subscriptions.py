"""Graph Subscription Lifecycle (CTR-0152, PRP-0097, UDR-0075 D7/D8).

subscribe / renew / delete / maintain against Microsoft Graph, persisting subscription
records in the webhook store (CTR-0151). Also the ``token-health`` and ``validate``
self-tests surfaced by the management API (CTR-0154) and the agent tool (CTR-0155).

The renewal interval (MSGRAPH_SUBSCRIPTION_RENEW_HOURS, default 12) MUST be shorter
than the resource's max expiry (UDR-0075 D7). Auto-renewal is driven by a managed,
protected Cron job (``app.cron.managed``) registered at startup that calls ``maintain``
via the cron internal-handler path -- it runs only while CRON_ENABLED (UDR-0075 D8 as
amended by PRP-0097 task 4: consolidated into the Cron Scheduler). Manual renewal here
always works regardless of CRON_ENABLED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from app.core.config import settings
from app.webhook import store
from app.webhook.msgraph import SOURCE_NAME, adapter, graph_client
from app.webhook.registry import IngressContext

logger = logging.getLogger(__name__)

# The transcript resource (communications/onlineMeetings/getAllTranscripts) supports ONLY
# the "created" change type -- "updated"/"deleted" are rejected with a 400 ExtensionError.
_CHANGE_TYPE = "created"

# The managed Cron job that renews subscriptions (PRP-0097 task 4). It exists iff there is
# at least one subscription AND CRON_ENABLED -- see sync_maintenance_job().
MAINTENANCE_JOB_ID = "cron_webhook_maintain"
MAINTENANCE_ACTION = "webhook_maintain_subscriptions"
_MAINTENANCE_DESCRIPTION = "Renew Microsoft Graph webhook subscriptions before they expire (managed)."


def sync_maintenance_job() -> None:
    """Keep the managed Cron maintenance job in sync with the subscription set.

    The job exists iff there is at least one persisted subscription AND CRON_ENABLED;
    otherwise it is removed, so the Cron portal never shows an idle maintenance job after
    the last subscription is deleted (PRP-0097 task 4). Never raises.
    """
    try:
        has_subs = bool(store.list_subscriptions(SOURCE_NAME))
        if has_subs and settings.cron_enabled:
            from app.cron.managed import ensure_managed_internal_job

            interval = max(1, int(settings.msgraph_subscription_renew_hours)) * 3600
            ensure_managed_internal_job(
                job_id=MAINTENANCE_JOB_ID,
                category="webhook",
                description=_MAINTENANCE_DESCRIPTION,
                interval_seconds=interval,
                internal_action=MAINTENANCE_ACTION,
            )
        else:
            from app.cron import store as cron_store

            cron_store.delete_job(MAINTENANCE_JOB_ID)
    except Exception:
        logger.warning("failed to sync the webhook maintenance Cron job", exc_info=True)


def _renew_delta() -> timedelta:
    hours = max(1, int(settings.msgraph_subscription_renew_hours))
    return timedelta(hours=hours)


def _next_expiration() -> datetime:
    """Compute the target expiration = now + 2x the renewal interval (bounded).

    Graph caps expiry per-resource; the actual accepted value may be clamped by Graph.
    Using 2x the renew interval keeps a safety margin so a single missed renewal does
    not immediately expire the subscription.
    """
    return datetime.now(UTC) + _renew_delta() * 2


def _record_from_graph(sub: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": sub.get("id"),
        "resource": sub.get("resource"),
        "change_type": sub.get("changeType"),
        "expiration": sub.get("expirationDateTime"),
        "notification_url": sub.get("notificationUrl"),
        "created_at": now,
        "renewed_at": now,
    }


async def subscribe(*, resource: str = "", notification_url: str = "") -> dict[str, Any]:
    """Create a Graph subscription and persist its record."""
    res = (resource or settings.msgraph_webhook_resource or "").strip()
    url = (notification_url or settings.msgraph_webhook_notification_url or "").strip()
    if not res:
        msg = "No resource to subscribe (set MSGRAPH_WEBHOOK_RESOURCE)."
        raise ValueError(msg)
    if not url:
        msg = "No notification URL (set MSGRAPH_WEBHOOK_NOTIFICATION_URL)."
        raise ValueError(msg)
    if not settings.msgraph_webhook_client_state:
        msg = "No clientState secret (set MSGRAPH_WEBHOOK_CLIENT_STATE)."
        raise ValueError(msg)
    sub = await graph_client.create_subscription(
        resource=res,
        change_type=_CHANGE_TYPE,
        notification_url=url,
        client_state=settings.msgraph_webhook_client_state,
        expiration=_next_expiration(),
        # Rich resources (getAllTranscripts) require a lifecycle URL for >1h expiry. Reuse
        # the same ingress endpoint -- it handles the validation handshake and lifecycle
        # events (reauthorizationRequired -> renew) the same way (UDR-0075 D3).
        lifecycle_notification_url=url,
    )
    record = _record_from_graph(sub)
    store.save_subscription(SOURCE_NAME, record)
    # A subscription now exists -> ensure the managed maintenance Cron job is present.
    sync_maintenance_job()
    logger.info("Created Graph subscription %s for resource %s", record["id"], res)
    return record


async def renew(sub_id: str) -> dict[str, Any]:
    """Renew one subscription and update its persisted record."""
    sub = await graph_client.renew_subscription(sub_id, expiration=_next_expiration())
    record = store.get_subscription(SOURCE_NAME, sub_id) or {"id": sub_id}
    record.update(
        {
            "expiration": sub.get("expirationDateTime", record.get("expiration")),
            "renewed_at": datetime.now(UTC).isoformat(),
        }
    )
    store.save_subscription(SOURCE_NAME, record)
    logger.info("Renewed Graph subscription %s", sub_id)
    return record


async def delete(sub_id: str) -> bool:
    """Delete a subscription from Graph and the store."""
    try:
        await graph_client.delete_subscription(sub_id)
    except graph_client.GraphApiError as exc:
        if exc.status_code != 404:
            raise
    store.delete_subscription(SOURCE_NAME, sub_id)
    # If that was the last subscription, remove the now-idle managed maintenance Cron job.
    sync_maintenance_job()
    logger.info("Deleted Graph subscription %s", sub_id)
    return True


def _expiry_soon(record: dict[str, Any]) -> bool:
    exp_raw = record.get("expiration")
    if not exp_raw:
        return True
    try:
        exp = datetime.fromisoformat(str(exp_raw))
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp <= datetime.now(UTC) + _renew_delta()


async def maintain() -> dict[str, Any]:
    """Renew every persisted subscription whose expiry is within the renewal window.

    Called by the internal scheduler (only while CRON_ENABLED, UDR-0075 D8) and on
    demand via the management API / agent tool. Never raises; reports per-id outcomes.
    """
    renewed: list[str] = []
    failed: list[dict[str, str]] = []
    for record in store.list_subscriptions(SOURCE_NAME):
        sub_id = record.get("id")
        if not sub_id or not _expiry_soon(record):
            continue
        try:
            await renew(sub_id)
            renewed.append(sub_id)
        except Exception as exc:
            logger.warning("Failed to renew subscription %s: %s", sub_id, exc)
            failed.append({"id": sub_id, "error": str(exc)})
    return {"renewed": renewed, "failed": failed, "checked": len(store.list_subscriptions(SOURCE_NAME))}


async def list_all() -> dict[str, Any]:
    """Return persisted records plus (best-effort) the live Graph view."""
    persisted = store.list_subscriptions(SOURCE_NAME)
    live: list[dict[str, Any]] = []
    error = None
    try:
        live = await graph_client.list_subscriptions()
    except Exception as exc:
        error = str(exc)
    return {"subscriptions": persisted, "live": live, "live_error": error}


async def token_health() -> dict[str, Any]:
    """Delegate to the Graph client's app-only credential / permission probe."""
    return await graph_client.token_health()


async def validate() -> dict[str, Any]:
    """Validation-handshake self-test (CTR-0152 validate).

    Confirms the listener config is complete and that the adapter echoes a
    ``validationToken`` correctly (the contract Graph relies on at subscribe time).
    """
    report: dict[str, Any] = {
        "ok": False,
        "notification_url_set": bool(settings.msgraph_webhook_notification_url),
        "resource_set": bool(settings.msgraph_webhook_resource),
        "client_state_set": bool(settings.msgraph_webhook_client_state),
    }
    token = "validation-selftest-token"
    ctx = IngressContext(
        source=SOURCE_NAME,
        method="GET",
        headers={},
        query={"validationToken": token},
        raw_body=b"",
        client_ip="127.0.0.1",
    )
    resp = await adapter.handle(ctx)
    report["handshake_echo_ok"] = resp.status_code == 200 and resp.body == token and resp.media_type == "text/plain"
    report["ok"] = all(
        [
            report["notification_url_set"],
            report["resource_set"],
            report["client_state_set"],
            report["handshake_echo_ok"],
        ]
    )
    return report


__all__ = [
    "MAINTENANCE_ACTION",
    "MAINTENANCE_JOB_ID",
    "delete",
    "list_all",
    "maintain",
    "renew",
    "subscribe",
    "sync_maintenance_job",
    "token_health",
    "validate",
]
