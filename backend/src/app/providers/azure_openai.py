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
from app.providers.structured import (
    STRUCTURED_OUTPUT_NAME,
    effective_schema,
    forced_tool_use_fragment,
    strip_web_search,
)

NAME = "azure-openai"

# Reasoning effort catalog (PRP-0071, UDR-0047 D2/D3). The OpenAI Responses API
# accepts none / minimal / low / medium / high / xhigh, but the reasoning-only
# policy hides none / minimal -- only low and above are offered (UDR-0047 D3).
OPENAI_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
OPENAI_EFFORT_DEFAULT = "medium"

# Text verbosity catalog (PRP-0081, UDR-0057 D4). gpt-5.x exposes a text-
# generation verbosity control (OpenAI Responses API `text: {verbosity}`)
# orthogonal to reasoning effort: it shapes how terse / expansive the visible
# answer is. The default mirrors the API's own default (medium) so the
# un-changed path is byte-for-byte (UDR-0057 D6). Unlike temperature / top_p,
# verbosity IS accepted by gpt-5.x reasoning models, so it is the one generation
# knob the v1 catalog advertises beyond effort.
OPENAI_VERBOSITY_LEVELS: tuple[str, ...] = ("low", "medium", "high")
OPENAI_VERBOSITY_DEFAULT = "medium"


# Structured output vs. hosted web search (PRP-0082, UDR-0058 D2). The OpenAI
# Responses API rejects the hosted web_search tool together with a JSON response
# format ("Web Search cannot be used with JSON mode."). We subclass the connector
# and, at the single request-assembly chokepoint, drop web_search whenever a JSON
# `text.format` is set. Always applied; inert (byte-for-byte) when no structured
# format is present, so non-structured turns are unchanged.


class _StructuredOutputMixin:
    async def _prepare_options(self, messages: Any, options: Any, **kwargs: Any) -> dict[str, Any]:
        run_options = await super()._prepare_options(messages, options, **kwargs)  # type: ignore[misc]
        text_cfg = run_options.get("text")
        if isinstance(text_cfg, dict) and text_cfg.get("format") is not None:
            strip_web_search(run_options)
        return run_options


_structured_client_cls: type | None = None


def _structured_openai_client_class() -> type:
    global _structured_client_cls
    if _structured_client_cls is None:
        _structured_client_cls = type("StructuredOpenAIChatClient", (_StructuredOutputMixin, OpenAIChatClient), {})
    return _structured_client_cls


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
    # Azure OpenAI Responses API supports background runs + resume (CTR-0045).
    supports_background = True

    def models(self) -> list[str]:
        return settings.model_list

    def build_chat_client(self, model: str) -> Any:
        # Prompt caching (PRP-0080, FEAT-0038 / UDR-0056 D4): Azure/OpenAI prompt
        # caching is AUTOMATIC for prefixes >= 1024 tokens, so this provider needs
        # no request rewrite and returns the plain client (pass-through). The seam
        # responsibility still lives here -- a future provider with EXPLICIT caching
        # injects it in its own build_chat_client (as app.providers.anthropic does);
        # an optional stable prompt_cache_key hint is intentionally deferred (the
        # automatic discount already applies). PROMPT_CACHE_ENABLED gates only the
        # explicit (anthropic) lane.
        # Wrapped in the structured-output subclass so the hosted web_search tool is
        # dropped when a JSON `text.format` is set (PRP-0082); inert otherwise.
        return _structured_openai_client_class()(
            model=model,
            azure_endpoint=settings.azure_openai_endpoint or None,
            **get_chat_client_credential_kwargs(),
        )

    def model_options_catalog(self, model: str) -> dict[str, Any]:
        # Generalized per-model option catalog (PRP-0081, UDR-0057 D2/D4). gpt-5.x
        # advertises reasoning effort + text verbosity. It does NOT advertise
        # temperature / top_p / top_k: they are not part of a reasoning model's
        # request (reasoning-only policy, UDR-0047 D3 / UDR-0057 D3). Fixed,
        # backend-owned allowed lists + defaults; the operator picks per message.
        return {
            "options": [
                {
                    "key": "effort",
                    "kind": "enum",
                    "allowed": list(OPENAI_EFFORT_LEVELS),
                    "default": OPENAI_EFFORT_DEFAULT,
                },
                {
                    "key": "verbosity",
                    "kind": "enum",
                    "allowed": list(OPENAI_VERBOSITY_LEVELS),
                    "default": OPENAI_VERBOSITY_DEFAULT,
                },
            ]
        }

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        # Derived effort-axis view of model_options_catalog (back-compat for the
        # GET /api/model reasoning_options map, CTR-0069 v4).
        return {"allowed": list(OPENAI_EFFORT_LEVELS), "default": OPENAI_EFFORT_DEFAULT}

    def build_model_options(self, model: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        # Reasoning-only policy: always send reasoning.effort (UDR-0047 D3).
        # Requested effort wins when allowed; otherwise the catalog default.
        selected = selected or {}
        effort = selected.get("effort")
        if effort not in OPENAI_EFFORT_LEVELS:
            effort = OPENAI_EFFORT_DEFAULT
        options: dict[str, Any] = {"reasoning": {"effort": effort, "summary": "detailed"}}

        # Text verbosity (UDR-0057 D4). OpenAI Responses API `text: {verbosity}`;
        # MAF OpenAIChatClient forwards default_options keys to the request the
        # same way it forwards `reasoning` (verified at implementation against the
        # deployed connector). Output-neutral default (UDR-0057 D6): send the key
        # ONLY when a valid, non-default verbosity is chosen, so the default path
        # is byte-for-byte identical to pre-PRP-0081.
        verbosity = selected.get("verbosity")
        if verbosity in OPENAI_VERBOSITY_LEVELS and verbosity != OPENAI_VERBOSITY_DEFAULT:
            options["text"] = {"verbosity": verbosity}
        return options

    def web_search_tool(self, model: str) -> Any:
        return openai_web_search_tool()

    def structured_output_support(self, model: str) -> dict[str, Any]:
        # Structured output (PRP-0082, UDR-0058 D1/D6). gpt-5.x supports the OpenAI
        # Responses API `text.format` json_schema control with `strict: true`, which
        # the API GUARANTEES conforms to the schema (modulo truncation / refusal).
        # forced_tool_use is the declared universal fallback for a hypothetical
        # non-native model (UDR-0058 D2).
        return {"supported": True, "native": True, "fallback": "forced_tool_use"}

    def build_structured_output(self, model: str, schema: dict[str, Any] | None, mode: str) -> dict[str, Any]:
        # Native: OpenAI Responses API `text.format` (PRP-0082, UDR-0058 D2). For an
        # explicit schema use json_schema strict (the conformance guarantee); for the
        # generic / no-schema mode use the json_object format. Merged into the run
        # options next to `text.verbosity` (the AG-UI endpoint deep-merges `text`).
        eff = effective_schema(schema, mode)
        if eff is None:
            return {}
        if not self.structured_output_support(model)["native"]:
            return forced_tool_use_fragment(eff)
        if mode == "json_schema" and isinstance(schema, dict) and schema:
            return {
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": STRUCTURED_OUTPUT_NAME,
                        "schema": schema,
                        "strict": True,
                    }
                }
            }
        return {"text": {"format": {"type": "json_object"}}}
