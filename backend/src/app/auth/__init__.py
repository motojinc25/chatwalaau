"""API Authentication Dependency (CTR-0083, PRP-0045, PRP-0051, PRP-0057).

Shared FastAPI dependencies for authentication across REST endpoints,
the AG-UI SSE endpoint, and the OpenAI Responses API.

Two dependencies are exposed:

- ``verify_api_key`` -- write endpoints consumed by the SPA, the CLI
  client, and AG-UI (CTR-0009). Accepts EITHER a Bearer ``API_KEY``
  header OR a valid session cookie (PRP-0057). Loopback clients
  (``127.0.0.1``, ``::1``, ``localhost``, or the Starlette TestClient
  sentinel) pass through without any credential. Non-loopback clients
  require Bearer or cookie when ``APP_REQUIRE_AUTH_ON_LAN`` is
  ``true`` (the default).
- ``verify_api_key_strict`` -- external-app entry points such as
  ``/v1/responses`` (CTR-0057). Bearer ``API_KEY`` only; cookies are
  intentionally ignored regardless of ``AUTH_USERNAME``.

History:
    - PRP-0045 (v0.43.0) introduced the unified Bearer check.
    - PRP-0048 (v0.46.0) added the AuthProvider Protocol seam.
    - PRP-0051 (v0.49.0) reverted to an inline Bearer check after the
      commercial line was retired.
    - PRP-0057 (v0.54.0) added the cookie session lane for the SPA
      while keeping the strict variant unchanged. The two lanes are
      branches inside this module; no Protocol seam is reintroduced
      (per UDR-0033 boundary).
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Literal

# NOTE: ``Request`` is kept as a runtime import (not under TYPE_CHECKING)
# because FastAPI's dependency-injection machinery resolves the annotation
# on ``verify_api_key`` / ``verify_api_key_strict`` at route-registration
# time via ``get_type_hints``. Moving it under TYPE_CHECKING makes FastAPI
# fall back to treating ``request`` as a body parameter, which causes a
# 422 on every authenticated write endpoint.
from fastapi import HTTPException, Request

from app.core.config import settings

# ---- Detail strings (kept stable for caller-visible 503 / 401 payloads) ----

_MISSING_AUTH_DETAIL = (
    "Client is on a non-loopback address but no authentication method is "
    "configured. Set API_KEY (CLI/external) or AUTH_USERNAME + "
    "AUTH_PASSWORD_HASH (browser ID/PW) in .env, or set "
    "APP_REQUIRE_AUTH_ON_LAN=false to acknowledge unauthenticated LAN exposure."
)
_MISSING_API_KEY_STRICT_DETAIL = "API is not configured. Set API_KEY in .env to enable external-app access."
_MISSING_CREDENTIAL_DETAIL = (
    "Missing or invalid credential. Expected: Authorization: Bearer <token>, "
    "or a valid session cookie (after POST /api/auth/login)."
)
_MISSING_HEADER_DETAIL_STRICT = "Missing or invalid Authorization header. Expected: Bearer <token>"
_INVALID_KEY_DETAIL = "Invalid API key."

# Starlette TestClient uses "testclient" as the client host in its ASGI
# scope. Treat it as loopback so in-process tests do not need to inject
# Bearer headers.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "testclient"})


@dataclass(frozen=True)
class Identity:
    """Authenticated principal surfaced by the auth dependency.

    ``subject`` is ``"local"`` for loopback / open-mode requests,
    ``"api-key"`` for Bearer-matched requests, and the configured
    ``AUTH_USERNAME`` for cookie-matched requests (PRP-0057).
    ``source`` distinguishes the three lanes so downstream code can
    log it without re-deriving from headers.
    """

    subject: str = "local"
    source: Literal["loopback", "api-key", "session"] = "loopback"


_LOCAL = Identity(subject="local", source="loopback")
_VIA_API_KEY = Identity(subject="api-key", source="api-key")


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


async def verify_api_key(request: Request) -> Identity:
    """FastAPI dependency: validate the caller on write / SPA endpoints.

    Decision order (CTR-0083 v4.1, amended by PRP-0057 follow-up
    on 2026-05-16):

    1. Valid session cookie -> Identity(source="session"). Tried first so
       an authenticated loopback browser reports the correct username
       instead of falling into the loopback bypass branch.
    2. Matching Bearer ``API_KEY`` -> Identity(source="api-key").
    3. Loopback peer with no credential / failed credential ->
       Identity(source="loopback"). Preserved so CLI tools and
       ``curl localhost`` continue to work without configuration.
    4. ``APP_REQUIRE_AUTH_ON_LAN=false`` -> open mode.
    5. Neither method configured -> 503.
    6. Method(s) configured but no valid credential -> 401.
    """
    # 1. Try cookie session lane first (cheap dict lookup if enabled).
    #    Cookie wins over loopback bypass so /api/auth/me reports the
    #    real username instead of the "local" sentinel.
    session_identity = await _try_session_lane(request)
    if session_identity is not None:
        return session_identity

    # 2. Try Bearer.
    bearer = _extract_bearer(request)
    api_key_configured = bool(settings.api_key)
    if bearer is not None and api_key_configured and bearer == settings.api_key:
        return _VIA_API_KEY

    # 3. Loopback bypass for credential-less / failed-credential callers.
    #    This is what keeps "chatwalaau chat" without API_KEY working on
    #    localhost even when the operator has enabled AUTH_USERNAME for
    #    the SPA. Browsers visiting /chat hit the AuthGuard via the
    #    /api/auth/status flow before any /api/* request, so they are
    #    redirected to /login regardless of this bypass.
    client_host = request.client.host if request.client else None
    if is_client_loopback(client_host):
        return _LOCAL

    # 4. Operator-acknowledged open mode.
    if not settings.app_require_auth_on_lan:
        return _LOCAL

    # 5. Nothing configured.
    web_enabled = getattr(settings, "web_auth_enabled", False)
    if not api_key_configured and not web_enabled:
        raise HTTPException(status_code=503, detail=_MISSING_AUTH_DETAIL)

    # 6. Method(s) configured but no valid credential presented.
    if web_enabled:
        raise HTTPException(status_code=401, detail=_MISSING_CREDENTIAL_DETAIL)
    if bearer is None:
        raise HTTPException(status_code=401, detail=_MISSING_HEADER_DETAIL_STRICT)
    raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)


async def verify_api_key_strict(request: Request) -> Identity:
    """FastAPI dependency: Bearer-only variant for external-app paths.

    Used by the OpenAI-compatible Responses API (``/v1/responses``,
    CTR-0057). Session cookies are intentionally ignored on this
    endpoint -- external-app callers speak Bearer auth only.

    - ``API_KEY`` empty -> 503.
    - ``API_KEY`` set -> require ``Authorization: Bearer <API_KEY>``;
      401 on missing or mismatched header.
    """
    if not settings.api_key:
        raise HTTPException(status_code=503, detail=_MISSING_API_KEY_STRICT_DETAIL)
    bearer = _extract_bearer(request)
    if bearer is None:
        raise HTTPException(status_code=401, detail=_MISSING_HEADER_DETAIL_STRICT)
    if bearer != settings.api_key:
        raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)
    return _VIA_API_KEY


__all__ = [
    "Identity",
    "is_client_loopback",
    "verify_api_key",
    "verify_api_key_strict",
]
