"""API Authentication Dependency (CTR-0083, PRP-0045, PRP-0051).

Shared FastAPI dependencies for Bearer ``API_KEY`` validation across
authenticated REST endpoints and the AG-UI SSE endpoint.

Two dependencies are exposed:

- ``verify_api_key`` -- write endpoints consumed by the SPA, the CLI
  client, and AG-UI (CTR-0009). Loopback clients (``127.0.0.1``,
  ``::1``, ``localhost``, or the Starlette TestClient sentinel) pass
  through without a Bearer header. Non-loopback clients require
  ``Authorization: Bearer <API_KEY>`` when ``APP_REQUIRE_AUTH_ON_LAN``
  is ``true`` (the default).
- ``verify_api_key_strict`` -- external-app entry points such as
  ``/v1/responses`` (CTR-0057), where the Bearer token is the
  caller's identity. Always requires the header, regardless of client
  address.

History:
    - PRP-0045 (v0.43.0) introduced the unified Bearer check.
    - PRP-0048 (v0.46.0) rewrote both functions to delegate to an
      ``AuthProvider`` Protocol so the commercial ``wedxchatci``
      package could register MSAL.
    - PRP-0051 (v0.49.0) reverted the delegation when the WeDXChatCi
      commercial line was retired. The implementation is once again
      inline; the public surface (these two callables and ``Identity``)
      is unchanged from a caller's view.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress

# NOTE: ``Request`` is kept as a runtime import (not under TYPE_CHECKING)
# because FastAPI's dependency-injection machinery resolves the annotation
# on ``verify_api_key`` / ``verify_api_key_strict`` at route-registration
# time via ``get_type_hints``. Moving it under TYPE_CHECKING makes FastAPI
# fall back to treating ``request`` as a body parameter, which causes a
# 422 on every authenticated write endpoint.
from fastapi import HTTPException, Request

from app.core.config import settings

# ---- Detail strings (kept stable for caller-visible 503 / 401 payloads) ----

_MISSING_API_KEY_DETAIL = (
    "Client is on a non-loopback address but API_KEY is not configured. "
    "Set API_KEY in .env, or set APP_REQUIRE_AUTH_ON_LAN=false to acknowledge "
    "unauthenticated LAN exposure."
)
_MISSING_API_KEY_STRICT_DETAIL = "API is not configured. Set API_KEY in .env to enable external-app access."
_MISSING_HEADER_DETAIL = "Missing or invalid Authorization header. Expected: Bearer <token>"
_INVALID_KEY_DETAIL = "Invalid API key."

# Starlette TestClient uses "testclient" as the client host in its ASGI
# scope. Treat it as loopback so in-process tests do not need to inject
# Bearer headers.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "testclient"})


@dataclass(frozen=True)
class Identity:
    """Authenticated principal surfaced by the auth dependency.

    The OSS inline check sets ``subject="local"`` for every accepted
    request. The dataclass is preserved (instead of returning ``None``)
    so callers can type the dependency as
    ``Annotated[Identity, Depends(verify_api_key)]`` and so the public
    surface matches the pre-PRP-0051 shape.
    """

    subject: str = "local"


_LOCAL = Identity()


def _is_client_loopback(client_host: str | None) -> bool:
    """Return True when the request's client address is a loopback peer."""
    if not client_host:
        return False
    host = client_host.strip().lower()
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_bearer_header(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail=_MISSING_HEADER_DETAIL)
    token = auth_header[len("Bearer ") :]
    if token != settings.api_key:
        raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)


async def verify_api_key(request: Request) -> Identity:
    """FastAPI dependency: validate the caller on write endpoints.

    Behavior:

    - Loopback client -> pass through (zero-config localhost).
    - Non-loopback client, ``APP_REQUIRE_AUTH_ON_LAN=false`` -> pass
      through (operator-acknowledged open mode).
    - Non-loopback client, ``APP_REQUIRE_AUTH_ON_LAN=true``,
      ``API_KEY`` empty -> 503.
    - Non-loopback client, ``APP_REQUIRE_AUTH_ON_LAN=true``,
      ``API_KEY`` set -> require ``Authorization: Bearer <API_KEY>``;
      401 on missing or mismatched header.
    """
    client_host = request.client.host if request.client else None
    if _is_client_loopback(client_host):
        return _LOCAL

    if not settings.app_require_auth_on_lan:
        return _LOCAL

    if not settings.api_key:
        raise HTTPException(status_code=503, detail=_MISSING_API_KEY_DETAIL)

    _check_bearer_header(request)
    return _LOCAL


async def verify_api_key_strict(request: Request) -> Identity:
    """FastAPI dependency: always-on variant for external-app paths.

    Used by the OpenAI-compatible Responses API (``/v1/responses``,
    CTR-0057) where the Bearer token is the caller's identity. The
    check applies regardless of client address.

    - ``API_KEY`` empty -> 503.
    - ``API_KEY`` set -> require ``Authorization: Bearer <API_KEY>``;
      401 on missing or mismatched header.
    """
    if not settings.api_key:
        raise HTTPException(status_code=503, detail=_MISSING_API_KEY_STRICT_DETAIL)
    _check_bearer_header(request)
    return _LOCAL


__all__ = [
    "Identity",
    "verify_api_key",
    "verify_api_key_strict",
]
