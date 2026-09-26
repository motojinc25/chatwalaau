"""GPT-Live wire client (PRP-0188, UDR-0170 D1 / D2).

Microsoft Agent Framework has no GPT-Live support, and the ``openai`` package has no
``live`` resource (its ``realtime`` resource is a different protocol). So the
integration speaks the wire protocol directly:

- session creation: one HTTPS POST to ``/openai/v1/live/sessions`` with the WebRTC
  SDP offer (``httpx``);
- control: a sideband WebSocket to ``/openai/v1/live/sessions/{id}/attach``
  (``websockets``), the same library the Realtime STT lane uses (CTR-0021).

Both authenticate with the live offering's own ``api_key_env`` when it declares one,
otherwise through ``app.azure_credential`` -- ``api-key`` when AZURE_OPENAI_API_KEY is
set, an Entra ``Bearer`` token otherwise (UDR-0034). The credential never leaves this
process: the browser receives only the SDP answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)

#: The voices GPT-Live accepts for ``session.audio.output.voice`` (UDR-0170 D8, step 2).
LIVE_VOICES: tuple[str, ...] = (
    "alloy",
    "ash",
    "ballad",
    "beacon",
    "bossa",
    "cedar",
    "cinder",
    "coral",
    "delta",
    "echo",
    "gleam",
    "marin",
    "meridian",
    "quartz",
    "ripple",
    "sage",
    "shimmer",
    "stone",
    "tempo",
    "verse",
    "vesper",
    "willow",
)
#: The service default, and the App Settings default.
DEFAULT_LIVE_VOICE = "marin"


def resolve_voice(value: str | None) -> str:
    """A known voice, or the default for anything else (a session is never refused for it)."""
    return value if value in LIVE_VOICES else DEFAULT_LIVE_VOICE


_CREATE_TIMEOUT_SEC = 20.0


class LiveUpstreamError(Exception):
    """The service refused or failed a session creation (answered 502)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreatedSession:
    session_id: str
    sdp_answer: str
    expires_at: int | None = None


def endpoint_host(endpoint: str) -> str:
    """Host of AZURE_OPENAI_ENDPOINT, tolerating ``https://host/`` and bare ``host``."""
    raw = endpoint.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    return urlparse(raw).netloc.rstrip("/")


def sessions_url(endpoint: str) -> str:
    return f"https://{endpoint_host(endpoint)}/openai/v1/live/sessions"


def attach_url(endpoint: str, session_id: str) -> str:
    return f"wss://{endpoint_host(endpoint)}/openai/v1/live/sessions/{quote(session_id, safe='')}/attach"


def build_session_config(*, deployment: str, instructions: str, voice: str = DEFAULT_LIVE_VOICE) -> dict[str, Any]:
    """The strict GPT-Live ``session`` object (S4): exactly these four keys."""
    return {
        "model": deployment,
        "instructions": instructions,
        "audio": {"output": {"voice": resolve_voice(voice)}},
        "delegation": {"type": "client"},
    }


async def auth_header(api_key: str | None = None) -> tuple[str, str]:
    """``(name, value)`` for the upstream request. The Entra lane may block, so off-loop.

    ``api_key`` is the live offering's own key (its ``api_key_env``), when it has one.
    """
    if api_key:
        return ("api-key", api_key)
    from app.azure_credential import get_realtime_websocket_auth_header

    return await asyncio.to_thread(get_realtime_websocket_auth_header)


def _upstream_error(response: httpx.Response) -> LiveUpstreamError:
    code = "upstream_error"
    message = f"GPT-Live session creation failed with HTTP {response.status_code}"
    try:
        body = response.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or code)
            message = str(err.get("message") or message)
    except ValueError:
        pass
    return LiveUpstreamError(response.status_code, code, message)


async def create_session(
    *,
    endpoint: str,
    deployment: str,
    instructions: str,
    sdp_offer: str,
    voice: str = DEFAULT_LIVE_VOICE,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CreatedSession:
    """POST the WebRTC session creation and return the session id and SDP answer."""
    name, value = await auth_header(api_key)
    body = {
        "session": build_session_config(deployment=deployment, instructions=instructions, voice=voice),
        "transport": {"type": "webrtc", "sdp": sdp_offer},
    }
    async with httpx.AsyncClient(timeout=_CREATE_TIMEOUT_SEC, transport=transport) as client:
        try:
            response = await client.post(sessions_url(endpoint), json=body, headers={name: value})
        except httpx.HTTPError as exc:
            raise LiveUpstreamError(0, "upstream_unreachable", str(exc) or type(exc).__name__) from exc
    if response.status_code >= 400:
        raise _upstream_error(response)
    try:
        data = response.json()
        session = data["session"]
        answer = data["transport"]["sdp"]
        session_id = str(session["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise LiveUpstreamError(response.status_code, "bad_upstream_response", "Malformed session response") from exc
    expires = session.get("expires_at") if isinstance(session, dict) else None
    return CreatedSession(
        session_id=session_id,
        sdp_answer=str(answer),
        expires_at=expires if isinstance(expires, int) else None,
    )


async def delete_session(
    *,
    endpoint: str,
    session_id: str,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Best-effort ``DELETE /openai/v1/live/sessions/{id}`` (PRP-0188 step 3).

    Used ONLY when a session was created but its sideband could not be attached: with
    no sideband, ``session.close`` cannot be sent, and the session would otherwise live
    until it expires. The GPT-Live reference (2026-09) documents no REST close, so this
    is the conventional REST shape tried once: any 2xx (or 404 -- already gone) counts
    as done; anything else, including 405 "not supported", is logged and returns False.
    Never raises.
    """
    try:
        name, value = await auth_header(api_key)
        url = f"{sessions_url(endpoint)}/{quote(session_id, safe='')}"
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            response = await client.delete(url, headers={name: value})
    except Exception:
        logger.warning("Live: DELETE of orphaned session %s failed", session_id, exc_info=True)
        return False
    if response.status_code < 300 or response.status_code == 404:
        return True
    logger.warning(
        "Live: DELETE of orphaned session %s answered HTTP %s; it will expire on its own",
        session_id,
        response.status_code,
    )
    return False


async def connect_sideband(*, endpoint: str, session_id: str, api_key: str | None = None) -> Any:
    """Open the sideband WebSocket to a running session (S3). Caller closes it."""
    import websockets

    name, value = await auth_header(api_key)
    return await websockets.connect(
        attach_url(endpoint, session_id),
        additional_headers={name: value},
        max_size=None,
        open_timeout=10.0,
        close_timeout=5.0,
    )
