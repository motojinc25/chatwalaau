"""Harness Agent Management + Authoring API (CTR-0194 / CTR-0195, PRP-0135).

CTR-0194 (management + run-target): inventory / detail / dry-run validate; the
run-target is selected per-conversation via AG-UI ``state.harness_id``
(CTR-0009), NOT a global activation -- there is no rebuild and no AgentRegistry
involvement (UDR-0119 D3). CTR-0195 (authoring): validate / create / update /
delete a ``kind: Harness`` YAML under the jailed DECLARATIVE_AGENTS_DIR; a save
auto-registers into the inventory and clears the per-conversation session cache
so edits take effect on the next turn.

All endpoints are gated by CTR-0083 (``verify_api_key``); loopback bypass
preserved. The whole surface 404s when DECLARATIVE_AGENTS_DIR is unset so the
SPA can gate its launcher by probing the list endpoint (the CTR-0182 pattern).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent.declarative.loader import _agents_dir
from app.agent.harness import authoring
from app.agent.harness.factory import preflight
from app.agent.harness.loader import load_inventory, resolve_spec
from app.agent.harness.runtime import clear_cache
from app.agent.harness.spec import HarnessAgentError
from app.auth import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/harness-agents", tags=["Harness Agents"])


class HarnessAuthoringBody(BaseModel):
    """Authoring payload: canonical ``document`` (backend serializes) or raw ``yaml``."""

    document: dict[str, Any] | None = None
    yaml: str | None = Field(default=None, max_length=200_000)
    name: str | None = Field(default=None, max_length=256)


def _require_configured() -> None:
    """404 the whole surface when no DECLARATIVE_AGENTS_DIR is configured."""
    if _agents_dir() is None:
        raise HTTPException(status_code=404, detail={"error": "harness_agents_unavailable"})


def register_harness_agents(app: FastAPI) -> None:
    """Mount the Harness Agent management + authoring endpoints (CTR-0194/0195)."""

    # ---- Authoring (CTR-0195) -- registered BEFORE the /{id} catch-all ----
    def _require_writable() -> None:
        from app.demo import is_demo_mode

        directory, writable = authoring.authoring_status()
        if is_demo_mode():
            raise HTTPException(
                status_code=409, detail={"error": "read_only", "message": "Authoring is disabled in demo mode."}
            )
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
        from app.demo import is_demo_mode

        directory, writable = authoring.authoring_status()
        return {
            "available": directory is not None and not is_demo_mode(),
            "writable": writable,
            "harness_dir": str(directory or ""),
        }

    @router.post("/authoring/validate", dependencies=[Depends(verify_api_key)])
    async def validate_authoring(body: HarnessAuthoringBody) -> dict:
        """Dry-run validate: map + preflight without persisting (UDR-0100 D6 pattern)."""
        return authoring.validate_document(body.model_dump(exclude_none=True))

    @router.get("/authoring/{agent_id:path}/source", dependencies=[Depends(verify_api_key)])
    async def read_harness_source(agent_id: str) -> dict:
        try:
            text = authoring.read_source(agent_id)
        except HarnessAgentError as exc:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from None
        return {"id": agent_id, "yaml": text, "document": authoring.document_from_yaml(text)}

    @router.post("/authoring", dependencies=[Depends(verify_api_key)])
    async def create_harness_endpoint(body: HarnessAuthoringBody) -> dict:
        _require_writable()
        try:
            new_id = authoring.create_agent(body.model_dump(exclude_none=True))
        except HarnessAgentError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_agent", "message": str(exc)}) from None
        except Exception as exc:  # malformed YAML (yaml.YAMLError) etc.
            raise HTTPException(status_code=400, detail={"error": "invalid_yaml", "message": str(exc)}) from None
        await clear_cache()
        return {"id": new_id, **load_inventory()}

    @router.put("/authoring/{agent_id:path}", dependencies=[Depends(verify_api_key)])
    async def update_harness_endpoint(agent_id: str, body: HarnessAuthoringBody) -> dict:
        _require_writable()
        try:
            authoring.update_agent(agent_id, body.model_dump(exclude_none=True))
        except HarnessAgentError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_agent", "message": str(exc)}) from None
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_yaml", "message": str(exc)}) from None
        await clear_cache()
        return {"id": agent_id, **load_inventory()}

    @router.delete("/authoring/{agent_id:path}", dependencies=[Depends(verify_api_key)])
    async def delete_harness_endpoint(agent_id: str) -> dict:
        _require_writable()
        try:
            authoring.delete_agent(agent_id)
        except HarnessAgentError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_agent", "message": str(exc)}) from None
        await clear_cache()
        return load_inventory()

    # ---- Management + run-target (CTR-0194) ----
    @router.get("", dependencies=[Depends(verify_api_key)])
    async def list_harness_agents() -> dict:
        _require_configured()
        return load_inventory()

    @router.get("/{agent_id:path}", dependencies=[Depends(verify_api_key)])
    async def get_harness_agent(agent_id: str) -> dict:
        _require_configured()
        try:
            spec = resolve_spec(agent_id)
        except HarnessAgentError as exc:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from None
        document: dict | None = None
        try:
            document = authoring.document_from_yaml(authoring.read_source(agent_id))
        except Exception:
            document = None
        from app.demo import is_demo_mode

        return {
            "id": spec.id,
            "name": spec.name,
            "display_name": spec.display_name,
            "description": spec.description,
            "group_path": list(spec.group_path),
            "warnings": spec.warnings,
            "runnable": not spec.warnings and not is_demo_mode(),
            **preflight(spec),
            "document": document,
        }

    @router.post("/{agent_id:path}/validate", dependencies=[Depends(verify_api_key)])
    async def validate_stored(agent_id: str) -> dict:
        _require_configured()
        try:
            text = authoring.read_source(agent_id)
        except HarnessAgentError as exc:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from None
        return authoring.validate_document({"yaml": text})

    app.include_router(router)


__all__ = ["register_harness_agents", "router"]
