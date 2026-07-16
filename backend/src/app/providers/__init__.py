"""Base-model provider registry and dispatch (CTR-0102, PRP-0069, UDR-0045).

Single seam behind ``app.agui.agent_registry._build_chat_client``. Given a
model id (a catalog offering id), dispatch to the owning provider to build the
MAF ChatClient, the per-model ``default_options``, and the provider-supplied
web search tool.

Model -> provider routing comes SOLELY from the Model Offering Catalog
(``model_offerings.jsonc``; PRP-0113 / UDR-0094 D1). The legacy per-provider
env namespaces (``AZURE_OPENAI_MODELS`` / ``ANTHROPIC_MODELS`` /
``OPENAI_MODELS`` / ``FOUNDRY_MODELS``) have been REMOVED -- there is no
env-namespace routing lane. ``resolve_models()`` returns the catalog's chat
offerings in authored file order (UDR-0093 D1) and is empty for a non-demo
deployment with no catalog (the agent registry fail-fasts, UDR-0094 D1).
DEMO_MODE never reaches this module -- the registry short-circuits to
DemoChatClient before dispatch (UDR-0045 D7).
"""

from __future__ import annotations

from typing import Any

from app import models_catalog
from app.models_catalog import DEFAULT_CONTEXT_WINDOW
from app.providers.anthropic import AnthropicProvider, anthropic_web_search_tool
from app.providers.azure_openai import AzureOpenAIProvider, openai_web_search_tool
from app.providers.base import Provider
from app.providers.foundry import FoundryProvider
from app.providers.openai import OpenAIProvider

_AZURE = AzureOpenAIProvider()
_ANTHROPIC = AnthropicProvider()
_OPENAI = OpenAIProvider()
_FOUNDRY = FoundryProvider()

# Ordered for merged-list construction: Azure, Anthropic, OpenAI, then Foundry.
_ORDERED: tuple[Provider, ...] = (_AZURE, _ANTHROPIC, _OPENAI, _FOUNDRY)
PROVIDERS: dict[str, Provider] = {p.name: p for p in _ORDERED}


def resolve_models() -> list[tuple[str, str]]:
    """Return the ordered ``(model, provider_name)`` list from the catalog.

    Chat routing comes SOLELY from the Model Offering Catalog (PRP-0113 /
    UDR-0094 D1): the chat offerings in AUTHORED FILE ORDER (UDR-0093 D1 -- the
    default is NOT hoisted); ``model`` is the offering id and ``provider_name``
    its declared provider. Returns ``[]`` for a non-demo deployment with no
    catalog -- the agent registry surfaces that as a fail-fast (UDR-0094 D1).

    This list is PRESENTATION ORDER. It does not encode the default model -- ask
    :func:`resolve_default_model` for that.
    """
    catalog = models_catalog.active_catalog()
    if catalog is not None:
        return [(o.id, o.provider) for o in catalog.chat_offerings()]
    return []


def resolve_default_model() -> str:
    """Return the model that is PRESELECTED, independent of display order.

    UDR-0093 D2. Presentation order is the operator's (UDR-0093 D1), so position
    no longer implies default and reading it positionally would silently promote
    whichever model happens to be listed first. The default is asked of the
    catalog explicitly. Returns ``""`` for a non-demo deployment with no catalog
    (UDR-0094 D1).
    """
    catalog = models_catalog.active_catalog()
    if catalog is not None:
        default = catalog.default_offering()
        return default.id if default is not None else ""
    return ""


def provider_for(model: str) -> Provider:
    """Return the Provider that owns ``model`` (defaults to azure-openai).

    The provider comes from the model's catalog offering (PRP-0113 / UDR-0094).
    An unknown model (no active catalog, or an id not in the catalog) defaults to
    azure-openai so read-only capability lookups never raise.
    """
    catalog = models_catalog.active_catalog()
    if catalog is not None:
        offering = catalog.get(model)
        if offering is not None:
            return PROVIDERS.get(offering.provider, _AZURE)
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


def structured_output_support(model: str) -> dict[str, Any]:
    """Per-model structured-output capability descriptor (CTR-0102 v5, UDR-0058)."""
    return provider_for(model).structured_output_support(model)


def build_structured_output(model: str, schema: dict[str, Any] | None, mode: str) -> dict[str, Any]:
    """Provider-native structured-output run-options fragment (CTR-0102 v5, UDR-0058 D2)."""
    return provider_for(model).build_structured_output(model, schema, mode)


def structured_output_map(models: list[str]) -> dict[str, dict[str, Any]]:
    """model -> structured-output capability for the GET /api/model selector (CTR-0069 v5)."""
    return {model: structured_output_support(model) for model in models}


def merge_generation_options(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge ``extra`` run-options into ``base``, deep-merging known nested option
    objects so sibling keys coexist (PRP-0082).

    Both ``build_model_options`` and ``build_structured_output`` can set the same
    top-level container: OpenAI ``text`` (``text.verbosity`` + ``text.format``) and
    Anthropic ``output_config`` (``output_config.effort`` + ``output_config.format``).
    A plain ``dict.update`` would clobber one with the other; this deep-merges those
    two keys one level down and shallow-merges everything else. Mutates and returns
    ``base``.
    """
    for key, val in extra.items():
        if key in ("text", "output_config") and isinstance(base.get(key), dict) and isinstance(val, dict):
            base[key] = {**base[key], **val}
        else:
            base[key] = val
    return base


def background_supported(model: str) -> bool:
    """Whether ``model``'s provider supports background responses (CTR-0045).

    Azure OpenAI / OpenAI -> True, Anthropic -> False, Foundry -> False
    (verification-gated, UDR-0085 D6). Unknown models resolve via
    ``provider_for`` (defaults to azure-openai). Used by the AG-UI run-option
    guard and the GET /api/model capability map (PRP-0073).
    """
    return bool(getattr(provider_for(model), "supports_background", False))


def background_supported_map(models: list[str]) -> dict[str, bool]:
    """model -> background support flag for the GET /api/model selector (CTR-0041)."""
    return {model: background_supported(model) for model in models}


def get_max_context_tokens(model: str | None = None) -> int:
    """Resolve max context tokens for ``model`` from the catalog (CTR-0069, PRP-0113).

    An offering's declared ``context_window`` wins; when unset it defaults to the
    single backend constant ``DEFAULT_CONTEXT_WINDOW`` (UDR-0094 D5, replacing the
    removed ``MODEL_MAX_CONTEXT_TOKENS`` env config). ``model=None`` resolves to
    the catalog default offering. Returns the default constant when no catalog is
    active (non-demo no-catalog is a fail-fast elsewhere; this stays non-raising).
    """
    catalog = models_catalog.active_catalog()
    if catalog is not None:
        resolved = model
        if resolved is None:
            default = catalog.default_offering()
            resolved = default.id if default is not None else None
        offering = catalog.get(resolved)
        if offering is not None and offering.context_window is not None:
            return offering.context_window
    return DEFAULT_CONTEXT_WINDOW


def max_context_tokens_map(models: list[str]) -> dict[str, int]:
    """model -> max_context_tokens for the GET /api/model selector (CTR-0069, PRP-0109)."""
    return {model: get_max_context_tokens(model) for model in models}


__all__ = [
    "PROVIDERS",
    "Provider",
    "anthropic_web_search_tool",
    "background_supported",
    "background_supported_map",
    "build_chat_client",
    "build_model_options",
    "build_structured_output",
    "get_max_context_tokens",
    "max_context_tokens_map",
    "merge_generation_options",
    "model_options_catalog",
    "model_options_map",
    "openai_web_search_tool",
    "provider_for",
    "reasoning_catalog",
    "reasoning_options_map",
    "resolve_effort",
    "resolve_models",
    "resolve_options",
    "structured_output_map",
    "structured_output_support",
    "web_search_tool",
]
