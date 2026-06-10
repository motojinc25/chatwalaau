"""Server -> client notification channel over WebSocket (CTR-0110, PRP-0077).

A small, general-purpose push channel so the backend can notify connected SPA
clients of out-of-band state changes in real time, without the SPA polling or
refetching. Its first event type is ``session_title`` (the Auto Session Title
background task, CTR-0109, pushes the finalized title), mirroring how CTR-0108
is a general runner with the title task as its first consumer. Future
notifications (e.g. the deferred memory-extraction job) reuse the same hub.

The hub holds the set of live WebSocket connections and broadcasts a JSON event
to all of them best-effort (a dead socket is dropped, never raised). It runs in
the main FastAPI process / event loop, so a background task (CTR-0108) on the
same loop can broadcast directly.

Authentication mirrors CTR-0083 (see ``app.auth.authorize_websocket``): loopback
peers bypass; same-origin handshakes carry the session cookie automatically; the
API_KEY lane is accepted via an ``api_key`` query parameter (the browser
WebSocket API cannot set an Authorization header). The gate is never relaxed.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class NotificationHub:
    """Tracks live WebSocket clients and broadcasts JSON events to them."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        logger.debug("notification client connected (%d total)", len(self._clients))

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)
        logger.debug("notification client disconnected (%d total)", len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Send ``event`` (JSON) to every connected client; drop dead sockets.

        Best-effort: a send failure removes that client and never raises, so a
        broadcast from a background task can never disturb the chat path.
        """
        if not self._clients:
            return
        dead: list[WebSocket] = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json(event)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self._clients.discard(websocket)


# Process-wide singleton hub (one event loop / one process).
hub = NotificationHub()


def register_notifications_endpoint(app: FastAPI) -> None:
    """Register the WebSocket notification endpoint at /ws/notifications (CTR-0110)."""

    @app.websocket("/ws/notifications")
    async def notifications(websocket: WebSocket) -> None:
        from app.auth import authorize_websocket

        if not await authorize_websocket(websocket):
            # Reject the handshake before accepting (policy violation).
            await websocket.close(code=1008)
            return
        await hub.connect(websocket)
        try:
            # The server only pushes; we read to detect client disconnects and
            # ignore any inbound payloads.
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("notification socket closed unexpectedly", exc_info=True)
        finally:
            hub.disconnect(websocket)

    logger.info("Notification WebSocket registered at /ws/notifications (CTR-0110)")


__all__ = ["NotificationHub", "hub", "register_notifications_endpoint"]
