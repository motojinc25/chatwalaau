"""Base-model provider Protocol (CTR-0102, PRP-0069, UDR-0045).

A Provider owns everything that differs between base-model providers:
the model list it serves, how to construct the MAF ``ChatClient`` for a
model (including ``(provider x hosting)`` credential / endpoint
resolution), the model-aware reasoning effort catalog, the per-model
generation / reasoning ``default_options`` (effort-aware), and the
provider-supplied hosted web search tool (when any).

No new in-house seam is introduced: the returned chat client is a MAF
``ChatClient`` (BaseChatClient), reused exactly as ``OpenAIChatClient`` and
``DemoChatClient`` already are (UDR-0045 D1/D9).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Provider(Protocol):
    """Provider seam consumed by ``app.providers`` dispatch and CTR-0070."""

    name: str

    # Whether this provider supports OpenAI-style background responses
    # (Responses API ``background=true`` + continuation_token resume,
    # CTR-0045). Azure OpenAI supports it; Anthropic does not. Consumed by
    # the GET /api/model background_supported_map (CTR-0041) and the AG-UI
    # run-option guard (CTR-0045), and surfaced in the UI to disable the
    # Background toggle for non-supporting models (PRP-0073).
    supports_background: bool

    def models(self) -> list[str]:
        """Ordered model ids this provider serves (from its env namespace)."""
        ...

    def build_chat_client(self, model: str) -> Any:
        """Construct the MAF ChatClient for ``model`` (credential owned here)."""
        ...

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        """Reasoning effort catalog for ``model`` (PRP-0071, UDR-0047 D2).

        Returns ``{"allowed": [<effort>, ...], "default": <effort>}``. The
        allowed list and built-in default are provider-owned; the default may
        be overridden per model via the provider's env override key.
        """
        ...

    def build_model_options(self, model: str, effort: str | None = None) -> dict[str, Any]:
        """Per-model Agent ``default_options`` (provider-specific shape).

        ``effort`` selects the reasoning strength; ``None`` or a value not in the
        model's allowed list resolves to the catalog default (UDR-0047 D7).
        """
        ...

    def web_search_tool(self, model: str) -> Any | None:
        """Provider-supplied hosted web search tool, or None when unsupplied."""
        ...
