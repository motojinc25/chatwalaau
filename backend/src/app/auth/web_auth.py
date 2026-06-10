"""Web Authentication REST API (CTR-0094, PRP-0057).

Provides four endpoints for the SPA web auth lane:

- ``POST /api/auth/login``  (unauth) -- exchange ID/PW for an opaque session cookie
- ``POST /api/auth/logout`` (auth via CTR-0083) -- revoke the current session
- ``GET  /api/auth/me``     (auth via CTR-0083) -- inspect the current session
- ``GET  /api/auth/status`` (unauth) -- SPA bootstrap mode discovery

``POST /api/auth/login`` is the only mutating endpoint on the backend
that does NOT consume CTR-0083. Documented exception per CTR-0063 v4.
The contract itself still consumes CTR-0083 (for logout / me), so the
contract-level invariant remains green.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import Identity, is_client_loopback, verify_api_key
from app.auth.password import (
    ParsedPasswordHash,
    parse_password_hash,
    verify_password,
)
from app.auth.session_store import get_session_store
from app.core.config import settings
from app.core.version import get_app_version

_logger = logging.getLogger(__name__)

# PRP-0068 / CTR-0094 v5 / UDR-0044 D2: the running app version is resolved
# once at import time from the single shared helper and reported on
# /api/auth/status so the SPA sidebar footer can display it.
_APP_VERSION = get_app_version()

router = APIRouter(prefix="/api/auth", tags=["Web Auth"])


# ---- Module-level parsed-hash cache (CTR-0093 startup-time parse) ----


@dataclass
class _CachedHash:
    raw: str
    parsed: ParsedPasswordHash | None


_CACHED_HASH: _CachedHash | None = None
_CACHED_HASH_LOCK = asyncio.Lock()


async def _get_parsed_password_hash() -> ParsedPasswordHash | None:
    """Parse ``AUTH_PASSWORD_HASH`` lazily and memoise.

    Re-parses if the raw setting value changed (test suites mutate
    settings during the run). Returns ``None`` when the lane is
    disabled OR when the hash is empty.
    """
    raw = settings.auth_password_hash.strip()
    if not raw:
        return None
    global _CACHED_HASH
    async with _CACHED_HASH_LOCK:
        if _CACHED_HASH is not None and _CACHED_HASH.raw == raw:
            return _CACHED_HASH.parsed
        try:
            parsed = parse_password_hash(raw)
        except ValueError as exc:
            # The startup validator in CTR-0093 should have already
            # caught this; log once and treat the lane as disabled.
            _logger.error("AUTH_PASSWORD_HASH parse failed: %s", exc)
            _CACHED_HASH = _CachedHash(raw=raw, parsed=None)
            return None
        _CACHED_HASH = _CachedHash(raw=raw, parsed=parsed)
        return parsed


def _reset_password_hash_cache_for_tests() -> None:
    """Drop the cached parse. Test-only helper."""
    global _CACHED_HASH
    _CACHED_HASH = None


# ---- Cookie helpers ----


def _cookie_secure_for_request(request: Request) -> bool:
    """Resolve the Secure attribute for the response cookie."""
    mode = settings.auth_cookie_secure
    if mode == "true":
        return True
    if mode == "false":
        return False
    # "auto"
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_ttl_seconds,
        path="/",
        httponly=True,
        secure=_cookie_secure_for_request(request),
        samesite="strict",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    # ``set_cookie`` with max_age=0 expires the cookie immediately;
    # browsers match on (name, path, domain) so all three attributes
    # match the issued cookie's identity.
    response.set_cookie(
        key=settings.auth_cookie_name,
        value="",
        max_age=0,
        path="/",
        httponly=True,
        secure=_cookie_secure_for_request(request),
        samesite="strict",
    )


# ---- Request / response models ----


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=4096)


class StatusResponse(BaseModel):
    mode: Literal["open", "api-key-only", "login-required"]
    authenticated: bool
    username: str | None = None
    # PRP-0066 / CTR-0094 v3 / UDR-0041 D9: additive demo flag so the SPA
    # can render a "DEMO" badge. Always serialised; defaults to false so
    # older SPA builds that ignore the field are unaffected.
    demo_mode: bool = False
    # PRP-0067 / CTR-0094 v4 / UDR-0043 D3: report the currently active
    # tool-approval mode so the SPA can render the
    # PermissionsDisabledBanner when the value is "skip". Always
    # serialised; v3 clients that ignore the field see no behaviour change.
    tool_approval_mode: Literal["skip", "auto", "always"] = "auto"
    # PRP-0068 / CTR-0094 v5 / UDR-0044 D1: running app version (equals
    # backend/pyproject.toml [project].version). Always serialised; v4
    # clients that ignore the field see no behaviour change. The SPA
    # sidebar footer renders "v{version}" when non-empty.
    version: str = _APP_VERSION
    # PRP-0077 / CTR-0094 v6 / UDR-0053: the active Auto Session Title mode
    # ("truncate" | "llm"). The SPA uses it as a hint; the per-session
    # auto_title_pending flag (CTR-0014) is the authoritative spinner driver.
    session_title_mode: str = "truncate"


class MeResponse(BaseModel):
    authenticated: Literal[True]
    username: str
    source: Literal["loopback", "api-key", "session"]


# ---- Endpoints ----


@router.post("/login", status_code=204)
async def login(body: LoginRequest, request: Request, response: Response) -> Response:
    """Exchange ID/PW for an HttpOnly session cookie.

    503 when ``AUTH_USERNAME`` is unset. 401 on bad credentials
    (constant-time, no user enumeration).
    """
    if not settings.web_auth_enabled:
        raise HTTPException(
            status_code=503,
            detail="Login is not configured. Set AUTH_USERNAME and AUTH_PASSWORD_HASH in .env.",
        )

    parsed = await _get_parsed_password_hash()
    if parsed is None:
        # Either the hash is empty (caught above via web_auth_enabled +
        # validator) or the parse failed at runtime. Either way the
        # lane cannot complete a login; report 503 not 401 so operators
        # see a config problem distinct from bad credentials.
        raise HTTPException(
            status_code=503,
            detail="Login is not configured (AUTH_PASSWORD_HASH invalid). "
            "Regenerate the hash using assets/docs/guides/web-auth.md.",
        )

    # Always run scrypt verification, even when the username is wrong,
    # so wall-clock response time does not leak which side failed.
    username_match = body.username == settings.auth_username
    password_match = verify_password(body.password, parsed)

    if not (username_match and password_match):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    store = get_session_store()
    session = await store.create(settings.auth_username)
    _set_session_cookie(response, session.token, request)
    response.status_code = 204
    return response


@router.post("/logout", status_code=204, dependencies=[Depends(verify_api_key)])
async def logout(request: Request, response: Response) -> Response:
    """Revoke the current session token (if any) and clear the cookie.

    Idempotent: callers without an active session still receive 204
    (after passing the CTR-0083 dependency check).
    """
    token_value = request.cookies.get(settings.auth_cookie_name, "")
    if token_value:
        store = get_session_store()
        await store.revoke(token_value)
    _clear_session_cookie(response, request)
    response.status_code = 204
    return response


@router.get("/me")
async def me(identity: Identity = Depends(verify_api_key)) -> MeResponse:
    """Return the current session principal."""
    return MeResponse(
        authenticated=True,
        username=identity.subject,
        source=identity.source,
    )


@router.get("/status")
async def status(request: Request) -> StatusResponse:
    """SPA bootstrap discovery -- never raises 4xx/5xx.

    Decision order (PRP-0057 follow-up, 2026-05-16):

    1. ``AUTH_USERNAME`` is set -> ``mode = "login-required"`` regardless
       of whether the request peer is loopback. The operator explicitly
       opted into the ID/PW lane; the SPA UX MUST show the login page on
       localhost too. (The CLI-facing loopback bypass in ``verify_api_key``
       is preserved separately so ``curl localhost`` and ``chatwalaau``
       CLI continue to work without credentials.)
    2. Loopback peer or ``APP_REQUIRE_AUTH_ON_LAN=false`` -> ``mode = "open"``.
       Same as the pre-PRP-0057 behavior.
    3. Otherwise -> ``mode = "api-key-only"``.

    PRP-0066 / CTR-0094 v3: the response carries ``demo_mode`` (the value
    of the ``DEMO_MODE`` setting) so the SPA can render a "DEMO" badge.
    """
    demo = bool(settings.demo_mode)
    # PRP-0067 / CTR-0094 v4: settings.tool_approval_mode is normalized
    # to one of {"skip", "auto", "always"} by the Settings validator.
    approval_mode = settings.tool_approval_mode  # type: ignore[assignment]
    # PRP-0077 / CTR-0094 v6: normalize the title mode to a known value.
    title_mode = "llm" if settings.session_title_mode == "llm" else "truncate"
    if settings.web_auth_enabled:
        token_value = request.cookies.get(settings.auth_cookie_name, "")
        if token_value:
            store = get_session_store()
            record = await store.lookup(token_value)
            if record is not None:
                return StatusResponse(
                    mode="login-required",
                    authenticated=True,
                    username=record.username,
                    demo_mode=demo,
                    tool_approval_mode=approval_mode,
                    session_title_mode=title_mode,
                )
        return StatusResponse(
            mode="login-required",
            authenticated=False,
            demo_mode=demo,
            tool_approval_mode=approval_mode,
        )

    client_host = request.client.host if request.client else None
    is_loopback = is_client_loopback(client_host)

    if is_loopback or not settings.app_require_auth_on_lan:
        return StatusResponse(
            mode="open",
            authenticated=True,
            username=None,
            demo_mode=demo,
            tool_approval_mode=approval_mode,
        )

    # No web auth lane; Bearer API_KEY required for non-loopback callers.
    return StatusResponse(
        mode="api-key-only",
        authenticated=False,
        demo_mode=demo,
        tool_approval_mode=approval_mode,
    )


__all__ = ["router"]
