"""Pydantic models for OpenAI Responses API (CTR-0057, PRP-0030)."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResponsesRequest(BaseModel):
    """OpenAI Responses API request schema."""

    model_config = ConfigDict(extra="allow")

    model: str = "chatwalaau"
    input: str | list[dict[str, Any]] = Field(...)
    stream: bool = False
    previous_response_id: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None


class ResponseOutput(BaseModel):
    """A single output item in the response."""

    type: str
    role: str | None = None
    content: list[dict[str, Any]] | None = None
    name: str | None = None
    arguments: str | None = None
    call_id: str | None = None
    output: str | None = None


class UsageInfo(BaseModel):
    """Token usage information."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ResponsesResponse(BaseModel):
    """OpenAI Responses API response schema."""

    id: str
    object: str = "response"
    model: str = "chatwalaau"
    output: list[ResponseOutput] = []
    usage: UsageInfo = UsageInfo()
