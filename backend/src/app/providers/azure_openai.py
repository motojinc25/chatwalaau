"""Azure OpenAI base-model provider (CTR-0102, PRP-0069, UDR-0045).

Wraps the existing ``OpenAIChatClient`` + ``app.azure_credential`` lane
(UDR-0034, unchanged) behind the Provider seam. This provider reproduces the
pre-PRP-0069 behavior exactly: the same client kwargs, the same hosted web
search tool, and the same ``reasoning.effort`` option shape.
"""

from __future__ import annotations

from typing import Any

from agent_framework_openai import OpenAIChatClient

from app.azure_credential import get_chat_client_credential_kwargs
from app.core.config import settings

NAME = "azure-openai"

# Reasoning effort catalog (PRP-0071, UDR-0047 D2/D3). The OpenAI Responses API
# accepts none / minimal / low / medium / high / xhigh, but the reasoning-only
# policy hides none / minimal -- only low and above are offered (UDR-0047 D3).
OPENAI_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
OPENAI_EFFORT_DEFAULT = "medium"


def openai_web_search_tool() -> Any:
    """Build the OpenAI hosted web search tool (country-scoped).

    Exposed at module scope so the DEMO path (which bypasses provider
    dispatch) can reuse the identical tool and preserve byte-for-byte demo
    behavior (UDR-0045 D7). ``get_web_search_tool`` is a static factory and
    needs no credentials.
    """
    return OpenAIChatClient.get_web_search_tool(
        user_location={"type": "approximate", "country": settings.web_search_country},
    )


class AzureOpenAIProvider:
    """Provider for Azure OpenAI deployments (default provider)."""

    name = NAME

    def models(self) -> list[str]:
        return settings.model_list

    def build_chat_client(self, model: str) -> Any:
        return OpenAIChatClient(
            model=model,
            azure_endpoint=settings.azure_openai_endpoint or None,
            **get_chat_client_credential_kwargs(),
        )

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        # Fixed, backend-owned allowed list + default (PRP-0071, UDR-0047 D2).
        # Azure OpenAI default reasoning effort is always `medium`; the operator
        # picks per message in the UI. Not env-configurable.
        return {"allowed": list(OPENAI_EFFORT_LEVELS), "default": OPENAI_EFFORT_DEFAULT}

    def build_model_options(self, model: str, effort: str | None = None) -> dict[str, Any]:
        # Reasoning-only policy: always send reasoning.effort (UDR-0047 D3).
        # Requested effort wins when allowed; otherwise the catalog default.
        catalog = self.reasoning_catalog(model)
        chosen = effort if effort in catalog["allowed"] else catalog["default"]
        return {"reasoning": {"effort": chosen, "summary": "detailed"}}

    def web_search_tool(self, model: str) -> Any:
        return openai_web_search_tool()
