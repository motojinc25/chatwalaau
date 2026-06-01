"""Anthropic (Claude) base-model provider (CTR-0102, PRP-0069, UDR-0045).

Wraps the first-party MAF connectors behind the Provider seam:

- Direct  (ANTHROPIC_HOSTING=direct)  -> ``AnthropicClient`` against the
  Anthropic public API, authenticated by ``ANTHROPIC_API_KEY``
  (+ optional ``ANTHROPIC_BASE_URL``).
- Foundry (ANTHROPIC_HOSTING=foundry) -> ``AnthropicFoundryClient`` against
  Anthropic on Azure AI Foundry. Endpoint: ``ANTHROPIC_FOUNDRY_RESOURCE`` (the
  resource name / subdomain, expanded to
  ``https://<resource>.services.ai.azure.com/anthropic/``) OR the full
  ``ANTHROPIC_FOUNDRY_BASE_URL``. Auth (mutually exclusive in the SDK): the
  ``api-key`` header from ``ANTHROPIC_FOUNDRY_API_KEY`` when set, otherwise an
  Entra ID ``Authorization: Bearer`` token from the mode-selected Azure
  credential (cli / managed-identity / default, AZURE_CREDENTIAL_MODE) via
  ``app.azure_credential.get_token_provider()`` (PRP-0071 follow-up; UDR-0045
  D4 amends the D8 API-key-only deferral).

Both clients are MAF ``BaseChatClient`` subclasses, so the Agent layer treats
them exactly like ``OpenAIChatClient`` (UDR-0045 D1/D9). Connector imports are
lazy so an Azure-only deployment never pays the import cost.

Web search: ``AnthropicClient.get_web_search_tool()`` returns a hosted-tool
dict (``{"type": "web_search_20250305", "name": "web_search"}``) that the MAF
Anthropic connector knows how to round-trip (it handles
``web_search_tool_result`` and ``web_search_result_location`` content blocks
during deserialization). The OpenAI and Anthropic tool dicts have different
shapes (OpenAI takes ``user_location``; Anthropic takes no parameters), but
the abstraction sits at the ``Provider.web_search_tool(model)`` seam: each
provider returns the right dict for its own connector, and the registry
appends whichever non-None tool the active model's provider supplies. This
follow-up supersedes the original UDR-0045 D5 deferral for Anthropic.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

NAME = "anthropic"

# Reasoning effort catalog (PRP-0071, UDR-0047 D2). Anthropic adaptive-thinking
# effort levels for Claude Opus 4.7 / 4.8: low / medium / high / xhigh / max
# (xhigh sits between high and max; available on Opus 4.7+). The effort is sent
# in a separate output_config object (NOT inside thinking) per the Anthropic API.
ANTHROPIC_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
ANTHROPIC_EFFORT_DEFAULT = "xhigh"

# One INFO log line per active hosting lane per process (mirrors UDR-0034).
_logged_lanes: set[str] = set()


def _log_lane(lane: str) -> None:
    if lane in _logged_lanes:
        return
    _logged_lanes.add(lane)
    logger.info("Anthropic provider hosting lane: %s", lane)


def anthropic_web_search_tool() -> Any:
    """Return the Anthropic hosted web search tool dict.

    Exposed at module scope so it can be referenced by symmetric tests and any
    future cross-provider helper, mirroring the
    ``app.providers.azure_openai.openai_web_search_tool`` factory. The MAF
    static method ``AnthropicClient.get_web_search_tool`` accepts optional
    ``type_name`` and ``name`` overrides; the defaults
    (``web_search_20250305`` / ``web_search``) are correct for Claude 4.x.
    """
    from agent_framework.anthropic import AnthropicClient

    return AnthropicClient.get_web_search_tool()


class AnthropicProvider:
    """Provider for Anthropic Claude models (Direct + Foundry hostings)."""

    name = NAME

    def models(self) -> list[str]:
        return settings.anthropic_model_list

    def build_chat_client(self, model: str) -> Any:
        if settings.anthropic_hosting == "foundry":
            from agent_framework.anthropic import AnthropicFoundryClient

            kwargs: dict[str, Any] = {"model": model}
            # Endpoint: base_url (full URL) and resource (subdomain) are mutually
            # exclusive in the SDK; base_url wins when both are set.
            if settings.anthropic_foundry_base_url:
                kwargs["base_url"] = settings.anthropic_foundry_base_url
            elif settings.anthropic_foundry_resource:
                kwargs["resource"] = settings.anthropic_foundry_resource
            # Auth: api-key XOR Entra ID (mutually exclusive in the SDK). The
            # API key wins when set; otherwise authenticate with Entra ID via the
            # mode-selected Azure credential (cli / managed-identity / default,
            # AZURE_CREDENTIAL_MODE) as a bearer-token provider on the
            # cognitiveservices scope (UDR-0045 D4 + UDR-0034). Passing a token
            # provider as ANTHROPIC_FOUNDRY_API_KEY would be sent as the api-key
            # header and rejected with HTTP 401.
            if settings.anthropic_foundry_api_key:
                kwargs["api_key"] = settings.anthropic_foundry_api_key
                _log_lane("foundry/api-key")
            else:
                from app.azure_credential import get_active_lane, get_token_provider

                kwargs["azure_ad_token_provider"] = get_token_provider()
                _log_lane(f"foundry/entra:{get_active_lane()}")
            return AnthropicFoundryClient(**kwargs)

        from agent_framework.anthropic import AnthropicClient

        kwargs = {"model": model}
        if settings.anthropic_api_key:
            kwargs["api_key"] = settings.anthropic_api_key
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
        _log_lane("direct")
        return AnthropicClient(**kwargs)

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        # Fixed, backend-owned allowed list + default (PRP-0071, UDR-0047 D2).
        # Anthropic default reasoning effort is always `xhigh`; the operator
        # picks per message in the UI. Not env-configurable.
        return {"allowed": list(ANTHROPIC_EFFORT_LEVELS), "default": ANTHROPIC_EFFORT_DEFAULT}

    def build_model_options(self, model: str, effort: str | None = None) -> dict[str, Any]:
        # Adaptive thinking is the only supported mode on Opus 4.7 / 4.8 (manual
        # thinking.type=enabled + budget_tokens returns HTTP 400). The effort
        # level goes in a separate output_config object; display=summarized keeps
        # the thinking blocks populated for the reasoning UI (CTR-0017, UDR-0047
        # D5/F3). max_tokens remains required as a hard output cap.
        catalog = self.reasoning_catalog(model)
        chosen = effort if effort in catalog["allowed"] else catalog["default"]
        return {
            "max_tokens": settings.anthropic_max_tokens,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": chosen},
        }

    def web_search_tool(self, model: str) -> Any:
        """Return Anthropic's hosted web search tool dict.

        The dict ``{"type": "web_search_20250305", "name": "web_search"}`` is
        passed alongside Python callables in ``Agent(tools=...)``; the MAF
        Anthropic connector serializes it into the Anthropic Messages API
        ``tools`` array and handles ``web_search_tool_result`` /
        ``web_search_result_location`` content blocks in the response stream
        (deserialization at ``agent_framework_anthropic._chat_client`` lines
        1096 / 1383). The corresponding OpenAI tool returned by
        ``app.providers.azure_openai.openai_web_search_tool`` has a different
        shape (it carries ``user_location`` from ``WEB_SEARCH_COUNTRY``); each
        provider returns the shape its own connector expects, and the registry
        appends whichever non-None tool the active model's provider supplies.
        This supersedes the original UDR-0045 D5 deferral for Anthropic.
        """
        return anthropic_web_search_tool()
