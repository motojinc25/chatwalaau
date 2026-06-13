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


def build_model_options(model: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    return provider_for(model).build_model_options(model, selected)


def model_options_catalog(model: str) -> dict[str, Any]:
    """Return ``{"options": [descriptor, ...]}`` for ``model`` (CTR-0102 v4)."""
    return provider_for(model).model_options_catalog(model)


def reasoning_catalog(model: str) -> dict[str, Any]:
    """Return ``{"allowed": [...], "default": ...}`` for ``model`` (CTR-0102 v2)."""
    return provider_for(model).reasoning_catalog(model)


def resolve_effort(model: str, requested: str | None) -> str:
    """Resolve a requested effort to a valid one for ``model`` (UDR-0047 D4).

    Returns ``requested`` when it is in the model's allowed list; otherwise the
    model's catalog default (also for ``None`` / unknown). Never raises, so a
    bad ``state.reasoning`` can never produce a provider validation error.
    """
    catalog = provider_for(model).reasoning_catalog(model)
    if requested in catalog["allowed"]:
        return requested  # type: ignore[return-value]
    return catalog["default"]


def resolve_options(model: str, requested: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve a requested option selection against ``model``'s catalog (UDR-0057 D7).

    Returns ``{key: value}`` for EVERY option the model advertises: a valid /
    in-range requested value wins, otherwise the descriptor default. Never raises,
    so a bad ``state.model_options`` can never produce a provider validation
    error. Used by the AG-UI endpoint both to build the per-request options and to
    echo the resolved selections on the usage event (CTR-0009).
    """
    requested = requested or {}
    resolved: dict[str, Any] = {}
    for desc in model_options_catalog(model)["options"]:
        key = desc["key"]
        val = requested.get(key)
        if desc.get("kind") == "number":
            lo, hi = desc.get("min"), desc.get("max")
            if isinstance(val, (int, float)) and not isinstance(val, bool) and lo <= val <= hi:
                resolved[key] = val
            else:
                resolved[key] = desc.get("default")
        else:  # enum (default)
            resolved[key] = val if val in desc.get("allowed", []) else desc.get("default")
    return resolved


def reasoning_options_map(models: list[str]) -> dict[str, dict[str, Any]]:
    """model -> reasoning catalog map for the GET /api/model selector (CTR-0069)."""
    return {model: reasoning_catalog(model) for model in models}


def model_options_map(models: list[str]) -> dict[str, dict[str, Any]]:
    """model -> generalized option catalog for the GET /api/model selector (CTR-0069 v4)."""
    return {model: model_options_catalog(model) for model in models}


def web_search_tool(model: str) -> Any | None:
    return provider_for(model).web_search_tool(model)


def background_supported(model: str) -> bool:
    """Whether ``model``'s provider supports background responses (CTR-0045).

    Azure OpenAI -> True, Anthropic -> False. Unknown models resolve via
    ``provider_for`` (defaults to azure-openai). Used by the AG-UI run-option
    guard and the GET /api/model capability map (PRP-0073).
    """
    return bool(getattr(provider_for(model), "supports_background", False))


def background_supported_map(models: list[str]) -> dict[str, bool]:
    """model -> background support flag for the GET /api/model selector (CTR-0041)."""
    return {model: background_supported(model) for model in models}


__all__ = [
    "PROVIDERS",
    "Provider",
    "anthropic_web_search_tool",
    "background_supported",
    "background_supported_map",
    "build_chat_client",
    "build_model_options",
    "model_options_catalog",
    "model_options_map",
    "openai_web_search_tool",
    "provider_for",
    "reasoning_catalog",
    "reasoning_options_map",
    "resolve_effort",
    "resolve_models",
    "resolve_options",
    "web_search_tool",
]
