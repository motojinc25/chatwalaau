"""Prompt Templates REST API (CTR-0047).

Provides CRUD endpoints for managing prompt templates.
Templates are stored as individual JSON files in TEMPLATES_DIR.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_201_CREATED

from app.auth import verify_api_key
from app.core.config import settings
from app.prompt_templates.models import TemplateCreate, TemplateResponse, TemplateUpdate
from app.prompt_templates.storage import TemplateStorage

router = APIRouter(prefix="/api/templates", tags=["Templates"])

_storage = TemplateStorage(Path(settings.templates_dir))


@router.get("")
async def list_templates() -> list[TemplateResponse]:
    """List all templates sorted by updated_at descending."""
    return [TemplateResponse(**t) for t in _storage.list_all()]


@router.post("", status_code=HTTP_201_CREATED, dependencies=[Depends(verify_api_key)])
async def create_template(body: TemplateCreate) -> TemplateResponse:
    """Create a new template."""
    result = _storage.create(body.model_dump())
    return TemplateResponse(**result)


@router.get("/{template_id}")
async def get_template(template_id: str) -> TemplateResponse:
    """Get a single template by ID."""
    result = _storage.get(template_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateResponse(**result)


@router.put("/{template_id}", dependencies=[Depends(verify_api_key)])
async def update_template(template_id: str, body: TemplateUpdate) -> TemplateResponse:
    """Update an existing template."""
    result = _storage.update(template_id, body.model_dump())
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateResponse(**result)


@router.delete("/{template_id}", status_code=204, dependencies=[Depends(verify_api_key)])
async def delete_template(template_id: str) -> None:
    """Delete a template."""
    if not _storage.delete(template_id):
        raise HTTPException(status_code=404, detail="Template not found")
