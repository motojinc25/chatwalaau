"""Computer Use Control API (CTR-0233, PRP-0189 Section 2.8 / 2.10).

- ``GET  /api/computer-use/status``    is Computer Use offered on this host, and why not;
  the state of the stdio MCP desktop provider (PRP-0190)
- ``POST /api/computer-use/abort``     the SPA's kill switch (one of four, UDR-0171 D11)
- ``GET  /api/computer-use/metrics``   p50 / p95 per phase, p95 cycle, capacity
- ``GET  /api/computer-use/captures/{thread_id}/latest``   the newest capture of a chat
- ``GET  /api/computer-use/captures/{thread_id}/{file}``   one capture PNG (history)

Every route depends on CTR-0083 (``verify_api_key``); the POST is a mutating method
of CAP-002 and therefore consumes it by the system-model invariant.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse

from app.auth import verify_api_key
from app.computer_use import captures, state, trace
from app.computer_use.availability import availability
from app.computer_use.worker import HOTKEY_LABEL, provider_status

router = APIRouter(prefix="/api/computer-use", tags=["Computer Use"])


@router.get("/status", dependencies=[Depends(verify_api_key)])
async def computer_use_status() -> dict[str, Any]:
    verdict = availability()
    body: dict[str, Any] = {"offered": verdict.offered, "kill_switch": HOTKEY_LABEL, "busy": state.is_held()}
    if verdict.reason:
        body["reason"] = verdict.reason
    body["provider"] = provider_status()  # PRP-0190: never starts it
    return body


@router.post("/abort", dependencies=[Depends(verify_api_key)])
async def computer_use_abort() -> dict[str, Any]:
    """Stop whatever the agent is doing on the desktop now."""
    state.request_abort("api")
    return {"aborted": True}


@router.get("/metrics", dependencies=[Depends(verify_api_key)])
async def computer_use_metrics(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    return {"days": days, **trace.summarize(trace.read(days))}


_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/captures/{thread_id}/latest", dependencies=[Depends(verify_api_key)])
async def computer_use_latest_capture(thread_id: str) -> Response:
    """The newest capture of a chat, for the SPA's live viewer (amendment A4); 204 when none."""
    if captures.thread_dir(thread_id) is None:
        raise HTTPException(status_code=400, detail="Invalid thread_id")
    pointer = captures.latest(thread_id)
    if pointer is None:
        return Response(status_code=204, headers=_NO_STORE)
    body = {**pointer, "url": f"/api/computer-use/captures/{thread_id}/{pointer['file']}"}
    return Response(content=json.dumps(body, ensure_ascii=False), media_type="application/json", headers=_NO_STORE)


@router.get("/captures/{thread_id}/{name}", dependencies=[Depends(verify_api_key)])
async def computer_use_capture_file(thread_id: str, name: str) -> FileResponse:
    path = captures.file_path(thread_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Capture not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


__all__ = ["router"]
