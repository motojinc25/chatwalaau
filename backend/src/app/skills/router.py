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

The apply boundary covers EVERY lane that holds a built provider (UDR-0130 D3).
The AgentRegistry rebuild reaches the Prompt lane; the per-conversation harness
session cache (CTR-0009 / UDR-0119 D3) holds its own built agent and must be
cleared in the same apply. Since UDR-0130 D1 the harness lane runs on the same
``create_skills_provider()`` source, so skipping it would leave a disabled skill
gone from chat and alive in every cached harness conversation until an unrelated
authoring write happened to clear it -- a worse state than the divergence D1
removed. The cost is that an in-flight harness transcript (MAF-internal, in
memory, UDR-0119 D4) is discarded; that is the same cost the authoring-write
clear already imposes, on a second trigger.

Both endpoints are gated by CTR-0083 (``verify_api_key``); loopback bypass keeps
localhost-first development zero-config (UDR-0065 D6).

PRP-0165 adds two things to this router.

1. The selection is now DURABLE (UDR-0148, superseding UDR-0065 D4). The in-memory
   override store is still the runtime authority -- nothing about the build path
   changed -- but every successful apply also writes it to the state file under
   SKILLS_DIR, and startup loads it back before the first agent is built. An
   absent file still means "nothing disabled", so a fleet that never opens this
   screen behaves exactly as it did before.
2. The catalog and install endpoints (CTR-0205) live here too, because they end in
   the same place a selection apply does: a registry rebuild plus a harness cache
   clear. Their WRITE half is refused in demo mode and when SKILL_INSTALL_ENABLED
   is off; the read half stays available so the screen is never blank.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.skills import catalog as catalog_mod
from app.skills import state as state_mod
from app.skills.install import InstallError, install_skill, uninstall_skill
from app.skills.inventory import get_skills_inventory
from app.skills.overrides import get_skills_override_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["Skills"])


async def _clear_harness_sessions(*, reason: str) -> None:
    """Drop every cached harness conversation so the apply reaches that lane too.

    UDR-0130 D3. The import is local for the same reason the ``rebuild_agent_registry``
    import is: ``app.agent.harness.factory`` imports ``app.skills.provider``, so a
    module-level import here would close the cycle.
    """
    from app.agent.harness.runtime import cache_size, clear_cache

    dropped = cache_size()
    await clear_cache()
    if dropped:
        logger.info("Harness session cache cleared (%d entry/entries, reason=%s)", dropped, reason)


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


class InstallRequest(BaseModel):
    """Install or reinstall one catalog skill (CTR-0205).

    ``force`` overrides ONLY the local-modification refusal. There is deliberately
    no force for a name collision: MAF drops the duplicate silently, so the write
    would produce a folder the agent never loads (UDR-0146 D3).
    """

    id: str = Field(..., min_length=1, max_length=512)
    force: bool = False


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

        # UDR-0130 D3: the harness session cache holds agents built from this same
        # source, so the apply is not complete until they are dropped. Ordering is
        # rebuild-then-clear: the rollback above owns a FAILED rebuild, and a clear
        # that has not run yet cannot leave a half-applied state.
        await _clear_harness_sessions(reason="skills_apply")

        # UDR-0148: persist the selection AFTER the rebuild succeeded. The rollback
        # above owns a failed rebuild, so a write that has not happened yet cannot
        # leave the file describing a state the process never reached.
        state_mod.save_disabled(disabled)

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

        # UDR-0130 D3: same boundary as PUT -- Reload rebuilds every lane, not just
        # the registry.
        await _clear_harness_sessions(reason="skills_reload")

        # Prune disabled names that no longer exist on disk (UDR-0068 D2). The rebuild
        # above refreshed the live-build snapshot, so it is the authoritative
        # discovered set.
        from app.skills.loaded import get_loaded_skills

        discovered = get_loaded_skills()
        pruned = prior & discovered
        if pruned != prior:
            store.set_disabled(pruned)
            state_mod.save_disabled(pruned)
            logger.info("Skills reload pruned %d stale override(s)", len(prior - pruned))

        inventory = await get_skills_inventory()
        logger.info("Skills reloaded from disk: %d skill(s) discovered", len(discovered))
        return inventory

    # ------------------------------------------------------------------
    # Skill Catalog and Installation (CTR-0205, PRP-0165)
    # ------------------------------------------------------------------

    async def _apply_install_change(*, reason: str) -> None:
        """Rebuild every lane that holds a built provider after a disk change.

        Identical to the apply boundary a selection change uses (UDR-0130 D3):
        an installed skill that only reached the registry would stay invisible to
        every cached harness conversation, and an uninstalled one would stay alive
        there.
        """
        try:
            from app.agui.agent_factory import rebuild_agent_registry

            await rebuild_agent_registry(agent_registry)
        except Exception:
            logger.exception("Skill %s succeeded but the agent rebuild failed", reason)
            raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None
        await _clear_harness_sessions(reason=reason)

    @router.get("/catalog", dependencies=[Depends(verify_api_key)])
    async def get_catalog() -> dict:
        """Return the catalog snapshot merged with the install ledger and disk state.

        Readable even when the write side is off (demo mode, SKILL_INSTALL_ENABLED
        false) so the screen shows what IS installed instead of nothing; the
        ``available`` flag tells the UI to disable every install control.
        """
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.post("/catalog/refresh", dependencies=[Depends(verify_api_key)])
    async def refresh_catalog_endpoint() -> dict:
        """Rebuild the catalog snapshot from every configured source.

        Per-source atomic: a source that fails keeps its previous entries, marked
        stale with the error attached (UDR-0147 D3), so one rate-limited repository
        cannot blank a working catalog.
        """
        if not catalog_mod.install_available():
            raise HTTPException(status_code=403, detail={"error": "install_disabled"})
        await catalog_mod.refresh_catalog()
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.post("/install", dependencies=[Depends(verify_api_key)])
    async def install_endpoint(body: InstallRequest) -> dict:
        """Install or reinstall one catalog skill, then rebuild the agents."""
        try:
            await install_skill(body.id, force=body.force)
        except InstallError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.payload()) from None
        await _apply_install_change(reason="skill_install")
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.delete("/install/{skill_id:path}", dependencies=[Depends(verify_api_key)])
    async def uninstall_endpoint(skill_id: str) -> dict:
        """Remove one installed skill and its ledger row, then rebuild the agents."""
        try:
            await uninstall_skill(skill_id)
        except InstallError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.payload()) from None
        await _apply_install_change(reason="skill_uninstall")
        return await asyncio.to_thread(catalog_mod.build_view)

    app.include_router(router)


__all__ = ["register_skills_management", "router"]
