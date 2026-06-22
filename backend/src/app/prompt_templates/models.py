"""Pydantic models for Prompt Templates API (CTR-0047)."""

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    body: str = Field(..., min_length=1)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=50)
    # Optional slash command token for /prompt (CTR-0047 v2, PRP-0088). Blank ->
    # the command token is derived from the template name (CTR-0126).
    slash_command: str = Field(default="", max_length=50)


class TemplateUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    body: str = Field(..., min_length=1)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=50)
    slash_command: str = Field(default="", max_length=50)


class TemplateResponse(BaseModel):
    id: str
    name: str
    description: str
    category: str
    body: str
    slash_command: str = ""
    created_at: str
    updated_at: str
