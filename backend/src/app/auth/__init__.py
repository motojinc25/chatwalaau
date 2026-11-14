"""API Authentication Dependency (CTR-0083 v7, PRP-0045, PRP-0057, PRP-0253).

Shared FastAPI dependencies for authentication across REST endpoints, the AG-UI SSE
endpoint, the OpenAI Responses API and key management.

Since PRP-0253 (UDR-0234) API callers present MANAGED keys from the API Key Store
(CTR-0282, ``API_KEYS_FILE``). ``API_KEY`` and ``RESPONSES_API_KEY`` are no longer read.
Each key carries one scope, ``responses`` < ``api`` < ``admin``:

- ``verify_api_key_strict`` (``/v1/responses``, CTR-0057): a Bearer key of any scope.
  Cookies and loopback are ignored. 503 when the store holds no active key.
- ``verify_api_key`` (``/api/*``, ``/ag-ui/``): a session cookie, a Bearer key with scope
  ``api`` or ``admin``, a loopback peer, or ``APP_REQUIRE_AUTH_ON_LAN=false`` open mode.
- ``verify_api_key_no_loopback`` (uploads): a session cookie or a Bearer ``api`` / ``admin``
  key while web sign-in is on; otherwise ``verify_api_key``.
- ``authorize_websocket``: the ``verify_api_key`` rules, the key via ``?api_key=``.
- ``verify_key_admin`` (key management, CTR-0283): a session cookie, a loopback peer or a
  Bearer ``admin`` key. Open mode does NOT grant it (UDR-0234 D7).

An expired, disabled or unknown key gets 401; a key whose scope is too narrow gets 403
(UDR-0234 D6). A session or loopback identity counts as ``admin``.

History:
    - PRP-0045 (v0.43.0) introduced the unified Bearer check.
    - PRP-0057 (v0.54.0) added the cookie session lane for the SPA.
    - PRP-0252 (v0.214.0) added RESPONSES_API_KEY; PRP-0253 (v0.215.0) replaced both keys
      with managed, scoped keys.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any, Literal

# NOTE: ``Request`` is kept as a runtime import (not under TYPE_CHECKING)
# because FastAPI's dependency-injection machinery resolves the annotation
# on ``verify_api_key`` / ``verify_api_key_strict`` at route-registration
# time via ``get_type_hints``. Moving it under TYPE_CHECKING makes FastAPI
# fall back to treating ``request`` as a body parameter, which causes a
# 422 on every authenticated write endpoint.
from fastapi import HTTPException, Request

from app.api_keys import store as key_store
from app.core.config import settings

# ---- Detail strings (kept stable for caller-visible 503 / 401 / 403 payloads) ----

_MISSING_AUTH_DETAIL = (
    "Client is on a non-loopback address but no authentication method is "
    "configured. Create an API key (API Keys screen or `chatwalaau keys create`) or set "
    "AUTH_USERNAME + AUTH_PASSWORD_HASH (browser ID/PW) in .env, or set "
    "APP_REQUIRE_AUTH_ON_LAN=false to acknowledge unauthenticated LAN exposure."
)
_MISSING_API_KEY_STRICT_DETAIL = (
    "API is not configured. Create an API key (API Keys screen or `chatwalaau keys create`) to enable "
    "external-app access."
)
_MISSING_CREDENTIAL_DETAIL = (
    "Missing or invalid credential. Expected: Authorization: Bearer <token>, "
    "or a valid session cookie (after POST /api/auth/login)."
)
_MISSING_HEADER_DETAIL_STRICT = "Missing or invalid Authorization header. Expected: Bearer <token>"
_INVALID_KEY_DETAIL = "Invalid API key."
_EXPIRED_KEY_DETAIL = "API key expired."
_DISABLED_KEY_DETAIL = "API key disabled."
_SCOPE_DETAIL = "This key's scope does not allow this endpoint."
_ADMIN_DETAIL = "Key management needs a signed-in session, this machine, or an admin API key."

# Starlette TestClient uses "testclient" as the client host in its ASGI
# scope. Treat it as loopback so in-process tests do not need to inject
# Bearer headers.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "testclient"})


@dataclass(frozen=True)
class Identity:
    """Authenticated principal surfaced by the auth dependency.

    ``subject`` is ``"local"`` for loopback / open-mode requests, the key NAME for a
    managed key, and the configured ``AUTH_USERNAME`` for cookie-matched requests.
    ``source`` distinguishes the lanes. ``scope`` is the key's scope; a session or
    loopback identity is ``admin`` (UDR-0234 D2). ``key_id`` is set for key matches.
    """

    subject: str = "local"
    source: Literal["loopback", "api-key", "session"] = "loopback"
    scope: str = "admin"
    key_id: str | None = None


_LOCAL = Identity(subject="local", source="loopback")


def _key_identity(record: key_store.KeyRecord) -> Identity:
    return Identity(subject=record.name, source="api-key", scope=record.scope, key_id=record.id)


def is_client_loopback(client_host: str | None) -> bool:
    """Return True when the request's client address is a loopback peer.

    Exposed for use by ``app.auth.web_auth`` (CTR-0094 status endpoint)
    so the loopback classification rule lives in one place.
    """
    if not client_host:
        return False
    host = client_host.strip().lower()
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: Headers a reverse proxy or tunnel adds when it forwards someone else's request.
_FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip")


def is_local_request(request: Request) -> bool:
    """Return True only for a request made on this machine (CTR-0083 v5, PRP-0242, UDR-0223 D3).

    The peer must be loopback AND the request must carry no forwarding header, so a
    reverse proxy or tunnel on the same host (nginx, cloudflared) never makes a remote
    person look local. Used by the stdio MCP gate; the loopback bypass of
    ``verify_api_key`` is unchanged.
    """
    client_host = request.client.host if request.client else None
    if not is_client_loopback(client_host):
        return False
    return not any(name in request.headers for name in _FORWARDING_HEADERS)


# Backward-compat alias for the previous private name (the existing
# integration test ``test_ctr0083_api_auth.py`` patches the helper by
# this name).
_is_client_loopback = is_client_loopback


def _extract_bearer(request: Request) -> str | None:
    """Return the Bearer token from the Authorization header, or None."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header[len("Bearer ") :]


def _extract_session_cookie(request: Request) -> str | None:
    """Return the configured session cookie value, or None."""
    name = getattr(settings, "auth_cookie_name", "")
    if not name:
        return None
    return request.cookies.get(name)


async def _try_session_lane(request: Request) -> Identity | None:
    """Attempt the cookie session lane. Returns Identity on match, else None.

    Defensively reads via ``getattr`` so test suites that patch
    ``app.auth.settings`` with a minimal stand-in (``SimpleNamespace``
    or similar) get the "lane disabled" behavior automatically when
    they do not configure the web auth fields.
    """
    if not getattr(settings, "web_auth_enabled", False):
        return None
    token_value = _extract_session_cookie(request)
    if not token_value:
        return None
    # Lazy import: app.auth.session_store imports nothing from app.auth,
    # so this is purely for cold-start latency / test isolation.
    from app.auth.session_store import get_session_store

    store = get_session_store()
    record = await store.lookup(token_value)
    if record is None:
        return None
    return Identity(subject=record.username, source="session")


def _store() -> key_store.KeyStore:
    """The live API Key Store (CTR-0282). A seam tests may patch."""
    return key_store.get_store()


def _key_failure(outcome: str) -> HTTPException:
    detail = {key_store.EXPIRED: _EXPIRED_KEY_DETAIL, key_store.DISABLED: _DISABLED_KEY_DETAIL}.get(
        outcome, _INVALID_KEY_DETAIL
    )
    return HTTPException(status_code=401, detail=detail)


def _check_key(token: str, required: str) -> tuple[Identity | None, HTTPException | None]:
    """``(identity, None)`` when ``token`` is an active key of at least ``required`` scope,
    else ``(None, the error to raise if nothing else admits the caller)``."""
    outcome, record = _store().authenticate(token)
    if record is None:
        return None, _key_failure(outcome)
    if not key_store.scope_allows(record.scope, required):
        return None, HTTPException(status_code=403, detail=_SCOPE_DETAIL)
    return _key_identity(record), None


async def verify_api_key(request: Request) -> Identity:
    """FastAPI dependency: validate the caller on write / SPA endpoints (CTR-0083 v7).

    Decision order:

    1. Valid session cookie -> Identity(source="session").
    2. Bearer key with scope ``api`` or ``admin`` -> Identity(source="api-key").
    3. Loopback peer -> Identity(source="loopback") (unchanged; out of PRP-0253's scope).
    4. ``APP_REQUIRE_AUTH_ON_LAN=false`` -> open mode.
    5. No active key and no web sign-in -> 503.
    6. Otherwise: the Bearer's failure (401 expired / disabled / invalid, 403 scope), or
       401 for a missing credential.
    """
    session_identity = await _try_session_lane(request)
    if session_identity is not None:
        return session_identity

    bearer = _extract_bearer(request)
    failure: HTTPException | None = None
    if bearer is not None:
        identity, failure = _check_key(bearer, "api")
        if identity is not None:
            return identity

    client_host = request.client.host if request.client else None
    if is_client_loopback(client_host):
        return _LOCAL

    if not settings.app_require_auth_on_lan:
        return _LOCAL

    web_enabled = getattr(settings, "web_auth_enabled", False)
    if not _store().has_active() and not web_enabled:
        raise HTTPException(status_code=503, detail=_MISSING_AUTH_DETAIL)

    if failure is not None:
        raise failure
    if web_enabled:
        raise HTTPException(status_code=401, detail=_MISSING_CREDENTIAL_DETAIL)
    raise HTTPException(status_code=401, detail=_MISSING_HEADER_DETAIL_STRICT)


async def verify_api_key_no_loopback(request: Request) -> Identity:
    """``verify_api_key`` WITHOUT the loopback bypass when web login is enabled
    (CTR-0022, UDR-0097 D5).

    A directly-opened ``/api/uploads/...`` URL bypasses the SPA AuthGuard, and behind a
    dev / reverse proxy every request reaches the backend from the proxy's loopback
    address, so the normal loopback bypass would leave uploaded images openable by anyone
    once ``AUTH_USERNAME`` is set.

    - Web login configured: require a valid session cookie or a Bearer ``api`` / ``admin``
      key; 401 (or 403 for a narrower scope) otherwise.
    - Web login NOT configured: defer to ``verify_api_key``.
    """
    if not settings.web_auth_enabled:
        return await verify_api_key(request)

    session_identity = await _try_session_lane(request)
    if session_identity is not None:
        return session_identity

    bearer = _extract_bearer(request)
    if bearer is not None:
        identity, failure = _check_key(bearer, "api")
        if identity is not None:
            return identity
        if failure is not None and failure.status_code == 403:
            raise failure

    raise HTTPException(status_code=401, detail=_MISSING_CREDENTIAL_DETAIL)


async def authorize_websocket(websocket: Any) -> bool:
    """Authorize a WebSocket handshake (CTR-0110), mirroring ``verify_api_key``.

    The browser WebSocket API cannot set an Authorization header, so a key is accepted
    via the ``api_key`` query parameter (scope ``api`` or ``admin``); the session-cookie
    lane works for same-origin handshakes; loopback peers bypass; and
    ``APP_REQUIRE_AUTH_ON_LAN=false`` opens it. Returns True to accept. Never raises.
    """
    name = getattr(settings, "auth_cookie_name", "")
    if getattr(settings, "web_auth_enabled", False) and name:
        token_value = websocket.cookies.get(name)
        if token_value:
            from app.auth.session_store import get_session_store

            record = await get_session_store().lookup(token_value)
            if record is not None:
                return True
    qp_key = websocket.query_params.get("api_key")
    if qp_key:
        try:
            identity, _ = _check_key(qp_key, "api")
        except Exception:
            identity = None
        if identity is not None:
            return True
    client_host = websocket.client.host if websocket.client else None
    if is_client_loopback(client_host):
        return True
    return not settings.app_require_auth_on_lan


async def verify_api_key_strict(request: Request) -> Identity:
    """FastAPI dependency: Bearer-only variant for ``/v1/responses`` (CTR-0057).

    Any active key passes: ``responses``, ``api`` and ``admin`` all cover it. Session
    cookies and loopback are ignored.

    - No active key in the store -> 503.
    - Missing Bearer -> 401; expired / disabled / unknown key -> 401.
    """
    if not _store().has_active():
        raise HTTPException(status_code=503, detail=_MISSING_API_KEY_STRICT_DETAIL)
    bearer = _extract_bearer(request)
    if bearer is None:
        raise HTTPException(status_code=401, detail=_MISSING_HEADER_DETAIL_STRICT)
    identity, failure = _check_key(bearer, "responses")
    if identity is None:
        raise failure or HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)
    return identity


async def verify_key_admin(request: Request) -> Identity:
    """FastAPI dependency for key management (CTR-0283, UDR-0234 D7).

    Accepts a session cookie, a loopback peer or a Bearer ``admin`` key. Open mode
    (``APP_REQUIRE_AUTH_ON_LAN=false``) does NOT grant it.
    """
    session_identity = await _try_session_lane(request)
    if session_identity is not None:
        return session_identity

    bearer = _extract_bearer(request)
    failure: HTTPException | None = None
    if bearer is not None:
        identity, failure = _check_key(bearer, "admin")
        if identity is not None:
            return identity

    client_host = request.client.host if request.client else None
    if is_client_loopback(client_host):
        return _LOCAL

    if failure is not None:
        raise failure
    raise HTTPException(status_code=401, detail=_ADMIN_DETAIL)


__all__ = [
    "Identity",
    "authorize_websocket",
    "is_client_loopback",
    "is_local_request",
    "verify_api_key",
    "verify_api_key_strict",
    "verify_key_admin",
]
