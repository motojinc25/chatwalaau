"""Base-model provider registry and dispatch (CTR-0102, PRP-0069, UDR-0045).

Single seam behind ``app.agui.agent_registry._build_chat_client``. Given a
model id, dispatch to the owning provider to build the MAF ChatClient, the
per-model ``default_options``, and the provider-supplied web search tool.

Model -> provider routing comes from the per-provider env namespaces
(``AZURE_OPENAI_MODELS`` -> azure-openai, ``ANTHROPIC_MODELS`` -> anthropic,
``OPENAI_MODELS`` -> openai, ``FOUNDRY_MODELS`` -> foundry).
``resolve_models()`` is the SSOT for the merged ordered list (Azure first,
then Anthropic, then OpenAI, then Foundry; UDR-0045 D3 / UDR-0073 D3 /
UDR-0085 D3). DEMO_MODE never reaches this module -- the registry
short-circuits to DemoChatClient before dispatch (UDR-0045 D7).
"""

from __future__ import annotations

from typing import Any

from app import models_catalog
from app.core.config import settings
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
    """Return the merged ordered ``(model, provider_name)`` list.

    Two lanes (PRP-0109, UDR-0087):

    - CATALOG lane (``model_offerings.jsonc`` present): the chat offerings in the
      AUTHORED FILE ORDER (UDR-0093 D1 -- the default is NOT hoisted); ``model`` is
      the offering id and ``provider_name`` its declared provider. The legacy env
      namespaces are ignored (D11).
    - LEGACY lane (no catalog / DEMO_MODE): the merged per-namespace list --
      Azure models first (preserving the pre-PRP-0069 default), then Anthropic,
      then OpenAI, then Foundry (UDR-0045 D3 / UDR-0073 D3 / UDR-0085 D3). Ids
      are unique across providers (enforced by Settings._validate_anthropic); a
      defensive de-dup keeps the first occurrence if a duplicate slips through.

    This list is PRESENTATION ORDER. It no longer encodes the default model -- ask
    :func:`resolve_default_model` for that.
    """
    catalog = models_catalog.active_catalog()
    if catalog is not None:
        return [(o.id, o.provider) for o in catalog.chat_offerings()]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for provider in _ORDERED:
        for model in provider.models():
            if model in seen:
                continue
            seen.add(model)
            out.append((model, provider.name))
    return out


def resolve_default_model() -> str:
    """Return the model that is PRESELECTED, independent of display order.

    UDR-0093 D2. Before PRP-0112 the registry took ``resolve_models()[0]``, which
    was correct only because :meth:`Catalog.chat_offerings` hoisted the ``default``
    offering to index 0. Now that presentation order is the operator's (UDR-0093
    D1), position no longer implies default, and reading it positionally would
    silently promote whichever model happens to be listed first -- a wrong-model bug
    with no type error and no exception. So the catalog lane asks the catalog.

    The LEGACY lane (no catalog file) has no ``default`` flag, so "the first model
    of the merged namespace order" remains its definition of default, unchanged.
    """
    catalog = models_catalog.active_catalog()
    if catalog is not None:
        default = catalog.default_offering()
        return default.id if default is not None else ""
    models = resolve_models()
    return models[0][0] if models else ""


def provider_for(model: str) -> Provider:
    """Return the Provider that owns ``model`` (defaults to azure-openai).

    On the catalog lane the provider comes from the offering (PRP-0109); on the
    legacy lane it comes from the env namespace that lists the model.
    """
    catalog = models_catalog.active_catalog()
    if catalog is not None:
        offering = catalog.get(model)
        if offering is not None:
            return PROVIDERS.get(offering.provider, _AZURE)
        return _AZURE
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
    """Resolve max context tokens for ``model``, catalog-aware (CTR-0069, PRP-0109).

    On the catalog lane an offering's declared ``context_window`` wins; otherwise
    (and on the legacy lane) the ``MODEL_MAX_CONTEXT_TOKENS`` env config applies
    via ``settings.get_max_context_tokens`` (byte-for-byte on the legacy lane).
    ``model=None`` resolves to the catalog default offering when a catalog is
    active, else the legacy default model.
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
        return settings.get_max_context_tokens(resolved)
    return settings.get_max_context_tokens(model)


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
