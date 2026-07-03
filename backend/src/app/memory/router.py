"""Memory Curation REST router (CTR-0164, PRP-0100 / UDR-0079).

The trigger API for the per-turn "like" write path of the Agent Curated Memory
(CTR-0162). The SPA (CTR-0165) posts here when the operator toggles a turn's
thumbs-up.

- ``POST /api/memory/curate`` toggles a turn's liked state (persisted in the
  session record via CTR-0014) and, on like, dispatches the CTR-0163
  ``memory-curate`` background task (dedup by thread + turn); on unlike it clears
  the state and dispatches nothing. CTR-0083-gated mutation (loopback bypass).
- ``GET /api/memory/status`` reports whether the feature is enabled (read;
  follows the read convention). The SPA hides the like affordance when disabled.

Un-liking then re-liking re-dispatches (the toggle is stateless beyond the
persisted ``memory_liked`` marker). The background task is inert in DEMO_MODE and
for temporary chats; it pushes a ``memory_curated`` event on completion (CTR-0110).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["Agent Memory"])


class CurateRequest(BaseModel):
    thread_id: str = Field(..., min_length=1, max_length=256)
    turn_key: str = Field(..., min_length=1, max_length=256)
    liked: bool
    user_text: str = Field(default="", max_length=20000)
    assistant_text: str = Field(default="", max_length=20000)
    model: str = Field(default="", max_length=256)


class CurateResponse(BaseModel):
    thread_id: str
    turn_key: str
    liked: bool
    dispatched: bool


class MemoryStatusResponse(BaseModel):
    enabled: bool


class LikedEntry(BaseModel):
    turn_key: str
    status: str


class LikedStateResponse(BaseModel):
    thread_id: str
    liked: list[LikedEntry]


@router.get("/status", response_model=MemoryStatusResponse, dependencies=[Depends(verify_api_key)])
async def memory_status() -> MemoryStatusResponse:
    """Report whether Agent Curated Memory is enabled (CTR-0164)."""
    return MemoryStatusResponse(enabled=settings.agent_memory_enabled)


@router.get("/liked/{thread_id}", response_model=LikedStateResponse, dependencies=[Depends(verify_api_key)])
async def liked_state(thread_id: str) -> LikedStateResponse:
    """Return the persisted per-turn like state for a session (CTR-0164 / CTR-0014).

    Read convention (loopback bypass). Lets the SPA restore the filled like icons
    on reload. An unknown session or a legacy record without ``memory_liked`` yields
    an empty list.
    """
    from app.session.storage import read_session_json

    try:
        data = read_session_json(thread_id)
    except (OSError, ValueError):
        data = None
    raw = (data or {}).get("memory_liked")
    entries = (
        [
            LikedEntry(turn_key=str(item["turn_key"]), status=str(item.get("status", "curated")))
            for item in raw
            if isinstance(item, dict) and item.get("turn_key")
        ]
        if isinstance(raw, list)
        else []
    )
    return LikedStateResponse(thread_id=thread_id, liked=entries)


@router.post("/curate", response_model=CurateResponse, dependencies=[Depends(verify_api_key)])
async def curate_turn(body: CurateRequest) -> CurateResponse:
    """Toggle a turn's like state and (on like) dispatch the curation task (CTR-0164).

    On ``liked=true``: persist a ``pending`` ``memory_liked`` marker and dispatch the
    CTR-0163 background pass. On ``liked=false``: clear the marker, dispatch nothing.
    Returns immediately; the actual reconcile is fire-and-forget (CTR-0108).
    """
    from app.background import dispatch as dispatch_background
    from app.background.memory_curate import clear_liked, set_liked

    dispatched = False
    if not settings.agent_memory_enabled:
        # Feature off: never dispatch. Report the requested liked flag so the SPA
        # (which hides the control anyway) stays consistent.
        return CurateResponse(thread_id=body.thread_id, turn_key=body.turn_key, liked=body.liked, dispatched=False)

    if body.liked:
        set_liked(body.thread_id, body.turn_key, "pending")
        dispatch_background(
            "memory-curate",
            dedup_key=f"{body.thread_id}:{body.turn_key}",
            ctx={
                "thread_id": body.thread_id,
                "turn_key": body.turn_key,
                "user_text": body.user_text,
                "assistant_text": body.assistant_text,
                "model": body.model,
            },
        )
        dispatched = True
    else:
        clear_liked(body.thread_id, body.turn_key)

    logger.info(
        "memory curate toggle: thread=%s turn=%s liked=%s dispatched=%s",
        body.thread_id,
        body.turn_key,
        body.liked,
        dispatched,
    )
    return CurateResponse(thread_id=body.thread_id, turn_key=body.turn_key, liked=body.liked, dispatched=dispatched)


__all__ = ["router"]
