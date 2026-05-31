"""Base-model provider Protocol (CTR-0102, PRP-0069, UDR-0045).

A Provider owns everything that differs between base-model providers:
the model list it serves, how to construct the MAF ``ChatClient`` for a
model (including ``(provider x hosting)`` credential / endpoint
resolution), the per-model generation / reasoning ``default_options``, and
the provider-supplied hosted web search tool (when any).

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

    def models(self) -> list[str]:
        """Ordered model ids this provider serves (from its env namespace)."""
        ...

    def build_chat_client(self, model: str) -> Any:
        """Construct the MAF ChatClient for ``model`` (credential owned here)."""
        ...

    def build_model_options(self, model: str) -> dict[str, Any]:
        """Per-model Agent ``default_options`` (provider-specific shape)."""
        ...

    def web_search_tool(self, model: str) -> Any | None:
        """Provider-supplied hosted web search tool, or None when unsupplied."""
        ...
