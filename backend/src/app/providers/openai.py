"""OpenAI (direct) base-model provider (CTR-0102, PRP-0095, UDR-0073).

The third base-model provider, behind the existing ``app.providers`` seam
(UDR-0045). OpenAI direct uses the SAME MAF connector as Azure OpenAI -- a
single ``agent_framework_openai.OpenAIChatClient`` class that resolves to the
direct OpenAI public API when constructed with ``api_key`` / ``base_url`` and
no ``azure_endpoint`` / ``credential`` (UDR-0073 D1).

For v1 the supported scope is REASONING models only (e.g. gpt-5.x / o-series),
whose generation-option catalog, reasoning-effort handling, hosted web search,
structured output, and background support are IDENTICAL to the ``azure-openai``
provider (UDR-0073 D5). The provider therefore subclasses ``AzureOpenAIProvider``
and overrides only what differs -- ``name``, ``models()``, and
``build_chat_client()`` (the API-key credential lane) -- inheriting the catalog,
options, web search, structured output, and ``supports_background`` unchanged
(UDR-0073 D7). This guarantees zero behavioral drift from ``azure-openai`` for
the reasoning lane while keeping the Azure provider byte-for-byte.

Non-reasoning models (gpt-4o / gpt-4.1 with temperature / top_p, the
``number``-kind descriptor reserved by UDR-0057 D3) are OUT OF SCOPE for v1 and
deferred to a follow-up PRP (UDR-0073 D5).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.providers.azure_openai import AzureOpenAIProvider, _structured_openai_client_class

logger = logging.getLogger(__name__)

NAME = "openai"

# One INFO log line per active lane per process (mirrors UDR-0034 / UDR-0045).
_logged_lanes: set[str] = set()


def _log_lane(lane: str) -> None:
    if lane in _logged_lanes:
        return
    _logged_lanes.add(lane)
    logger.info("OpenAI provider hosting lane: %s", lane)


class OpenAIProvider(AzureOpenAIProvider):
    """Provider for the direct OpenAI public API (API-key auth, reasoning models).

    Reuses ``AzureOpenAIProvider`` for everything except the model namespace and
    the credential / endpoint lane (UDR-0073 D7).
    """

    name = NAME
    # OpenAI Responses API supports background runs + continuation_token resume,
    # the same mechanism the azure-openai provider advertises (CTR-0045). Inherited
    # from AzureOpenAIProvider (True); restated here for clarity (UDR-0073 D6).
    supports_background = True

    def models(self) -> list[str]:
        return settings.openai_model_list

    def build_chat_client(self, model: str) -> Any:
        # Direct OpenAI lane: api_key (+ optional base_url for OpenAI-compatible
        # gateways), NO azure_endpoint -> the connector targets api.openai.com
        # (UDR-0073 D1/D4). Wrapped in the same structured-output subclass as the
        # azure lane so the hosted web_search tool is dropped when a JSON
        # text.format is set (PRP-0082); inert otherwise.
        _log_lane("direct")
        return _structured_openai_client_class()(
            model=model,
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url or None,
        )
