"""Declarative Agent Management API (CTR-0143, PRP-0094, UDR-0072).

Three endpoints let an operator inspect and switch the active declarative agent at
runtime; switching one agent rebuilds all per-model agents atomically (CTR-0070):

    GET  /api/agents          -- inventory: CORE + custom (nested tree) with the
                                 active flag and per-agent loaded / error state.
    PUT  /api/agents/active   -- activate ONE agent: validate the mapped spec, set
                                 it active, and REBUILD all per-model agents,
                                 responding only AFTER the rebuild (drives the SPA
                                 "rebuilding" indicator). Validation failure -> 400,
                                 nothing activated.
    POST /api/agents/reload   -- re-scan DECLARATIVE_AGENTS_DIR and rebuild (so
                                 added/removed YAML is picked up without a restart).

All endpoints are gated by CTR-0083 (``verify_api_key``); loopback bypass keeps
localhost-first development zero-config (UDR-0072 D10). The active selection is
in-memory only -- a restart re-initializes to CORE (UDR-0072 D7). Switching is
SPA-only; the OpenAI Responses API and Teams FOLLOW the active agent (UDR-0072 D10).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent.declarative import authoring
from app.agent.declarative.loader import load_inventory, resolve_spec
from app.agent.declarative.spec import CORE_AGENT_ID, DeclarativeAgentError
from app.agent.declarative.store import get_active_store, log_active_agent
from app.agent.declarative.tool_inventory import get_tool_inventory
from app.auth import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["Declarative Agents"])


class ActivateSelection(BaseModel):
    """Desired active declarative agent."""

    id: str = Field(..., min_length=1, max_length=512)


class AuthoringBody(BaseModel):
    """Authoring payload: canonical ``document`` (backend serializes) or raw ``yaml``.

    The form/canvas path sends ``document`` (UDR-0100 D8); the monaco raw-edit escape
    hatch sends ``yaml``. ``name`` optionally overrides the filename derivation on
    create. At least one of ``document`` / ``yaml`` must be present (validated by the
    authoring layer).
    """

    document: dict[str, Any] | None = None
    yaml: str | None = Field(default=None, max_length=200_000)
    name: str | None = Field(default=None, max_length=256)


def register_declarative_agents(app: FastAPI, *, agent_registry) -> None:
    """Mount the Declarative Agent Management endpoints, closing over the registry.

    The router needs the live ``AgentRegistry`` (CTR-0070) so PUT/POST can rebuild
    it; the registry is the module-level singleton created in ``app.main`` and shared
    by reference with the AG-UI / OpenAI-API / Teams surfaces (UDR-0072 D10).
    """

    @router.get("", dependencies=[Depends(verify_api_key)])
    async def list_agents() -> dict:
        """Return the declarative-agent inventory with the active flag + state."""
        return load_inventory(get_active_store().active_id())

    @router.put("/active", dependencies=[Depends(verify_api_key)])
    async def activate_agent(body: ActivateSelection) -> dict:
        """Validate + activate ONE agent, rebuild the agents, return the inventory.

        On a mapping/validation failure -> 400 and nothing is activated (D9). On a
        rebuild failure the active id is rolled back to its prior value so it stays
        consistent with the still-installed prior agents (UDR-0072 D8).
        """
        store = get_active_store()

        # Validate BEFORE changing anything (UDR-0072 D9): resolve_spec parses, maps,
        # rejects incompatible options (e.g. temperature) / malformed YAML / an unknown
        # id (raising DeclarativeAgentError), and annotates non-fatal warnings (ignored
        # connection, unknown / invalid options, unconfigured model). A YAML that maps
        # with ANY warning is NOT activatable -- the operator must fix it first -- so the
        # active agent is always a clean, fully-mapped spec.
        try:
            spec = resolve_spec(body.id)
        except DeclarativeAgentError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_agent", "message": str(exc)}) from None
        if spec.warnings:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "agent_has_warnings",
                    "message": "This agent cannot be activated until its warnings are resolved.",
                    "warnings": spec.warnings,
                },
            )

        prior = store.snapshot()
        store.set_active(body.id)
        try:
            # Local import avoids a module-level import cycle
            # (agent_factory -> ... -> app.agent.declarative.*).
            from app.agui.agent_factory import rebuild_agent_registry

            await rebuild_agent_registry(agent_registry)
        except Exception:
            store.set_active(prior)
            logger.exception("Declarative agent activation failed during agent rebuild")
            raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None

        log_active_agent()
        return load_inventory(store.active_id())

    @router.post("/reload", dependencies=[Depends(verify_api_key)])
    async def reload_agents() -> dict:
        """Re-scan DECLARATIVE_AGENTS_DIR and rebuild so on-disk changes apply.

        If the active agent's YAML disappeared or stopped mapping, the rebuild
        (via ``active_spec``) falls back to CORE; the active id is then reconciled
        to CORE so the inventory and the running agents agree.
        """
        store = get_active_store()
        try:
            from app.agui.agent_factory import rebuild_agent_registry

            await rebuild_agent_registry(agent_registry)
        except Exception:
            logger.exception("Declarative agent reload failed during agent rebuild")
            raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None

        # Reconcile: if the active id no longer resolves, active_spec() reset it to CORE.
        active = store.active_id()
        if active != "core":
            try:
                resolve_spec(active)
            except DeclarativeAgentError:
                store.reset()
        log_active_agent()
        return load_inventory(store.active_id())

    # ---- Tool inventory (CTR-0178, PRP-0117) ----
    @router.get("/tool-inventory", dependencies=[Depends(verify_api_key)])
    async def tool_inventory() -> dict:
        """Return the selectable tool surface (function / MCP / Skills) for authoring."""
        return await get_tool_inventory()

    # ---- Authoring (CTR-0177, PRP-0117) ----
    async def _rebuild() -> None:
        """Rebuild the agents so a written/deleted agent auto-registers (UDR-0100 D5)."""
        from app.agui.agent_factory import rebuild_agent_registry

        await rebuild_agent_registry(agent_registry)

    def _require_writable() -> None:
        """Guard: authoring needs a writable DECLARATIVE_AGENTS_DIR (and non-demo)."""
        from app.demo import is_demo_mode

        directory, writable = authoring.authoring_status()
        if is_demo_mode():
            raise HTTPException(status_code=409, detail={"error": "read_only", "message": "Authoring is disabled in demo mode."})
        if directory is None:
            raise HTTPException(
                status_code=403,
                detail={"error": "authoring_unavailable", "message": "DECLARATIVE_AGENTS_DIR is not configured."},
            )
        if not writable:
            raise HTTPException(
                status_code=403,
                detail={"error": "read_only", "message": "The declarative agents directory is not writable."},
            )

    @router.get("/authoring/status", dependencies=[Depends(verify_api_key)])
    async def authoring_status_endpoint() -> dict:
        """Report whether authoring is available + writable (drives the Create button)."""
        from app.demo import is_demo_mode

        directory, writable = authoring.authoring_status()
        return {
            "available": directory is not None and not is_demo_mode(),
            "writable": writable,
            "agents_dir": str(directory or ""),
        }

    @router.post("/authoring/validate", dependencies=[Depends(verify_api_key)])
    async def validate_authoring(body: AuthoringBody) -> dict:
        """Dry-run validate: map without persisting; return summary + warnings (D6)."""
        return authoring.validate_document(body.model_dump(exclude_none=True))

    @router.get("/authoring/{agent_id:path}/source", dependencies=[Depends(verify_api_key)])
    async def read_agent_source(agent_id: str) -> dict:
        """Return the raw canonical YAML of a custom agent for editing."""
        try:
            text = authoring.read_source(agent_id)
        except DeclarativeAgentError as exc:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from None
        return {"id": agent_id, "yaml": text, "document": authoring.document_from_yaml(text)}

    @router.post("/authoring", dependencies=[Depends(verify_api_key)])
    async def create_agent_endpoint(body: AuthoringBody) -> dict:
        """Create a new agent file, rebuild, and return the id + refreshed inventory."""
        _require_writable()
        try:
            new_id = authoring.create_agent(body.model_dump(exclude_none=True))
        except DeclarativeAgentError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_agent", "message": str(exc)}) from None
        except Exception as exc:  # malformed YAML (yaml.YAMLError) etc.
            raise HTTPException(status_code=400, detail={"error": "invalid_yaml", "message": str(exc)}) from None
        await _rebuild()
        log_active_agent()
        return {"id": new_id, **load_inventory(get_active_store().active_id())}

    @router.put("/authoring/{agent_id:path}", dependencies=[Depends(verify_api_key)])
    async def update_agent_endpoint(agent_id: str, body: AuthoringBody) -> dict:
        """Overwrite an existing agent file in place, rebuild, and return the inventory."""
        _require_writable()
        try:
            authoring.update_agent(agent_id, body.model_dump(exclude_none=True))
        except DeclarativeAgentError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_agent", "message": str(exc)}) from None
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_yaml", "message": str(exc)}) from None
        await _rebuild()
        log_active_agent()
        return {"id": agent_id, **load_inventory(get_active_store().active_id())}

    @router.delete("/authoring/{agent_id:path}", dependencies=[Depends(verify_api_key)])
    async def delete_agent_endpoint(agent_id: str) -> dict:
        """Delete a custom agent file, reconcile the active id to CORE, and rebuild."""
        _require_writable()
        store = get_active_store()
        try:
            authoring.delete_agent(agent_id)
        except DeclarativeAgentError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_agent", "message": str(exc)}) from None
        # If the deleted agent was active, fall back to CORE before rebuilding.
        if store.active_id() == agent_id:
            store.reset()
        await _rebuild()
        # Reconcile in case the active id no longer resolves.
        active = store.active_id()
        if active != CORE_AGENT_ID:
            try:
                resolve_spec(active)
            except DeclarativeAgentError:
                store.reset()
        log_active_agent()
        return load_inventory(store.active_id())

    app.include_router(router)


__all__ = ["register_declarative_agents", "router"]
