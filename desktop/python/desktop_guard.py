"""ChatWalaau Desktop local access guard (CTR-0211, PRP-0167, UDR-0151 D8).

An ASGI wrapper the Desktop launcher applies AROUND the unchanged ``app.main:app``. It
lives outside ``backend/src`` on purpose (UDR-0151 D1): the web version's loopback
posture (CTR-0083) is untouched, while the Desktop -- installed by non-developers and
running next to their browser -- is protected from other pages on the same PC.

Rules, applied to every ``http`` and ``websocket`` scope (``lifespan`` passes through):

1. ``Host`` must be exactly ``127.0.0.1:<port>``. This defeats DNS rebinding: a
   rebinding page reaches the socket but carries its own host name.       -> 421
2. The request must carry the per-launch token cookie (``cw_desktop``), which the
   Desktop sets as HttpOnly + SameSite=Strict in its own chat partition. A cross-site
   request never carries it.                                               -> 403
3. Exception: a request carrying ``Authorization: Bearer`` or ``X-API-Key`` is passed on
   WITHOUT the cookie only while ``API_KEY`` is configured; CTR-0083 then validates the
   credential exactly as today. With no API_KEY the header grants nothing.  -> 403

Pure standard library; importable without the backend (unit-tested by
tests/invariants/test_prp0167_desktop.py).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
import hmac
import json
from typing import Any

COOKIE_NAME = "cw_desktop"

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _headers(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or []:
        key = raw_key.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        # Multiple Cookie headers are legal over HTTP/2-to-1 downgrades; join them.
        out[key] = f"{out[key]}; {value}" if key in out and key == "cookie" else value
    return out


def _cookie_value(cookie_header: str, name: str) -> str | None:
    for part in cookie_header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return None


class DesktopGuard:
    """Wrap an ASGI app with the Desktop's Host + token checks."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        port: int,
        api_key_configured: Callable[[], bool],
    ) -> None:
        if len(token) < 32:
            raise ValueError("desktop token is too short")
        self.app = app
        self._token = token
        self._port = port
        self._expected_host = f"127.0.0.1:{port}"
        self._api_key_configured = api_key_configured

    def set_port(self, port: int) -> None:
        """The launcher learns the real port after binding (port 0 -> OS choice)."""
        self._port = port
        self._expected_host = f"127.0.0.1:{port}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        if headers.get("host", "") != self._expected_host:
            await self._reject(scope, send, 421, "misdirected_request")
            return

        if self._has_token(headers) or (self._has_api_key_header(headers) and self._api_key_configured()):
            await self.app(scope, receive, send)
            return

        await self._reject(scope, send, 403, "desktop_guard")

    def _has_token(self, headers: dict[str, str]) -> bool:
        value = _cookie_value(headers.get("cookie", ""), COOKIE_NAME)
        return value is not None and hmac.compare_digest(value.encode(), self._token.encode())

    @staticmethod
    def _has_api_key_header(headers: dict[str, str]) -> bool:
        auth = headers.get("authorization", "")
        return auth.lower().startswith("bearer ") or bool(headers.get("x-api-key"))

    @staticmethod
    async def _reject(scope: Scope, send: Send, status: int, reason: str) -> None:
        if scope.get("type") == "websocket":
            # Closing before accept makes the server answer the upgrade with 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": reason}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
