"""Declarative Workflow Management + Authoring API (CTR-0182 / CTR-0183, PRP-0118).

CTR-0182 (management + run-target): inspect workflows and dry-run validate; the
run-target is selected per-conversation via AG-UI ``state.workflow_id`` (CTR-0009),
NOT a global activation, so there is no rebuild here (UDR-0101 D3). CTR-0183
(authoring): create / update / delete a workflow YAML under the jailed
DECLARATIVE_AGENTS_DIR; a save auto-registers into the inventory (UDR-0101 D9/D10).

All endpoints are gated by CTR-0083 (``verify_api_key``); loopback bypass preserved.
The whole surface 404s when DECLARATIVE_AGENTS_DIR is unset so the SPA can gate its
launcher by probing the list endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent.declarative.loader import _agents_dir
from app.auth import verify_api_key
from app.workflow import authoring
from app.workflow.loader import load_workflow_inventory, resolve_workflow, validate_workflow_text
from app.workflow.spec import WorkflowError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["Declarative Workflows"])


class WorkflowAuthoringBody(BaseModel):
    """Authoring payload: canonical ``document`` (backend serializes) or raw ``yaml``."""

    document: dict[str, Any] | None = None
    yaml: str | None = Field(default=None, max_length=400_000)
    name: str | None = Field(default=None, max_length=256)


class ValidateBody(BaseModel):
    """Dry-run validate payload (a raw ``yaml`` document or a structured ``document``)."""

    document: dict[str, Any] | None = None
    yaml: str | None = Field(default=None, max_length=400_000)


def _require_configured() -> None:
    """404 the whole surface when no DECLARATIVE_AGENTS_DIR is configured (UDR-0101 D12)."""
    if _agents_dir() is None:
        raise HTTPException(status_code=404, detail={"error": "workflows_unavailable"})


def register_workflows(app: FastAPI) -> None:
    """Mount the Declarative Workflow management + authoring endpoints (CTR-0182/0183)."""

    # ---- Authoring (CTR-0183) -- registered BEFORE the /{id} catch-all ----
    def _require_writable() -> None:
        from app.demo import is_demo_mode

        directory, writable = authoring.authoring_status()
        if is_demo_mode():
            raise HTTPException(status_code=409, detail={"error": "read_only", "message": "Authoring is disabled in demo mode."})
        if directory is None:
            raise HTTPException(status_code=403, detail={"error": "authoring_unavailable", "message": "DECLARATIVE_AGENTS_DIR is not configured."})
        if not writable:
            raise HTTPException(status_code=403, detail={"error": "read_only", "message": "The declarative agents directory is not writable."})

    @router.get("/authoring/status", dependencies=[Depends(verify_api_key)])
    async def authoring_status_endpoint() -> dict:
        from app.demo import is_demo_mode

        directory, writable = authoring.authoring_status()
        return {
            "available": directory is not None and not is_demo_mode(),
            "writable": writable,
            "workflows_dir": str(directory or ""),
        }

    @router.post("/authoring/validate", dependencies=[Depends(verify_api_key)])
    async def validate_authoring(body: WorkflowAuthoringBody) -> dict:
        return authoring.validate_document(body.model_dump(exclude_none=True))

    @router.get("/authoring/{workflow_id:path}/source", dependencies=[Depends(verify_api_key)])
    async def read_workflow_source(workflow_id: str) -> dict:
        try:
            text = authoring.read_source(workflow_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from None
        return {"id": workflow_id, "yaml": text, "document": authoring.document_from_yaml(text)}

    @router.post("/authoring", dependencies=[Depends(verify_api_key)])
    async def create_workflow_endpoint(body: WorkflowAuthoringBody) -> dict:
        _require_writable()
        try:
            new_id = authoring.create_workflow(body.model_dump(exclude_none=True))
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_workflow", "message": str(exc)}) from None
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_yaml", "message": str(exc)}) from None
        return {"id": new_id, **load_workflow_inventory()}

    @router.put("/authoring/{workflow_id:path}", dependencies=[Depends(verify_api_key)])
    async def update_workflow_endpoint(workflow_id: str, body: WorkflowAuthoringBody) -> dict:
        _require_writable()
        try:
            authoring.update_workflow(workflow_id, body.model_dump(exclude_none=True))
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_workflow", "message": str(exc)}) from None
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_yaml", "message": str(exc)}) from None
        return {"id": workflow_id, **load_workflow_inventory()}

    @router.delete("/authoring/{workflow_id:path}", dependencies=[Depends(verify_api_key)])
    async def delete_workflow_endpoint(workflow_id: str) -> dict:
        _require_writable()
        try:
            authoring.delete_workflow(workflow_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_workflow", "message": str(exc)}) from None
        return load_workflow_inventory()

    # ---- Management (CTR-0182) ----
    @router.get("", dependencies=[Depends(verify_api_key)])
    async def list_workflows() -> dict:
        _require_configured()
        return load_workflow_inventory()

    @router.post("/validate", dependencies=[Depends(verify_api_key)])
    async def validate_posted(body: ValidateBody) -> dict:
        _require_configured()
        if isinstance(body.yaml, str) and body.yaml.strip():
            return validate_workflow_text(body.yaml)
        if isinstance(body.document, dict):
            return authoring.validate_document({"document": body.document})
        raise HTTPException(status_code=400, detail={"error": "empty", "message": "Provide 'yaml' or 'document'."})

    @router.get("/{workflow_id:path}", dependencies=[Depends(verify_api_key)])
    async def get_workflow(workflow_id: str) -> dict:
        _require_configured()
        try:
            spec = resolve_workflow(workflow_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from None
        return {
            "id": spec.id,
            "name": spec.name,
            "description": spec.description,
            "group_path": list(spec.group_path),
            "referenced_agents": spec.referenced_agents,
            "action_kinds": spec.action_kinds,
            "warnings": spec.warnings,
        }

    @router.post("/{workflow_id:path}/validate", dependencies=[Depends(verify_api_key)])
    async def validate_stored(workflow_id: str) -> dict:
        _require_configured()
        try:
            text = authoring.read_source(workflow_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from None
        return validate_workflow_text(text)

    app.include_router(router)


__all__ = ["register_workflows", "router"]
