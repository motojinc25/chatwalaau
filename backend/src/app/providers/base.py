"""Base-model provider Protocol (CTR-0102, PRP-0069, UDR-0045).

A Provider owns everything that differs between base-model providers:
the model list it serves, how to construct the MAF ``ChatClient`` for a
model (including ``(provider x hosting)`` credential / endpoint
resolution), the per-model GENERATION OPTION CATALOG (the set of knobs the
model accepts: reasoning effort plus, where supported, verbosity; PRP-0081 /
UDR-0057), the per-model generation ``default_options`` resolved from a
selection, and the provider-supplied hosted web search tool (when any).

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

    def model_options_catalog(self, model: str) -> dict[str, Any]:
        """Generation option catalog for ``model`` (PRP-0081, UDR-0057 D2).

        Returns ``{"options": [descriptor, ...]}`` where each descriptor is one
        of:

          - ``{"key", "kind": "enum",   "allowed": [...], "default": ...}``
          - ``{"key", "kind": "number", "min", "max", "step", "default"}``

        Each provider advertises ONLY the knobs the model actually accepts, so
        the UI renders nothing unsupported (UDR-0057 D2). Classic sampling params
        (temperature / top_p / top_k) MUST NOT be advertised for adaptive-thinking
        / reasoning-only models; the ``number`` kind is reserved for a future
        non-reasoning model (UDR-0057 D3).
        """
        ...

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        """Reasoning effort catalog for ``model`` (PRP-0071, UDR-0047 D2).

        Returns ``{"allowed": [<effort>, ...], "default": <effort>}``, the
        ``effort`` axis of :meth:`model_options_catalog`. Retained as a derived
        back-compat view for the GET /api/model ``reasoning_options`` map
        (CTR-0069); the generalized catalog is the source of truth.
        """
        ...

    def build_model_options(self, model: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        """Per-model Agent ``default_options`` (provider-specific shape).

        ``selected`` maps advertised option keys to chosen values
        (e.g. ``{"effort": "high", "verbosity": "low"}``). ``None``, missing, or
        invalid / out-of-range values resolve to the catalog default (UDR-0057
        D7). A resolved value equal to its default is OMITTED from the request so
        the un-changed path stays byte-for-byte (output-neutral default, UDR-0057
        D6).
        """
        ...

    def web_search_tool(self, model: str) -> Any | None:
        """Provider-supplied hosted web search tool, or None when unsupplied."""
        ...

    def structured_output_support(self, model: str) -> dict[str, Any]:
        """Per-model structured-output capability (CTR-0102 v5, UDR-0058 D1/D6).

        Returns ``{"supported": bool, "native": bool, "fallback": str}`` where
        ``fallback`` is ``"forced_tool_use"`` or ``"none"``. ``native`` is True when
        the model accepts the provider's first-class structured-output request
        (OpenAI ``text.format`` json_schema / Anthropic ``output_config.format``);
        when False the model still produces JSON via the forced-tool-use fallback
        (UDR-0058 D2). Published per model by GET /api/model (CTR-0069 v5) so the UI
        renders strictly what is advertised.
        """
        ...

    def build_structured_output(self, model: str, schema: dict[str, Any] | None, mode: str) -> dict[str, Any]:
        """Per-request structured-output run-options fragment (CTR-0102 v5, UDR-0058 D2/D3).

        ``mode`` is ``"json_schema"`` (use the explicit ``schema`` verbatim) or
        ``"json_object"`` (generic permissive / free-JSON when no schema is supplied,
        UDR-0058 D3). The returned dict is merged into the ``agent.run`` options:

          - native models -> the provider's first-class shape (OpenAI
            ``{"text": {"format": {...}}}`` / Anthropic
            ``{"output_config": {"format": {...}}}``).
          - non-native models -> a forced-tool-use fragment
            (``{"tools": [...], "tool_choice": {...}}``) constraining the answer to
            the schema (UDR-0058 D2).

        Returns ``{}`` when structured output is not requested. MUST NOT raise: a bad
        or oversized schema resolves to the generic object mode (UDR-0058 D2/D4).
        """
        ...
