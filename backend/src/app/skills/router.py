"""Skills Management API (CTR-0123, PRP-0087, UDR-0065).

Two endpoints let an operator gate the active Agent Skills set at runtime so a
large advertised Skills surface does not waste tokens every turn:

    GET  /api/skills  -- grouped inventory of discovered Skills (group + skill name
                         + description + current enabled state) plus any name
                         collisions across groups.
    PUT  /api/skills  -- apply an enable/disable selection: update the in-memory
                         override store and REBUILD all per-model agents (CTR-0070),
                         responding only AFTER the rebuild completes (this drives the
                         SPA "rebuilding" indicator).

Both endpoints are gated by CTR-0083 (``verify_api_key``); loopback bypass keeps
localhost-first development zero-config (UDR-0065 D6). The override store is
in-memory only -- a restart re-enables every Skill (UDR-0065 D4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.skills.inventory import get_skills_inventory
from app.skills.overrides import get_skills_override_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["Skills"])


class SkillSelection(BaseModel):
    """Desired enabled state for a single Skill."""

    name: str = Field(..., min_length=1, max_length=256)
    enabled: bool = True


class GroupSelection(BaseModel):
    """Desired enabled state for the skills under one group.

    A group-level toggle is expanded by the UI into its per-skill states before
    submission (UDR-0065 D2), so the server only needs the per-skill detail here.
    """

    name: str = Field(default="", max_length=256)
    skills: list[SkillSelection] = Field(default_factory=list)


class SkillsSelection(BaseModel):
    """The full desired selection submitted by the management UI."""

    groups: list[GroupSelection] = Field(default_factory=list)


def register_skills_management(app: FastAPI, *, agent_registry) -> None:
    """Mount the Skills Management endpoints, closing over the AgentRegistry.

    The router needs the live ``AgentRegistry`` (CTR-0070) so PUT can rebuild it;
    the registry is the module-level singleton created in ``app.main`` and shared
    by reference with the AG-UI / OpenAI-API endpoints (UDR-0065 D2).
    """

    @router.get("", dependencies=[Depends(verify_api_key)])
    async def list_skills() -> dict:
        """Return the grouped Skills inventory with current enabled/disabled state."""
        return await get_skills_inventory()

    @router.put("", dependencies=[Depends(verify_api_key)])
    async def apply_skills(body: SkillsSelection) -> dict:
        """Apply a selection, rebuild the agents, and return the refreshed inventory.

        On rebuild failure the override store is rolled back to its prior snapshot so
        it stays consistent with the still-installed prior agents (UDR-0065 D5).
        """
        # Reduce the grouped selection to a flat set of disabled skill NAMES
        # (the persisted identity; UDR-0065 D2).
        disabled = {sk.name for g in body.groups for sk in g.skills if not sk.enabled}

        store = get_skills_override_store()
        prior = store.snapshot()
        store.set_disabled(disabled)

        try:
            # Local import avoids a module-level import cycle
            # (agent_factory -> app.skills.* -> ...).
            from app.agui.agent_factory import rebuild_agent_registry

            await rebuild_agent_registry(agent_registry)
        except Exception:
            # Roll back so the store matches the prior agents that are still serving.
            store.set_disabled(prior)
            logger.exception("Skills selection apply failed during agent rebuild")
            raise HTTPException(
                status_code=500,
                detail={"error": "agent_rebuild_failed"},
            ) from None

        # Log the EFFECTIVE advertised set from the refreshed inventory (not just the
        # requested count): this is exactly what the next chat run will advertise, so
        # an operator can confirm the gating actually took effect. MAF's own
        # "Successfully loaded N skills" line reports the UNFILTERED discovery (the
        # FilteringSkillsSource drops disabled skills after that log), so it is not a
        # reliable signal on its own.
        inventory = await get_skills_inventory()
        enabled_names = [s["name"] for grp in inventory["groups"] for s in grp["skills"] if s["enabled"]]
        disabled_names = [s["name"] for grp in inventory["groups"] for s in grp["skills"] if not s["enabled"]]
        logger.info(
            "Skills selection applied: %d advertised, %d disabled%s",
            len(enabled_names),
            len(disabled_names),
            f" (disabled: {', '.join(sorted(disabled_names))})" if disabled_names else "",
        )
        return inventory

    @router.post("/reload", dependencies=[Depends(verify_api_key)])
    async def reload_skills() -> dict:
        """Re-discover SKILL.md from disk, rebuild the agents, and prune stale overrides.

        Unlike PUT (which applies a new selection), Reload keeps the current selection
        and forces a rebuild so on-disk edits -- a newly added skill folder, a removed
        one -- are picked up without a restart (PRP-0090, UDR-0068 D1/D2). The rebuild
        re-runs ``create_skills_provider()`` which re-reads disk and refreshes the
        ``loaded`` snapshot. Override entries naming a skill that no longer exists on
        disk are pruned so the store does not accumulate stale names.
        """
        store = get_skills_override_store()
        prior = store.snapshot()
        try:
            from app.agui.agent_factory import rebuild_agent_registry

            await rebuild_agent_registry(agent_registry)
        except Exception:
            logger.exception("Skills reload failed during agent rebuild")
            raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None

        # Prune disabled names that no longer exist on disk (UDR-0068 D2). The rebuild
        # above refreshed the live-build snapshot, so it is the authoritative
        # discovered set.
        from app.skills.loaded import get_loaded_skills

        discovered = get_loaded_skills()
        pruned = prior & discovered
        if pruned != prior:
            store.set_disabled(pruned)
            logger.info("Skills reload pruned %d stale override(s)", len(prior - pruned))

        inventory = await get_skills_inventory()
        logger.info("Skills reloaded from disk: %d skill(s) discovered", len(discovered))
        return inventory

    app.include_router(router)


__all__ = ["register_skills_management", "router"]
