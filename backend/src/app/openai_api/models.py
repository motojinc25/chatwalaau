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
    # Temporary Chat (CTR-0057 / CTR-0106, PRP-0076, UDR-0052). Opt-in; default
    # false (a normal API call is non-temporary). When true the run is
    # de-personalized (Identity-only system prompt, no User Preference Memory
    # read/write), the session is quarantine-routed and excluded from listing,
    # and previous_response_id chaining is rejected (no continuity).
    temporary: bool = False


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
