"""AI Discussion API and phase stream (CTR-0272 / CTR-0273, PRP-0232, UDR-0215).

REST under ``/api/discussions``; every endpoint depends on ``verify_api_key`` (CTR-0083). The
phase stream ``POST .../advance`` runs the next phase of a run and streams AG-UI-encoded events,
kept alive like ``/ag-ui/`` and logged once when the client closes it early (UDR-0214 D2 style).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import Identity, verify_api_key
from app.discussion import controller, participants, schema
from app.session.storage import SessionCorruptError, SessionUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discussions", tags=["AI Discussion"], dependencies=[Depends(verify_api_key)])


class SeatBody(BaseModel):
    kind: str = Field("preset", pattern="^(preset|agent)$")
    preset: str | None = Field(None, max_length=64)
    agent_id: str | None = Field(None, max_length=512)
    # A role seat's model and reasoning effort; an agent seat uses the agent's own and ignores both.
    model: str | None = Field(None, max_length=256)
    effort: str | None = Field(None, max_length=16)
    display_name: str | None = Field(None, max_length=60)


class CreateBody(BaseModel):
    topic: str = Field(..., min_length=1, max_length=2000)
    goal: str = Field("", max_length=2000)
    constraints: str = Field("", max_length=4000)
    participants: list[SeatBody] = Field(..., min_length=2, max_length=6)
    moderator_model: str | None = Field(None, max_length=256)
    rounds: int | None = Field(None, ge=1, le=10)
    folder_id: str | None = Field(None, max_length=128)


class InputBody(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=64)
    action: str = Field(..., max_length=16)
    text: str = Field("", max_length=8000)
    speaker_id: str | None = Field(None, max_length=128)


class DecisionBody(BaseModel):
    verdict: str = Field(..., max_length=16)
    note: str = Field("", max_length=2000)


class ContinueBody(BaseModel):
    focus: str = Field("", max_length=4000)
    # Omitted: 1 round; the opening is skipped when a conclusion is carried; the seats are kept (UDR-0216).
    rounds: int | None = Field(None, ge=1, le=10)
    skip_opening: bool | None = None
    participants: list[SeatBody] | None = Field(None, min_length=2, max_length=6)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, controller.DiscussionError):
        return HTTPException(status_code=exc.status, detail={"error": exc.code, "message": exc.message})
    if isinstance(exc, SessionCorruptError):
        return HTTPException(status_code=500, detail="Failed to read session")
    if isinstance(exc, SessionUnavailableError):
        return HTTPException(
            status_code=503, detail="Session is temporarily unavailable; retry.", headers={"Retry-After": "1"}
        )
    raise exc


def _thread(thread_id: str) -> str:
    from app.agent.temporary import is_temporary

    if is_temporary(thread_id):
        raise HTTPException(
            status_code=409, detail={"error": "temporary_chat", "message": "a temporary chat has no discussion"}
        )
    return thread_id


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except (controller.DiscussionError, SessionUnavailableError) as exc:
        raise _error(exc) from exc


@router.get("/options")
async def discussion_options() -> dict[str, Any]:
    """What the New AI discussion dialog offers: presets, limits, models and the default seats."""
    try:
        models = participants.configured_models()
        default = participants.default_model() if models else ""
    except participants.SeatError:
        models, default = [], ""
    return {
        "presets": participants.preset_catalog(),
        "default_seats": list(participants.DEFAULT_SEATS),
        "models": models,
        "default_model": default,
        # Reasoning efforts per model for a role seat; empty `allowed` = the model has none.
        "reasoning": {model: participants.reasoning_options(model) for model in models},
        "role_default_effort": participants.ROLE_DEFAULT_EFFORT,
        "limits": controller.limits(),
    }


@router.post("")
async def create_discussion(body: CreateBody, identity: Identity = Depends(verify_api_key)) -> dict[str, Any]:
    thread_id, run = _call(
        controller.create_discussion,
        brief={"topic": body.topic.strip(), "goal": body.goal.strip(), "constraints": body.constraints.strip()},
        seat_requests=[s.model_dump() for s in body.participants],
        moderator_model=body.moderator_model,
        rounds=body.rounds,
        folder_id=body.folder_id,
        initiated_by=identity.subject,
    )
    return {"thread_id": thread_id, "run": schema.public_run(run)}


@router.get("/{thread_id}/runs")
async def list_runs(thread_id: str) -> dict[str, Any]:
    return {"runs": _call(controller.list_runs, _thread(thread_id))}


@router.get("/{thread_id}/runs/{run_id}")
async def get_run(thread_id: str, run_id: str) -> dict[str, Any]:
    return {"run": _call(controller.get_run, _thread(thread_id), run_id)}


@router.post("/{thread_id}/runs/{run_id}/input")
async def post_input(
    thread_id: str, run_id: str, body: InputBody, identity: Identity = Depends(verify_api_key)
) -> dict[str, Any]:
    run, already, messages = _call(
        controller.apply_input,
        _thread(thread_id),
        run_id,
        request_id=body.request_id,
        action=body.action,
        text=body.text,
        speaker_id=body.speaker_id,
        actor=identity.subject,
    )
    return {"run": run, "already_applied": already, "messages": messages}


@router.post("/{thread_id}/runs/{run_id}/pause")
async def pause(thread_id: str, run_id: str) -> dict[str, Any]:
    return {"run": _call(controller.request_stop, _thread(thread_id), run_id, "pause")}


@router.post("/{thread_id}/runs/{run_id}/cancel")
async def cancel(thread_id: str, run_id: str) -> dict[str, Any]:
    return {"run": _call(controller.request_stop, _thread(thread_id), run_id, "cancel")}


@router.post("/{thread_id}/runs/{run_id}/decision")
async def decision(
    thread_id: str, run_id: str, body: DecisionBody, identity: Identity = Depends(verify_api_key)
) -> dict[str, Any]:
    run = _call(
        controller.record_decision,
        _thread(thread_id),
        run_id,
        verdict=body.verdict,
        note=body.note,
        actor=identity.subject,
    )
    return {"run": run}


@router.post("/{thread_id}/runs/{run_id}/continue")
async def continue_discussion(
    thread_id: str, run_id: str, body: ContinueBody, identity: Identity = Depends(verify_api_key)
) -> dict[str, Any]:
    run, messages = _call(
        controller.continue_discussion,
        _thread(thread_id),
        run_id,
        focus=body.focus,
        rounds=body.rounds,
        actor=identity.subject,
        skip_opening=body.skip_opening,
        seat_requests=None if body.participants is None else [s.model_dump() for s in body.participants],
    )
    return {"run": run, "messages": messages}


async def _log_closed(source: AsyncIterable[Any], *, thread_id: str, run_id: str) -> AsyncIterator[Any]:
    """Pass the stream through; log ONE line when the client closes it early (UDR-0214 D2 style)."""
    from app.agui.keepalive import KEEPALIVE_COMMENT

    started = time.monotonic()
    last_event = started
    keepalives = 0
    try:
        async for chunk in source:
            if chunk == KEEPALIVE_COMMENT:
                keepalives += 1
            else:
                last_event = time.monotonic()
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        now = time.monotonic()
        logger.warning(
            "AI discussion stream closed by the client before the phase ended: "
            "thread=%s run=%s elapsed=%.1fs silent_for=%.1fs keepalives=%d",
            thread_id,
            run_id,
            now - started,
            now - last_event,
            keepalives,
        )
        raise


@router.post("/{thread_id}/runs/{run_id}/advance")
async def advance(thread_id: str, run_id: str) -> StreamingResponse:
    """Run the next phase and stream it (CTR-0273). One advance per run at a time (D3)."""
    adv = _call(controller.begin_advance, _thread(thread_id), run_id)
    from app.agui.keepalive import with_keepalive

    return StreamingResponse(
        _log_closed(with_keepalive(controller.run_advance(adv)), thread_id=thread_id, run_id=run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
