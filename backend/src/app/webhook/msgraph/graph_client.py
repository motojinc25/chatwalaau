"""Microsoft Graph App-Only Client (CTR-0153, PRP-0097, UDR-0075 D6/D13).

The single Microsoft Graph boundary for the webhook gateway. App-only
(client-credentials) token acquisition from the dedicated ``GRAPH_*`` namespace, and
the small set of Graph REST calls the gateway and the meeting pipeline need:

- create / renew / delete / list change-notification subscriptions (CTR-0152),
- resolve an online meeting (by meeting id or joinWebUrl),
- fetch transcript content (CTR-0156).

Implemented with ``httpx`` only (no heavy Graph SDK, UDR-0075 D13). Credentials are
kept SEPARATE from the CAP-009 ``TEAMS_*`` Bot Framework credentials (UDR-0075 D6).
PLAIN notifications + fetch (UDR-0075 D5): subscriptions carry no encrypted payload.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_TOKEN_SCOPE = "https://graph.microsoft.com/.default"
_TIMEOUT = 30.0


class GraphConfigError(RuntimeError):
    """Raised when GRAPH_* credentials are not configured."""


class GraphApiError(RuntimeError):
    """Raised on a non-2xx Graph response; carries the status + body for diagnostics."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Graph API error {status_code}: {body[:500]}")


# Cached app-only token (process-local).
_token: str | None = None
_token_expires_at: datetime | None = None
_token_lock = asyncio.Lock()


def is_configured() -> bool:
    """True when the GRAPH_* app-only credentials are all present."""
    return bool(settings.graph_tenant_id and settings.graph_client_id and settings.graph_client_secret)


def _require_configured() -> None:
    if not is_configured():
        msg = "Microsoft Graph app-only credentials are not configured (GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET)."
        raise GraphConfigError(msg)


async def get_token(*, force: bool = False) -> str:
    """Return a valid app-only access token, refreshing when near expiry."""
    global _token, _token_expires_at
    _require_configured()
    async with _token_lock:
        now = datetime.now(UTC)
        if not force and _token and _token_expires_at and _token_expires_at > now + timedelta(seconds=60):
            return _token
        url = f"https://login.microsoftonline.com/{settings.graph_tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": settings.graph_client_id,
            "client_secret": settings.graph_client_secret,
            "scope": _TOKEN_SCOPE,
            "grant_type": "client_credentials",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, data=data)
        if resp.status_code != 200:
            raise GraphApiError(resp.status_code, resp.text)
        payload = resp.json()
        _token = payload["access_token"]
        _token_expires_at = now + timedelta(seconds=int(payload.get("expires_in", 3600)))
        logger.info("Acquired Microsoft Graph app-only token (expires in %ss)", payload.get("expires_in"))
        return _token


def _graph_url(path: str) -> str:
    base = (settings.graph_base_url or "https://graph.microsoft.com/v1.0").rstrip("/")
    return path if path.startswith("http") else f"{base}/{path.lstrip('/')}"


async def _request(method: str, path: str, *, json_body: dict | None = None) -> dict[str, Any]:
    """Issue an authenticated Graph request; raises GraphApiError on non-2xx."""
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, _graph_url(path), headers=headers, json=json_body)
    if resp.status_code == 401:
        # One retry with a forced token refresh (token may have just rotated).
        token = await get_token(force=True)
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, _graph_url(path), headers=headers, json=json_body)
    if resp.status_code >= 300:
        raise GraphApiError(resp.status_code, resp.text)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text}


async def get(path: str) -> dict[str, Any]:
    """GET a Graph resource (JSON)."""
    return await _request("GET", path)


async def get_text(path: str) -> str:
    """GET a Graph resource as raw text (e.g. a VTT transcript)."""
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_graph_url(path), headers=headers)
    if resp.status_code >= 300:
        raise GraphApiError(resp.status_code, resp.text)
    return resp.text


# --- Subscriptions (CTR-0152) ---


async def create_subscription(
    *,
    resource: str,
    change_type: str,
    notification_url: str,
    client_state: str,
    expiration: datetime,
    lifecycle_notification_url: str = "",
) -> dict[str, Any]:
    """Create a Graph change-notification subscription.

    For rich resources such as ``communications/onlineMeetings/getAllTranscripts``, Graph
    REQUIRES a ``lifecycleNotificationUrl`` whenever the expiration is more than 1 hour
    out (it delivers reauthorizationRequired / subscriptionRemoved / missed events there).
    """
    body: dict[str, Any] = {
        "changeType": change_type,
        "notificationUrl": notification_url,
        "resource": resource,
        "expirationDateTime": expiration.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "clientState": client_state,
    }
    if lifecycle_notification_url:
        body["lifecycleNotificationUrl"] = lifecycle_notification_url
    return await _request("POST", "subscriptions", json_body=body)


async def renew_subscription(sub_id: str, *, expiration: datetime) -> dict[str, Any]:
    """Extend a subscription's expiration."""
    body = {"expirationDateTime": expiration.astimezone(UTC).isoformat().replace("+00:00", "Z")}
    return await _request("PATCH", f"subscriptions/{sub_id}", json_body=body)


async def delete_subscription(sub_id: str) -> None:
    """Delete a subscription."""
    await _request("DELETE", f"subscriptions/{sub_id}")


async def list_subscriptions() -> list[dict[str, Any]]:
    """List all subscriptions visible to this app."""
    data = await get("subscriptions")
    value = data.get("value")
    return value if isinstance(value, list) else []


# --- Meeting / transcript resolution (CTR-0156) ---


async def resolve_online_meeting(*, organizer_id: str, meeting_id: str = "", join_web_url: str = "") -> dict[str, Any]:
    """Resolve an online meeting by id or joinWebUrl, app-only (UDR-0076 D5).

    App-only online-meeting access is ORGANIZER-SCOPED: the request MUST go through
    ``/users/{organizerId}/onlineMeetings/...`` and the organizer must be covered by the
    tenant's Application Access Policy (UDR-0076 D9). The tenant-wide
    ``communications/onlineMeetings/{id}`` path is NOT valid for an app-only GET (it
    returns 400 BadRequest). ``organizer_id`` is the organizer's AAD object id or UPN.
    """
    if not organizer_id:
        msg = "App-only meeting resolution requires the organizer's user id (organizer_id)."
        raise GraphApiError(400, msg)
    if meeting_id:
        return await get(f"users/{organizer_id}/onlineMeetings/{meeting_id}")
    if join_web_url:
        from urllib.parse import quote

        flt = quote(f"JoinWebUrl eq '{join_web_url}'", safe="")
        data = await get(f"users/{organizer_id}/onlineMeetings?$filter={flt}")
        value = data.get("value") or []
        if not value:
            msg = "No online meeting found for the given joinWebUrl under that organizer."
            raise GraphApiError(404, msg)
        return value[0]
    msg = "resolve_online_meeting requires meeting_id or join_web_url."
    raise GraphApiError(400, msg)


async def token_health() -> dict[str, Any]:
    """Probe app-only credentials + Graph reachability (CTR-0152 token-health).

    Returns a structured health report; never raises (so the UI can render the
    failure mode, e.g. a missing Application Access Policy, UDR-0076 D9).
    """
    if not is_configured():
        return {"ok": False, "configured": False, "error": "GRAPH_* credentials are not set."}
    report: dict[str, Any] = {"ok": False, "configured": True}
    try:
        await get_token(force=True)
        report["token"] = "ok"
    except Exception as exc:
        report["token"] = "failed"
        report["error"] = str(exc)
        return report
    try:
        subs = await list_subscriptions()
        report["subscriptions_reachable"] = True
        report["subscription_count"] = len(subs)
        report["ok"] = True
    except Exception as exc:
        report["subscriptions_reachable"] = False
        report["error"] = str(exc)
    return report


def _reset_token_cache_for_test() -> None:
    global _token, _token_expires_at
    _token = None
    _token_expires_at = None


__all__ = [
    "GraphApiError",
    "GraphConfigError",
    "create_subscription",
    "delete_subscription",
    "get",
    "get_text",
    "get_token",
    "is_configured",
    "list_subscriptions",
    "renew_subscription",
    "resolve_online_meeting",
    "token_health",
]
