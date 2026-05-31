"""Base-model provider registry and dispatch (CTR-0102, PRP-0069, UDR-0045).

Single seam behind ``app.agui.agent_registry._build_chat_client``. Given a
model id, dispatch to the owning provider to build the MAF ChatClient, the
per-model ``default_options``, and the provider-supplied web search tool.

Model -> provider routing comes from the per-provider env namespaces
(``AZURE_OPENAI_MODELS`` -> azure-openai, ``ANTHROPIC_MODELS`` -> anthropic).
``resolve_models()`` is the SSOT for the merged ordered list (Azure first,
then Anthropic; UDR-0045 D3). DEMO_MODE never reaches this module -- the
registry short-circuits to DemoChatClient before dispatch (UDR-0045 D7).
"""

from __future__ import annotations

from typing import Any

from app.providers.anthropic import AnthropicProvider, anthropic_web_search_tool
from app.providers.azure_openai import AzureOpenAIProvider, openai_web_search_tool
from app.providers.base import Provider

_AZURE = AzureOpenAIProvider()
_ANTHROPIC = AnthropicProvider()

# Ordered for merged-list construction: Azure first, then Anthropic.
_ORDERED: tuple[Provider, ...] = (_AZURE, _ANTHROPIC)
PROVIDERS: dict[str, Provider] = {p.name: p for p in _ORDERED}


def resolve_models() -> list[tuple[str, str]]:
    """Return the merged ordered ``(model, provider_name)`` list (UDR-0045 D3).

    Azure models first (preserving the pre-PRP-0069 default), then Anthropic.
    Ids are unique across providers (enforced by Settings._validate_anthropic);
    a defensive de-dup keeps the first occurrence if a duplicate slips through.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for provider in _ORDERED:
        for model in provider.models():
            if model in seen:
                continue
            seen.add(model)
            out.append((model, provider.name))
    return out


def provider_for(model: str) -> Provider:
    """Return the Provider that owns ``model`` (defaults to azure-openai)."""
    for candidate, name in resolve_models():
        if candidate == model:
            return PROVIDERS[name]
    return _AZURE


def build_chat_client(model: str) -> Any:
    return provider_for(model).build_chat_client(model)


def build_model_options(model: str) -> dict[str, Any]:
    return provider_for(model).build_model_options(model)


def web_search_tool(model: str) -> Any | None:
    return provider_for(model).web_search_tool(model)


__all__ = [
    "PROVIDERS",
    "Provider",
    "anthropic_web_search_tool",
    "build_chat_client",
    "build_model_options",
    "openai_web_search_tool",
    "provider_for",
    "resolve_models",
    "web_search_tool",
]
