"""Slash Command API (CTR-0126, PRP-0088, UDR-0066).

    GET /api/commands -- the merged effective command inventory: built-ins
                         (CTR-0125) + Prompt-Template-derived (CTR-0047) +
                         Skill-derived (CTR-0043), with collision reporting.

Read-only; dispatch is client-side (UDR-0066 D1). Auth-gated by CTR-0083
(``verify_api_key``) as an info-disclosure gate; loopback bypass keeps
localhost-first development zero-config (UDR-0066 D6).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.commands.inventory import get_commands_inventory

router = APIRouter(prefix="/api", tags=["Commands"])


@router.get("/commands", dependencies=[Depends(verify_api_key)])
async def list_commands() -> dict:
    """Return the merged slash command inventory with collision reporting."""
    return await get_commands_inventory()


__all__ = ["router"]
