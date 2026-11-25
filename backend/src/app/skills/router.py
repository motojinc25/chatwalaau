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

PRP-0266 (UDR-0247) changes who may write and what can be written:

- Every write of the install surface -- refresh, install, uninstall, the source
  routes, the manifest import, the preview and diff downloads and every authoring
  write -- depends on ``verify_key_admin`` (a signed-in session, this machine or an
  ``admin`` key). The enable / disable selection and Reload above stay on
  ``verify_api_key``: they install nothing (UDR-0247 D2).
- Sources can be added by URL (``/sources``), updates are reviewed with a diff and
  applied with ``expected_revision``, skills can be written in group ``local``
  (``/local``, CTR-0289) and manifests imported and exported (``/manifest``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from app.auth import Identity, verify_api_key, verify_key_admin
from app.skills import authoring as authoring_mod
from app.skills import catalog as catalog_mod
from app.skills import manifest as manifest_mod
from app.skills import state as state_mod
from app.skills import user_sources as sources_mod
from app.skills.install import InstallError, diff_skill, install_skill, uninstall_skill
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
    # PRP-0266: the revision the update diff showed; a moved catalog answers 409.
    expected_revision: str | None = Field(default=None, max_length=256)


class SourceUrl(BaseModel):
    """A source typed by an administrator (PRP-0266 C1)."""

    url: str = Field(..., min_length=1, max_length=2048)


class SourceAdd(SourceUrl):
    id: str = Field(default="", max_length=64)
    display_name: str = Field(default="", max_length=128)
    group: str = Field(default="", max_length=64)


class SourcePatch(BaseModel):
    ref: str = Field(default="", max_length=256)


class LocalCreate(BaseModel):
    """Create a skill in group local, or duplicate one (``from_id``)."""

    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=1024)
    body: str = Field(default="", max_length=1_000_000)
    from_id: str = Field(default="", max_length=512)


class LocalSave(BaseModel):
    """One change set: text files written and paths deleted (CTR-0289)."""

    files: dict[str, str] = Field(default_factory=dict)
    deleted: list[str] = Field(default_factory=list)


class ManifestImport(BaseModel):
    document: dict[str, Any]
    apply: bool = False
    select: list[str] | None = None


def _raise(exc: InstallError) -> None:
    raise HTTPException(status_code=exc.status, detail=exc.payload()) from None


def _who(identity: Identity) -> str:
    return f"{identity.source}:{identity.subject}"


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

    @router.post("/catalog/refresh", dependencies=[Depends(verify_key_admin)])
    async def refresh_catalog_endpoint(source: str | None = None) -> dict:
        """Rebuild the catalog snapshot from every configured source, or from one.

        Per-source atomic: a source that fails keeps its previous entries, marked
        stale with the error attached (UDR-0147 D3), so one rate-limited repository
        cannot blank a working catalog.
        """
        if not catalog_mod.install_available():
            raise HTTPException(status_code=403, detail={"error": "install_disabled"})
        try:
            await catalog_mod.refresh_catalog(source or None)
        except KeyError:
            raise HTTPException(
                status_code=404, detail={"error": "unknown_source", "message": f"{source} is not an active source."}
            ) from None
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.post("/install", dependencies=[Depends(verify_key_admin)])
    async def install_endpoint(body: InstallRequest) -> dict:
        """Install or reinstall one catalog skill, then rebuild the agents."""
        try:
            await install_skill(body.id, force=body.force, expected_revision=body.expected_revision)
        except InstallError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.payload()) from None
        await _apply_install_change(reason="skill_install")
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.get("/install/{skill_id:path}/diff", dependencies=[Depends(verify_key_admin)])
    async def diff_endpoint(skill_id: str) -> dict:
        """Compare the catalog commit of one skill with what is installed (no write)."""
        try:
            return await diff_skill(skill_id)
        except InstallError as exc:
            _raise(exc)

    @router.delete("/install/{skill_id:path}", dependencies=[Depends(verify_key_admin)])
    async def uninstall_endpoint(skill_id: str) -> dict:
        """Remove one installed skill and its ledger row, then rebuild the agents."""
        try:
            await uninstall_skill(skill_id)
        except InstallError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.payload()) from None
        await _apply_install_change(reason="skill_uninstall")
        return await asyncio.to_thread(catalog_mod.build_view)

    # ------------------------------------------------------------------
    # Sources added by URL (PRP-0266 C1)
    # ------------------------------------------------------------------

    @router.post("/sources/preview", dependencies=[Depends(verify_key_admin)])
    async def preview_source_endpoint(body: SourceUrl) -> dict:
        """Resolve a URL and list the skills it would publish (writes nothing)."""
        try:
            return await sources_mod.preview_source(body.url)
        except InstallError as exc:
            _raise(exc)

    @router.post("/sources")
    async def add_source_endpoint(body: SourceAdd, identity: Identity = Depends(verify_key_admin)) -> dict:
        """Add a source (unverified) and list it into the catalog."""
        try:
            await sources_mod.add_source(
                body.url, source_id=body.id, display_name=body.display_name, group=body.group, added_by=_who(identity)
            )
        except InstallError as exc:
            _raise(exc)
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.patch("/sources/{source_id}", dependencies=[Depends(verify_key_admin)])
    async def patch_source_endpoint(source_id: str, body: SourcePatch) -> dict:
        """Pin an added source to a ref, or track its default branch (empty ref)."""
        try:
            await sources_mod.update_source_ref(source_id, body.ref)
        except InstallError as exc:
            _raise(exc)
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.delete("/sources/{source_id}", dependencies=[Depends(verify_key_admin)])
    async def remove_source_endpoint(source_id: str) -> dict:
        """Remove an added source and its catalog block; installed skills stay."""
        try:
            sources_mod.remove_source(source_id)
        except InstallError as exc:
            _raise(exc)
        return await asyncio.to_thread(catalog_mod.build_view)

    # ------------------------------------------------------------------
    # Manifests (PRP-0266 C6)
    # ------------------------------------------------------------------

    @router.get("/manifest", dependencies=[Depends(verify_api_key)])
    async def export_manifest_endpoint() -> dict:
        """The ChatWalaau manifest (chatwalaau-skills.json) of this deployment."""
        return await asyncio.to_thread(manifest_mod.export_manifest)

    @router.post("/manifest/import")
    async def import_manifest_endpoint(body: ManifestImport, identity: Identity = Depends(verify_key_admin)) -> dict:
        """Preview, or with ``apply`` perform, a skills-lock.json or manifest import."""
        try:
            plan = await manifest_mod.import_manifest(
                body.document, apply=body.apply, select=body.select, added_by=_who(identity)
            )
        except InstallError as exc:
            _raise(exc)
        if body.apply and any(r.get("status") == "installed" for r in plan.get("results", [])):
            await _apply_install_change(reason="skill_manifest_import")
        plan["catalog"] = await asyncio.to_thread(catalog_mod.build_view)
        return plan

    # ------------------------------------------------------------------
    # Skills written in the product (CTR-0289, PRP-0266 C5)
    # ------------------------------------------------------------------

    @router.post("/local", dependencies=[Depends(verify_key_admin)])
    async def create_local_endpoint(body: LocalCreate) -> dict:
        """Create local/<name>, or duplicate ``from_id`` into it."""
        try:
            if body.from_id:
                record = await authoring_mod.duplicate(body.from_id, body.name)
            else:
                record = await authoring_mod.create_local(body.name, body.description, body.body)
        except InstallError as exc:
            _raise(exc)
        await _apply_install_change(reason="skill_local_create")
        return {"skill": record.to_json(), "catalog": await asyncio.to_thread(catalog_mod.build_view)}

    @router.post("/local/import", dependencies=[Depends(verify_key_admin)])
    async def import_local_endpoint(file: UploadFile = File(...), name: str = Form(default="")) -> dict:
        """Create local/<name> from a ZIP holding one skill."""
        data = await file.read()
        try:
            record = await authoring_mod.import_zip(data, name)
        except InstallError as exc:
            _raise(exc)
        await _apply_install_change(reason="skill_local_import")
        return {"skill": record.to_json(), "catalog": await asyncio.to_thread(catalog_mod.build_view)}

    @router.get("/local/{name}", dependencies=[Depends(verify_api_key)])
    async def get_local_endpoint(name: str) -> dict:
        """The files of local/<name> (text files with their content)."""
        try:
            return await asyncio.to_thread(authoring_mod.get_local, name)
        except InstallError as exc:
            _raise(exc)

    @router.put("/local/{name}", dependencies=[Depends(verify_key_admin)])
    async def save_local_endpoint(name: str, body: LocalSave) -> dict:
        """Save one change set to local/<name>: one write, one rebuild."""
        try:
            record = await authoring_mod.save_local(name, body.files, body.deleted)
        except InstallError as exc:
            _raise(exc)
        await _apply_install_change(reason="skill_local_save")
        return {"skill": record.to_json(), "catalog": await asyncio.to_thread(catalog_mod.build_view)}

    @router.delete("/local/{name}", dependencies=[Depends(verify_key_admin)])
    async def delete_local_endpoint(name: str) -> dict:
        """Delete local/<name> and its ledger row."""
        try:
            await authoring_mod.delete_local(name)
        except InstallError as exc:
            _raise(exc)
        await _apply_install_change(reason="skill_local_delete")
        return await asyncio.to_thread(catalog_mod.build_view)

    @router.get("/{group}/{name}/export.zip", dependencies=[Depends(verify_api_key)])
    async def export_zip_endpoint(group: str, name: str) -> Response:
        """Download one skill on disk as a ZIP."""
        try:
            filename, data = await asyncio.to_thread(authoring_mod.export_zip, group, name)
        except InstallError as exc:
            _raise(exc)
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    app.include_router(router)


__all__ = ["register_skills_management", "router"]
