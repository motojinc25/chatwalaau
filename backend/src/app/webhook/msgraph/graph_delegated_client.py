"""Microsoft Graph User-Delegated Client (CTR-0158, PRP-0098, UDR-0077).

A sibling of the app-only client (CTR-0153) for the manual, user-delegated ("Dedicated")
Teams meeting pipeline (FEAT-0054). Where CTR-0153 reads transcripts app-only and
ORGANIZER-SCOPED (``/users/{organizerId}/onlineMeetings/...``, gated by a Teams
Application Access Policy), this client authenticates the SIGNED-IN USER via the OAuth
2.0 Device Authorization Grant (Device Code Flow) and reads USER-SCOPED
(``/me/onlineMeetings/...``) -- so it needs neither a tenant-wide Application permission
nor an Application Access Policy (UDR-0077 D4/D5).

Design constraints (UDR-0077):

- D3: the user-delegated access token is held in PROCESS MEMORY only, for the single job
  run, and is then discarded. NO refresh token is requested (``offline_access`` is NOT in
  the scope set) and NOTHING is persisted -- not to disk, not to the CTR-0145 job store.
- D5: credentials REUSE the existing ``GRAPH_*`` namespace -- ``GRAPH_CLIENT_ID`` +
  ``GRAPH_TENANT_ID`` for the public-client device-code flow. NO new env var, NO client
  secret. The app registration must enable "Allow public client flows".
- D13 (inherited from UDR-0075): ``httpx`` only, no heavy Graph SDK.

Graph transcript access is organizer-scoped even under delegated permissions, so the
Dedicated lane summarizes meetings the signed-in user ORGANIZED (UDR-0077 D4).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings

# Reuse the app-only client's error types so the meeting pipeline's existing
# 404-means-not-ready-yet retry logic works identically for either lane.
from app.webhook.msgraph.graph_client import GraphApiError, GraphConfigError

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# Delegated scopes. Meeting resolution by JoinWebUrl needs OnlineMeetings.Read; transcript
# content needs OnlineMeetingTranscript.Read.All. ``offline_access`` is deliberately
# OMITTED so Entra returns NO refresh token (UDR-0077 D3).
_SCOPES = (
    "https://graph.microsoft.com/OnlineMeetings.Read "
    "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All"
)

_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def is_configured() -> bool:
    """True when the public-client device-code prerequisites are present.

    Only the client id + tenant id are required (no client secret) -- the device-code flow
    is a PUBLIC client flow (UDR-0077 D5).
    """
    return bool(settings.graph_tenant_id and settings.graph_client_id)


def _require_configured() -> None:
    if not is_configured():
        msg = (
            "Microsoft Graph delegated credentials are not configured "
            "(GRAPH_TENANT_ID / GRAPH_CLIENT_ID)."
        )
        raise GraphConfigError(msg)


def _authority(path: str) -> str:
    tenant = settings.graph_tenant_id
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{path}"


def _graph_url(path: str) -> str:
    base = (settings.graph_base_url or "https://graph.microsoft.com/v1.0").rstrip("/")
    return path if path.startswith("http") else f"{base}/{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Device Code Flow (UDR-0077 D3)
# ---------------------------------------------------------------------------


async def start_device_code() -> dict[str, Any]:
    """Begin a device-code flow; returns the user prompt + the polling handle.

    Returns the Entra ``devicecode`` payload: ``device_code`` (server poll handle),
    ``user_code`` + ``verification_uri`` (shown to the user), ``expires_in``, ``interval``,
    and ``message``. Raises GraphConfigError when GRAPH_* is unset, GraphApiError on a
    non-2xx response.
    """
    _require_configured()
    data = {"client_id": settings.graph_client_id, "scope": _SCOPES}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_authority("devicecode"), data=data)
    if resp.status_code != 200:
        raise GraphApiError(resp.status_code, resp.text)
    return resp.json()


async def poll_for_token(device_code: str, *, interval: int, expires_in: int) -> str:
    """Poll the token endpoint until the user authenticates; return the access token.

    Handles the standard device-code token-endpoint responses (UDR-0077 IMPL-2):
    ``authorization_pending`` (keep waiting), ``slow_down`` (increase the interval),
    ``expired_token`` / ``authorization_declined`` / ``access_denied`` (terminal failure).
    The access token is returned to the caller and held in memory only; no refresh token
    is requested or stored.
    """
    _require_configured()
    poll = max(1, int(interval))
    deadline = time.monotonic() + max(1, int(expires_in))
    data = {
        "client_id": settings.graph_client_id,
        "grant_type": _DEVICE_CODE_GRANT,
        "device_code": device_code,
    }
    while True:
        if time.monotonic() >= deadline:
            raise GraphApiError(400, "device code expired before authentication completed")
        await asyncio.sleep(poll)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_authority("token"), data=data)
        if resp.status_code == 200:
            payload = resp.json()
            logger.info("Acquired Microsoft Graph user-delegated token (device-code, expires in %ss)", payload.get("expires_in"))
            return payload["access_token"]
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        err = str(body.get("error", ""))
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            poll += 5
            continue
        # Terminal failures: expired_token, authorization_declined, access_denied, bad_verification_code, etc.
        raise GraphApiError(resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# User-scoped Graph reads (UDR-0077 D4): /me/onlineMeetings/...
# ---------------------------------------------------------------------------


async def _request(token: str, method: str, path: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, _graph_url(path), headers=headers)
    if resp.status_code >= 300:
        raise GraphApiError(resp.status_code, resp.text)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text}


async def get_me(token: str, path: str) -> dict[str, Any]:
    """GET a user-scoped Graph resource (JSON) with a delegated token."""
    return await _request(token, "GET", path)


async def get_text_me(token: str, path: str) -> str:
    """GET a user-scoped Graph resource as raw text (e.g. a VTT transcript)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_graph_url(path), headers=headers)
    if resp.status_code >= 300:
        raise GraphApiError(resp.status_code, resp.text)
    return resp.text


async def resolve_online_meeting_me(token: str, *, meeting_id: str = "", join_web_url: str = "") -> dict[str, Any]:
    """Resolve an online meeting USER-SCOPED via /me/onlineMeetings (UDR-0077 D4).

    The signed-in user is implicitly the organizer context, so no organizer id is needed.
    By meeting id: ``/me/onlineMeetings/{id}``. By join URL: a ``JoinWebUrl`` ``$filter``.
    """
    if meeting_id:
        return await get_me(token, f"me/onlineMeetings/{meeting_id}")
    if join_web_url:
        flt = quote(f"JoinWebUrl eq '{join_web_url}'", safe="")
        data = await get_me(token, f"me/onlineMeetings?$filter={flt}")
        value = data.get("value") or []
        if not value:
            msg = "No online meeting found for the given joinWebUrl under your account (did you organize it?)."
            raise GraphApiError(404, msg)
        return value[0]
    msg = "resolve_online_meeting_me requires meeting_id or join_web_url."
    raise GraphApiError(400, msg)


# ---------------------------------------------------------------------------
# Process-local run-token map (UDR-0077 D3): the delegated token NEVER lands in
# the persisted job params; the job carries an opaque token_ref instead.
# ---------------------------------------------------------------------------

_run_tokens: dict[str, str] = {}
_run_tokens_lock = asyncio.Lock()


async def put_run_token(ref: str, token: str) -> None:
    """Stash a delegated access token under an opaque ref for one job run (memory only)."""
    async with _run_tokens_lock:
        _run_tokens[ref] = token


async def take_run_token(ref: str) -> str | None:
    """Pop the delegated token for a job run (one-shot). Returns None if absent/consumed."""
    async with _run_tokens_lock:
        return _run_tokens.pop(ref, None)


def _clear_run_tokens_for_test() -> None:
    _run_tokens.clear()


__all__ = [
    "GraphApiError",
    "GraphConfigError",
    "get_me",
    "get_text_me",
    "is_configured",
    "poll_for_token",
    "put_run_token",
    "resolve_online_meeting_me",
    "start_device_code",
    "take_run_token",
]
