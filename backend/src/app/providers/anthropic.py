"""Anthropic (Claude) base-model provider (CTR-0102, PRP-0069, UDR-0045).

Wraps the first-party MAF connectors behind the Provider seam. Per-offering
hosting (PRP-0113 / UDR-0094): a Claude offering declares ``hosting`` (defaults
to ``direct``), so direct Claude and Foundry-hosted Claude coexist in one
deployment.

- Direct  (offering ``hosting: direct``)  -> ``AnthropicClient`` against the
  Anthropic public API, authenticated by the offering's ``api_key_env`` key
  (+ optional per-offering ``base_url``).
- Foundry (offering ``hosting: foundry``) -> ``AnthropicFoundryClient`` against
  Anthropic on Azure AI Foundry. Endpoint: the offering's ``base_url`` (the full
  Anthropic-compatible URL; the resource-subdomain shorthand is dropped,
  UDR-0094 D7). Auth (mutually exclusive in the SDK): the ``api-key`` header
  from the offering's ``api_key_env`` when set, otherwise an Entra ID
  ``Authorization: Bearer`` token from the mode-selected SHARED Azure credential
  (cli / managed-identity / default, AZURE_CREDENTIAL_MODE -- retained per
  UDR-0094 D6) via ``app.azure_credential.get_token_provider()``.

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

from app import models_catalog
from app.core.config import settings
from app.providers.structured import effective_schema, forced_tool_use_fragment, strip_web_search

logger = logging.getLogger(__name__)

NAME = "anthropic"

# Reasoning effort catalog (PRP-0071, UDR-0047 D2). Anthropic adaptive-thinking
# effort levels for Claude Opus 4.7 / 4.8: low / medium / high / xhigh / max
# (xhigh sits between high and max; available on Opus 4.7+). The effort is sent
# in a separate output_config object (NOT inside thinking) per the Anthropic API.
ANTHROPIC_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
ANTHROPIC_EFFORT_DEFAULT = "xhigh"

# Total output budget (thinking + visible text) per effort level. Anthropic
# counts BOTH adaptive-thinking tokens AND the answer against max_tokens, so a
# small cap lets high-effort thinking starve the answer: observed ~70s of xhigh
# thinking on Opus 4.8, then a single-character reply that stopped at
# stop_reason=max_tokens. Each effort therefore needs a far larger cap than the
# 8192 baseline. These tiers are within Claude Opus 4.7/4.8 output limits;
# ANTHROPIC_MAX_TOKENS acts as a floor an operator can raise further (e.g. for a
# model with a larger output window), never a value that can lower the tier.
ANTHROPIC_EFFORT_MAX_TOKENS: dict[str, int] = {
    "low": 8192,
    "medium": 16000,
    "high": 32000,
    "xhigh": 48000,
    "max": 64000,
}

# One INFO log line per active hosting lane per process (mirrors UDR-0034).
_logged_lanes: set[str] = set()


def _log_lane(lane: str) -> None:
    if lane in _logged_lanes:
        return
    _logged_lanes.add(lane)
    logger.info("Anthropic provider hosting lane: %s", lane)


# ---- Prompt caching (PRP-0080, FEAT-0038 / UDR-0056 D3) -------------------
# The MAF Anthropic connector assembles the Messages API request in
# RawAnthropicClient._prepare_options (it sets ``system`` as a plain string and
# ``tools`` as a list). We subclass the connector and override that single
# chokepoint to mark the STABLE prefix (the final system block + the final tool
# definition) with ``cache_control``, which caches the whole system + tools
# prefix. The per-message tail after the breakpoint stays uncached, so a cache
# hit is always prefix-exact; model outputs are unchanged (only billing /
# latency change). Up to 4 breakpoints are allowed; we use two.

_EXTENDED_CACHE_BETA = "extended-cache-ttl-2025-04-11"


def _cache_control_marker(ttl: str) -> dict[str, Any]:
    """Return the ``cache_control`` object for the configured TTL.

    ``5m`` (GA) uses the bare ephemeral marker; ``1h`` uses the extended cache
    (also requires the ``extended-cache-ttl-2025-04-11`` beta, added below).
    """
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


# Thinking blocks cannot carry cache_control (Anthropic rejects it / the MAF
# connector strips it). Never place a breakpoint on these block types.
_NON_CACHEABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def _mark_message_breakpoint(msg: dict[str, Any], marker: dict[str, Any]) -> bool:
    """Place ``cache_control`` on the last cacheable block of a message.

    Returns True if a breakpoint was placed. A bare string content is promoted to
    a text block; a block list is marked on its last non-thinking block.
    """
    content = msg.get("content")
    if isinstance(content, str) and content:
        msg["content"] = [{"type": "text", "text": content, "cache_control": marker}]
        return True
    if isinstance(content, list) and content:
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") not in _NON_CACHEABLE_BLOCK_TYPES:
                block["cache_control"] = marker
                return True
    return False


def _inject_prompt_cache(run_options: dict[str, Any], ttl: str) -> dict[str, Any]:
    """Annotate the stable prefix of an Anthropic request for caching (`system_and_3`).

    Uses the proven layout from the ChatWalaau reference agent and the Anthropic
    prompt-caching guidance: all 4 allowed `cache_control` breakpoints go on the
    system block + the last 3 (non-system) messages.

      1. The final system block. Anthropic renders ``tools -> system -> messages``,
         so a breakpoint on the last system block caches the WHOLE tools + system
         prefix -- a separate tools breakpoint would only cache a redundant subset,
         so we spend the remaining budget on the conversation instead.
      2. The last up-to-3 message content blocks -- a ROLLING conversation cache.
         An agentic tool loop re-sends the conversation tail (tool results, file
         contents) on every model call; marking the recent messages makes that tail
         read back at ~0.1x instead of full price. Multiple overlapping read points
         (a) let hits accrue incrementally as the conversation grows and (b) stay
         within Anthropic's 20-content-block cache lookback window in long tool
         loops, where a single trailing breakpoint can silently miss.

    Net-positive whenever consecutive requests share a message prefix (short tool
    loops + cross-turn -- the observed common case). Within a LONG tool loop the
    sliding-window compaction (CTR-0098) shifts the prefix; raise
    COMPACTION_KEEP_LAST_GROUPS to widen the stable window when caching is the
    priority (UDR-0056 D9). NOTE: Opus 4.x only caches a prefix of >= 4096 tokens;
    below that the markers are silently inert (no error, no write).

    Mutates and returns ``run_options`` (the kwargs for
    ``anthropic_client.beta.messages.create``). Defensive: only acts on the
    expected shapes; an unexpected shape is left untouched so caching simply does
    not engage rather than breaking the call.
    """
    marker = _cache_control_marker(ttl)
    remaining = 4

    system = run_options.get("system")
    if isinstance(system, str) and system:
        run_options["system"] = [{"type": "text", "text": system, "cache_control": marker}]
        remaining -= 1
    elif isinstance(system, list) and system and isinstance(system[-1], dict):
        system[-1]["cache_control"] = marker
        remaining -= 1

    # Roll the remaining breakpoints across the most recent messages (last 3 when a
    # system breakpoint was placed, else last 4).
    messages = run_options.get("messages")
    if isinstance(messages, list) and messages and remaining > 0:
        for msg in messages[-remaining:]:
            if isinstance(msg, dict):
                _mark_message_breakpoint(msg, marker)

    if ttl == "1h":
        betas = run_options.get("betas")
        if isinstance(betas, set):
            betas.add(_EXTENDED_CACHE_BETA)
        elif isinstance(betas, (list, tuple)):
            run_options["betas"] = set(betas) | {_EXTENDED_CACHE_BETA}
        else:
            run_options["betas"] = {_EXTENDED_CACHE_BETA}

    return run_options


class _PromptCacheMixin:
    """Mixin overriding ``_prepare_options`` to inject prompt-cache markers.

    Layered BEFORE the concrete MAF client in the MRO so ``super()`` builds the
    normal request and we annotate its stable prefix afterwards. The TTL is read
    per call from settings so a test toggling it sees the effect.
    """

    def _prepare_options(self, messages: Any, options: Any, **kwargs: Any) -> dict[str, Any]:
        run_options = super()._prepare_options(messages, options, **kwargs)  # type: ignore[misc]
        # Structured output vs hosted web search (PRP-0082, UDR-0058 D2): web search
        # is incompatible with a JSON output_config.format; drop it for that turn so
        # the request never fails. Inert when no structured format is set.
        oc = run_options.get("output_config")
        if isinstance(oc, dict) and oc.get("format") is not None:
            strip_web_search(run_options)
        # Prompt caching (PRP-0080, UDR-0056). Gated here so the wrapper is applied
        # unconditionally (for the structured strip above) while caching stays an
        # output-transparent opt-out -- byte-for-byte when both are off.
        if settings.prompt_cache_enabled:
            run_options = _inject_prompt_cache(run_options, settings.anthropic_prompt_cache_ttl)
        return run_options


# Caching subclasses are built lazily and memoized per base class, so the
# agent_framework.anthropic connector import stays confined to build_chat_client
# (an Azure-only deployment never pays it).
_caching_subclasses: dict[type, type] = {}


def _caching_client_class(base_cls: type) -> type:
    cls = _caching_subclasses.get(base_cls)
    if cls is None:
        cls = type(f"PromptCaching{base_cls.__name__}", (_PromptCacheMixin, base_cls), {})
        _caching_subclasses[base_cls] = cls
    return cls


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
    # Anthropic (incl. Opus 4.7/4.8) has no background-response equivalent;
    # background runs are an OpenAI Responses API feature (CTR-0045).
    supports_background = False

    def build_chat_client(self, model: str) -> Any:
        # Catalog routing (PRP-0113, UDR-0094): `model` is the offering id, so the
        # connector `model=` uses the offering's model_ref, the HOSTING is
        # per-offering (direct Claude and Foundry-hosted Claude can coexist in one
        # deployment -- the removed global ANTHROPIC_HOSTING could not express
        # that), and base_url / api_key_env come from the offering (no settings
        # fallback; UDR-0094 D2). Hosting defaults to "direct" when the offering
        # omits it. The Foundry Entra ID lane still reuses the SHARED Azure
        # credential (UDR-0094 D6, retained).
        offering = models_catalog.offering_for(model)
        model_ref = offering.model_ref if offering is not None else model
        hosting = (offering.hosting if (offering is not None and offering.hosting) else "direct")
        offering_api_key = offering.api_key() if (offering is not None and offering.api_key_env) else None
        offering_base_url = offering.base_url if offering is not None else None

        if hosting == "foundry":
            from agent_framework.anthropic import AnthropicFoundryClient

            kwargs: dict[str, Any] = {"model": model_ref}
            # Endpoint: the offering's base_url is the full Anthropic-on-Foundry URL
            # (the resource subdomain shorthand is dropped, UDR-0094 D7).
            if offering_base_url:
                kwargs["base_url"] = offering_base_url
            # Auth: api-key XOR Entra ID (mutually exclusive in the SDK). The
            # offering-referenced API key wins when set; otherwise authenticate with
            # Entra ID via the mode-selected SHARED Azure credential (cli /
            # managed-identity / default, AZURE_CREDENTIAL_MODE) as a bearer-token
            # provider on the cognitiveservices scope (UDR-0045 D4 + UDR-0034 +
            # UDR-0094 D6). Passing a token provider as an api-key would be rejected
            # with HTTP 401.
            api_key = offering_api_key
            if api_key:
                kwargs["api_key"] = api_key
                _log_lane("foundry/api-key")
            else:
                from app.azure_credential import get_active_lane, get_token_provider

                kwargs["azure_ad_token_provider"] = get_token_provider()
                _log_lane(f"foundry/entra:{get_active_lane()}")
            # Prompt caching (PRP-0080, UDR-0056 D3): use the cache-injecting
            # subclass when enabled; otherwise the plain connector (no
            # cache_control -> pre-PRP-0080 request shape).
            # Always wrap: the mixin gates prompt caching on PROMPT_CACHE_ENABLED
            # internally and additionally strips web search under structured output
            # (PRP-0082). Byte-for-byte when neither fires.
            return _caching_client_class(AnthropicFoundryClient)(**kwargs)

        from agent_framework.anthropic import AnthropicClient

        kwargs = {"model": model_ref}
        api_key = offering_api_key
        if api_key:
            kwargs["api_key"] = api_key
        base_url = offering_base_url
        if base_url:
            kwargs["base_url"] = base_url
        _log_lane("direct")
        return _caching_client_class(AnthropicClient)(**kwargs)

    def model_options_catalog(self, model: str) -> dict[str, Any]:
        # Generalized per-model option catalog (PRP-0081, UDR-0057 D2/D3). Opus
        # 4.7/4.8 advertise reasoning effort ONLY. temperature / top_p / top_k are
        # removed on adaptive-thinking models and the Messages API returns HTTP 400
        # if sent, so they are NEVER advertised (UDR-0057 D3); verbosity is an
        # OpenAI-only control. Fixed, backend-owned; the operator picks per message.
        return {
            "options": [
                {
                    "key": "effort",
                    "kind": "enum",
                    "allowed": list(ANTHROPIC_EFFORT_LEVELS),
                    "default": ANTHROPIC_EFFORT_DEFAULT,
                },
            ]
        }

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        # Derived effort-axis view of model_options_catalog (back-compat for the
        # GET /api/model reasoning_options map, CTR-0069 v4).
        return {"allowed": list(ANTHROPIC_EFFORT_LEVELS), "default": ANTHROPIC_EFFORT_DEFAULT}

    def build_model_options(self, model: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        # Adaptive thinking is the only supported mode on Opus 4.7 / 4.8 (manual
        # thinking.type=enabled + budget_tokens returns HTTP 400). The effort
        # level goes in a separate output_config object; display=summarized keeps
        # the thinking blocks populated for the reasoning UI (CTR-0017, UDR-0047
        # D5/F3).
        #
        # max_tokens caps thinking + text TOGETHER, so it must scale with the
        # effort or high-effort thinking starves the visible answer to ~1 token
        # (the xhigh "one character then stops" defect). Use the per-effort tier,
        # floored by the operator's ANTHROPIC_MAX_TOKENS so a larger configured
        # value still wins.
        selected = selected or {}
        effort = selected.get("effort")
        chosen = effort if effort in ANTHROPIC_EFFORT_LEVELS else ANTHROPIC_EFFORT_DEFAULT
        effort_budget = ANTHROPIC_EFFORT_MAX_TOKENS.get(chosen, ANTHROPIC_EFFORT_MAX_TOKENS["high"])
        max_tokens = max(settings.anthropic_max_tokens, effort_budget)
        return {
            "max_tokens": max_tokens,
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

    def structured_output_support(self, model: str) -> dict[str, Any]:
        # Structured output (PRP-0082, UDR-0058 D1/D6). Anthropic Opus 4.x supports
        # native structured output via `output_config.format` (the modern path that
        # supersedes the deprecated `output_format` parameter). forced_tool_use is
        # the declared universal fallback for a hypothetical non-native model
        # (UDR-0058 D2). The exact connector passthrough is verified at integration
        # (see PRP-0082 Risk); if unavailable on the pinned connector, set
        # native=False here and the forced-tool-use branch engages with no other
        # change.
        return {"supported": True, "native": True, "fallback": "forced_tool_use"}

    def build_structured_output(self, model: str, schema: dict[str, Any] | None, mode: str) -> dict[str, Any]:
        # Native: Anthropic Messages API `output_config.format` (PRP-0082, UDR-0058
        # D2). Returned as a separate `output_config` fragment; the AG-UI endpoint
        # deep-merges it with the `output_config{effort}` from build_model_options so
        # adaptive-thinking effort and the output format coexist.
        eff = effective_schema(schema, mode)
        if eff is None:
            return {}
        if not self.structured_output_support(model)["native"]:
            return forced_tool_use_fragment(eff)
        return {"output_config": {"format": {"type": "json_schema", "schema": eff}}}
