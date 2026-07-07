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


class SubscriptionLimitError(RuntimeError):
    """Raised when Graph rejects a create because the resource's per-app subscription
    limit is already reached (403 ExtensionError "reached its limit", PRP-0107, UDR-0075
    D14).

    Carries the target resource and the LIVE subscription(s) currently occupying the
    limit, so the management API (CTR-0154) can surface a recoverable 409 that offers a
    replace-and-resubscribe flow instead of an opaque 502.
    """

    def __init__(self, resource: str, existing: list[dict[str, Any]]) -> None:
        self.resource = resource
        self.existing = existing
        msg = f"Graph subscription limit reached for resource '{resource}' ({len(existing)} existing)."
        super().__init__(msg)


def _normalize_resource(resource: str) -> str:
    """Normalize a Graph resource for comparison (trim, drop trailing slash, casefold)."""
    return (resource or "").strip().rstrip("/").casefold()


def _same_resource(a: str | None, b: str | None) -> bool:
    """True when two Graph resource strings denote the same resource (PRP-0107 REPLACE-1)."""
    return _normalize_resource(str(a or "")) == _normalize_resource(str(b or ""))


def _is_limit_error(exc: Exception) -> bool:
    """True when a Graph error is the per-app per-resource subscription-limit 403."""
    return (
        isinstance(exc, graph_client.GraphApiError)
        and exc.status_code == 403
        and "reached its limit" in (exc.body or "").casefold()
    )


async def _live_subscriptions_for(resource: str) -> list[dict[str, Any]]:
    """Best-effort list of LIVE Graph subscriptions matching ``resource`` (never raises)."""
    try:
        live = await graph_client.list_subscriptions()
    except Exception:
        logger.warning("failed to list live subscriptions for resource match", exc_info=True)
        return []
    return [s for s in live if _same_resource(s.get("resource"), resource)]


async def _purge_resource_subscriptions(resource: str) -> list[str]:
    """Delete every LIVE Graph subscription whose resource matches ``resource``.

    Same-resource-only (PRP-0107 REPLACE-1 / UDR-0075 D14): unrelated subscriptions the
    app may hold are never touched. Each delete goes through ``delete`` so Graph and the
    persisted store (CTR-0151) stay in sync. Returns the deleted ids.
    """
    deleted: list[str] = []
    for sub in await _live_subscriptions_for(resource):
        sub_id = sub.get("id")
        if not sub_id:
            continue
        try:
            await delete(sub_id)
            deleted.append(sub_id)
        except Exception as exc:
            logger.warning("Failed to purge subscription %s during replace: %s", sub_id, exc)
    return deleted


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


async def subscribe(*, resource: str = "", notification_url: str = "", replace: bool = False) -> dict[str, Any]:
    """Create a Graph subscription and persist its record.

    When ``replace`` is true, first delete any LIVE subscription whose resource matches
    (same-resource-only, PRP-0107 REPLACE-1 / UDR-0075 D14) so a resource already at its
    per-app subscription limit can be re-subscribed. When ``replace`` is false and Graph
    rejects the create because the limit is reached, raise ``SubscriptionLimitError``
    carrying the blocking live subscription(s); the management API maps it to a
    recoverable 409 rather than an opaque 502.
    """
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
    if replace:
        purged = await _purge_resource_subscriptions(res)
        if purged:
            logger.info("Replace: purged %d existing subscription(s) for %s", len(purged), res)
    try:
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
    except graph_client.GraphApiError as exc:
        if _is_limit_error(exc):
            existing = await _live_subscriptions_for(res)
            raise SubscriptionLimitError(res, existing) from exc
        raise
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


def maintenance_schedule() -> dict[str, Any]:
    """Report the managed auto-renewal Cron job state (PRP-0107, UDR-0075 D15).

    The managed job (``cron_webhook_maintain``, D8 as amended) auto-renews subscriptions
    while CRON_ENABLED. This surfaces its state to the Webhook portal so operators can see
    and re-sync the schedule where they manage subscriptions. Never raises.
    """
    job: dict[str, Any] | None = None
    try:
        from app.cron import store as cron_store

        job = cron_store.get_job(MAINTENANCE_JOB_ID)
    except Exception:
        logger.warning("failed to read the webhook maintenance Cron job", exc_info=True)
    interval_seconds = int((job.get("schedule") or {}).get("interval_seconds") or 0) if job else 0
    return {
        "job_id": MAINTENANCE_JOB_ID,
        "exists": job is not None,
        "cron_enabled": bool(settings.cron_enabled),
        "enabled": bool(job.get("enabled")) if job else False,
        "interval_hours": round(interval_seconds / 3600, 2) if interval_seconds else None,
        "next_run_at": job.get("next_run_at") if job else None,
        "last_run_at": job.get("last_run_at") if job else None,
        "last_status": job.get("last_status") if job else None,
        "subscription_count": len(store.list_subscriptions(SOURCE_NAME)),
    }


def resync_maintenance_job() -> dict[str, Any]:
    """Remove then recreate the managed maintenance Cron job (idempotent, PRP-0107 / D15).

    Implements the operator's "confirm existing -> delete -> recreate" intent from the
    Webhook portal. The recreate happens only when there is at least one subscription AND
    CRON_ENABLED (the same rule ``sync_maintenance_job`` enforces); otherwise the job stays
    removed. Returns the resulting schedule state.
    """
    try:
        from app.cron import store as cron_store

        cron_store.delete_job(MAINTENANCE_JOB_ID)
    except Exception:
        logger.warning("failed to delete the webhook maintenance Cron job during re-sync", exc_info=True)
    sync_maintenance_job()
    return maintenance_schedule()


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
    "SubscriptionLimitError",
    "delete",
    "list_all",
    "maintain",
    "maintenance_schedule",
    "renew",
    "resync_maintenance_job",
    "subscribe",
    "sync_maintenance_job",
    "token_health",
    "validate",
]
