"""Live Voice Session API and Event Stream (CTR-0223 / CTR-0224, PRP-0188, UDR-0170).

- ``GET    /api/live/status``                     is Live offered here, and its limits
- ``POST   /api/live/sessions``                   SDP offer in, SDP answer out (D2)
- ``POST   /api/live/sessions/{id}/mute``         mute / unmute caller input (Q5)
- ``DELETE /api/live/sessions/{id}``              stop (``session.close`` via sideband)
- ``GET    /api/live/sessions/{id}/events``       SSE for the SPA (terminator
  ``live.closed``)

Every route is gated by CTR-0083 (``verify_api_key``). The browser never receives a
credential or a URL it could use to create a GPT-Live session (UDR-0170 D2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import models_catalog
from app.auth import verify_api_key
from app.core.config import settings
from app.live import persist
from app.live.client import LiveUpstreamError, connect_sideband, create_session, delete_session
from app.live.controller import LiveLimits, LiveSession, manager
from app.live.instructions import build_live_instructions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/live", tags=["Live"])

#: Same shape as a session thread id (a UUID, ``temp_``-prefixed for temporary chat).
_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_KEEPALIVE_SEC = 15.0

_agent_registry: Any = None


class StartRequest(BaseModel):
    thread_id: str
    sdp_offer: str = Field(min_length=1, max_length=200_000)


class MuteRequest(BaseModel):
    muted: bool


def live_target() -> models_catalog.ResolvedModelConfig | None:
    """The catalog's live offering, or None (PRP-0188 step 2, UDR-0170 D11)."""
    return models_catalog.live_config()


def _endpoint(target: models_catalog.ResolvedModelConfig) -> str:
    return (target.endpoint or settings.azure_openai_endpoint or "").strip()


def availability() -> tuple[bool, str | None]:
    """Whether Live is offered, and why not (UDR-0170 D11, the server-side part).

    Live exists exactly when the Model Offering Catalog registers a `live` offering
    (step 2): there is no environment gate and no deployment setting.
    """
    from app.demo import is_demo_mode

    if is_demo_mode():
        return False, "demo_mode"
    target = live_target()
    if target is None or not target.deployment.strip() or not _endpoint(target):
        return False, "not_configured"
    return True, None


def current_limits() -> LiveLimits:
    return LiveLimits(
        max_session_seconds=int(settings.gpt_live_max_session_seconds),
        idle_timeout_seconds=int(settings.gpt_live_idle_timeout_seconds),
        delegation_timeout_seconds=int(settings.gpt_live_delegation_timeout_seconds),
    )


def _active_agent() -> dict[str, str]:
    """The agent delegations run as, and the chat model they run on (D4, step 2)."""
    from app.agent.declarative.store import active_spec

    spec = active_spec()
    model = models_catalog.resolve_task_model("live_delegation", getattr(_agent_registry, "default_model", None))
    return {"id": spec.id, "name": spec.display_name or spec.name, "model": model}


@router.get("/status", dependencies=[Depends(verify_api_key)])
async def live_status() -> dict[str, Any]:
    offered, reason = availability()
    body: dict[str, Any] = {"offered": offered, "limits": current_limits().public()}
    if reason:
        body["reason"] = reason
    return body


@router.post("/sessions", dependencies=[Depends(verify_api_key)])
async def start_live_session(body: StartRequest) -> dict[str, Any]:
    offered, reason = availability()
    if not offered:
        raise HTTPException(status_code=503, detail={"code": "live_unavailable", "reason": reason})
    if not _THREAD_ID.match(body.thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread_id")
    if _agent_registry is None:
        raise HTTPException(status_code=409, detail={"code": "agent_unavailable"})
    try:
        _agent_registry.get(None)
        agent = _active_agent()
    except Exception as exc:
        raise HTTPException(status_code=409, detail={"code": "agent_unavailable", "message": str(exc)}) from exc
    if not manager.reserve(body.thread_id):
        raise HTTPException(status_code=409, detail={"code": "live_busy"})

    from app.agent.temporary import is_temporary

    limits = current_limits()
    target = live_target()
    if target is None:  # the catalog changed since availability() looked
        manager.release(body.thread_id)
        raise HTTPException(status_code=503, detail={"code": "live_unavailable", "reason": "not_configured"})
    endpoint = _endpoint(target)
    try:
        created = await create_session(
            endpoint=endpoint,
            deployment=target.deployment.strip(),
            instructions=build_live_instructions(),
            sdp_offer=body.sdp_offer,
            voice=settings.gpt_live_voice,
            api_key=target.api_key,
        )
        ws = await _attach_or_clean_up(
            endpoint=endpoint, session_id=created.session_id, api_key=target.api_key, thread_id=body.thread_id
        )
    except LiveUpstreamError as exc:
        manager.release(body.thread_id)
        raise HTTPException(
            status_code=502, detail={"code": exc.code, "message": exc.message, "status": exc.status}
        ) from exc
    except Exception:
        manager.release(body.thread_id)
        raise

    try:
        persist.ensure_session(body.thread_id)
        persist.start_live_record(body.thread_id, created.session_id)
    except Exception:
        logger.warning("Live: could not open the session record for %s", body.thread_id, exc_info=True)

    session = LiveSession(
        live_session_id=created.session_id,
        thread_id=body.thread_id,
        temporary=is_temporary(body.thread_id),
        agent_registry=_agent_registry,
        agent=agent,
        limits=limits,
        ws=ws,
        on_finished=manager.on_finished,
    )
    manager.add(session)
    session.start()
    logger.info("Live session %s started for thread %s (agent=%s)", created.session_id, body.thread_id, agent["id"])
    return {
        "live_session_id": created.session_id,
        "sdp_answer": created.sdp_answer,
        "limits": limits.public(),
        "agent": agent,
    }


#: Step 3: pauses between sideband attach attempts (so three attempts in all).
SIDEBAND_RETRY_DELAYS_SEC: tuple[float, ...] = (0.5, 1.5)


async def _attach_or_clean_up(*, endpoint: str, session_id: str, api_key: str | None, thread_id: str) -> Any:
    """Attach the sideband, retrying; if it never attaches, try to delete the session.

    Without a sideband nothing can run or close the session, so it is refused and the
    browser never talks to an uncontrolled session (D3). Step 3 adds the clean-up:
    the created session is DELETEd (best-effort, client.delete_session) instead of
    being left to expire, and the attempt is recorded on the chat's live_sessions[]
    with reason ``sideband_failed``.
    """
    last: Exception | None = None
    for delay in (0.0, *SIDEBAND_RETRY_DELAYS_SEC):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await connect_sideband(endpoint=endpoint, session_id=session_id, api_key=api_key)
        except Exception as exc:
            last = exc
            logger.warning("Live: sideband attach failed for %s", session_id, exc_info=True)
    deleted = await delete_session(endpoint=endpoint, session_id=session_id, api_key=api_key)
    try:
        persist.ensure_session(thread_id)
        persist.start_live_record(thread_id, session_id)
        persist.finish_live_record(thread_id, session_id, seconds=None, reason="sideband_failed", delegations=0)
    except Exception:
        logger.warning("Live: could not record the failed session %s", session_id, exc_info=True)
    message = str(last) or type(last).__name__ if last else "sideband attach failed"
    raise LiveUpstreamError(0, "sideband_failed", f"{message} (session deleted: {'yes' if deleted else 'no'})")


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


def _session_or_404(live_session_id: str) -> LiveSession:
    session = manager.get(live_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Live session not found")
    return session


@router.post("/sessions/{live_session_id}/text", dependencies=[Depends(verify_api_key)])
async def send_live_text(live_session_id: str, body: TextRequest) -> dict[str, Any]:
    """A message typed during Live (step 3, UDR-0170 D13)."""
    session = _session_or_404(live_session_id)
    if session.state != "live":
        raise HTTPException(status_code=409, detail={"code": "live_closing"})
    try:
        return await session.send_text(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Empty text") from exc


@router.post(
    "/sessions/{live_session_id}/delegations/{delegation_id}/cancel",
    dependencies=[Depends(verify_api_key)],
    status_code=202,
)
async def cancel_live_delegation(live_session_id: str, delegation_id: str) -> dict[str, Any]:
    """Cancel a queued or running delegation (step 3, UDR-0170 D14)."""
    session = _session_or_404(live_session_id)
    if not session.cancel_delegation(delegation_id):
        raise HTTPException(status_code=404, detail="Delegation not found or already finished")
    return {"status": "accepted", "delegation_id": delegation_id}


@router.post("/sessions/{live_session_id}/mute", dependencies=[Depends(verify_api_key)], status_code=202)
async def mute_live_session(live_session_id: str, body: MuteRequest) -> dict[str, Any]:
    session = _session_or_404(live_session_id)
    await session.set_muted(body.muted)
    return {"status": "accepted", "muted": body.muted}


@router.delete("/sessions/{live_session_id}", dependencies=[Depends(verify_api_key)], status_code=202)
async def stop_live_session(live_session_id: str) -> Response:
    session = _session_or_404(live_session_id)
    # Closing waits for session.closed (up to 10 s); the SPA learns the end from
    # live.closed on its event stream, so the request does not wait for it.
    session.request_close("user_stop")
    return Response(status_code=202)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/sessions/{live_session_id}/events", dependencies=[Depends(verify_api_key)])
async def live_events(live_session_id: str) -> StreamingResponse:
    session = _session_or_404(live_session_id)
    token, queue = session.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SEC)
                except TimeoutError:
                    if not session.is_current_subscriber(token):  # replaced by a re-attach
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event, data)
                if event == "live.closed":
                    return
        finally:
            session.unsubscribe(token)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def register_live(app: Any, *, agent_registry: Any) -> None:
    """Mount the Live routes and bind the agent registry the delegations run on."""
    global _agent_registry
    _agent_registry = agent_registry
    app.include_router(router)


async def shutdown() -> None:
    """Lifespan shutdown: close every running Live session (``server_shutdown``)."""
    await manager.shutdown()
