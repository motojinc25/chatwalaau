"""Azure OpenAI base-model provider (CTR-0102, PRP-0069, UDR-0045).

Wraps the existing ``OpenAIChatClient`` + ``app.azure_credential`` lane
(UDR-0034, unchanged) behind the Provider seam. This provider reproduces the
pre-PRP-0069 behavior exactly: the same client kwargs, the same hosted web
search tool, and the same ``reasoning.effort`` option shape.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_framework_openai import OpenAIChatClient

from app import models_catalog
from app.agent.approval_debug import describe_wire_input_full, log_wire_request, wire_pairing_report
from app.azure_credential import get_chat_client_credential_kwargs
from app.core.config import settings
from app.providers.base import hosted_tool_withheld
from app.providers.structured import (
    GENERIC_OBJECT_SCHEMA,
    STRUCTURED_OUTPUT_NAME,
    dedupe_wire_input,
    drop_orphan_outputs,
    effective_schema,
    orphan_outputs,
    pairing_undecidable,
    strip_skill_tools,
    strip_web_search,
    summarize_removed,
    unanswered_calls,
)

logger = logging.getLogger(__name__)

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
        # Background and Agent Skills are mutually exclusive (PRP-0134, UDR-0116).
        # A background turn that called a skill failed with "No tool call found for
        # function call id call_..." -- the framework releases a background run's
        # continuation token only on its non-streaming path, so after the tool ran the
        # next step re-read the finished response instead of POSTing the result. The two
        # features are separated instead of the framework being worked around: Background
        # wins for the turn and the skill tools are dropped here, at the same request
        # chokepoint the web-search strip uses (UDR-0058 D2 -- drop, never error). The UI
        # says so before the turn, so this is not a silent removal.
        if run_options.get("background"):
            strip_skill_tools(run_options)
        # Approval-resume tracing (PRP-0141 follow-up): the request as it will go on
        # the wire, ids only. This is the ground truth the three prior fixes lacked.
        #
        # ORDER IS NORMATIVE (PRP-0147, UDR-0126 D5): the trace -- and with it the
        # pairing verdict -- is computed on the input as MAF assembled it, BEFORE the
        # uniqueness repair below. The duplication's PRODUCER is upstream and is not
        # fixed here; a repair that erased its own evidence would make that producer
        # invisible and leave the next investigation with nothing. The removal count
        # logged afterwards is the signal that the producer is worsening.
        log_wire_request(messages=messages, run_options=run_options)
        before = run_options.get("input")
        before_items = list(before) if isinstance(before, list) else []
        removed = dedupe_wire_input(run_options)
        if removed:
            after = run_options.get("input")
            logger.info(
                "[wire dedup] removed %d duplicate item(s): %s",
                removed,
                summarize_removed(before_items, after if isinstance(after, list) else []),
            )
        # Pairing is SYMMETRIC (PRP-0149 C2, UDR-0126 D8). An output whose call is not
        # in the same request is a guaranteed 400 -- and, unlike an unanswered CALL, it
        # carries no FEAT-0028 risk, because no approval item is ever expressed as a
        # bare output. So this direction is REMOVED, not merely reported.
        orphaned = drop_orphan_outputs(run_options)
        if orphaned:
            logger.info("[wire pairing] removed %d orphan output item(s) with no matching call", orphaned)
        _report_pairing(run_options)
        return run_options

    def _get_conversation_id(self, response: Any, store: Any) -> str | None:
        """Never treat a response as a resumable server-side conversation unless a
        background run explicitly asked for one (PRP-0142 follow-up, UDR-0123).

        Root cause, reproduced against the installed framework: Azure's
        ``api-version=preview`` Responses endpoint returns a response whose
        conversation id is populated EVEN WHEN we send ``store=False``. MAF's base
        ``_get_conversation_id`` only returns ``None`` when it *sees* ``store is
        False``; when the flag does not reach this call it returns ``response.id``
        instead. MAF then believes the conversation is server-managed and, in the
        inner tool loop, trims the transcript to the last message
        (``_prepare_messages_for_next_iteration``: ``prepared_messages[:] =
        response.messages[-1:]``) -- so the follow-up request carries a bare
        ``function_call_output`` with no preceding ``function_call`` and Azure
        rejects it:

            400 No tool call found for function call output with call_id call_...

        This agent runs CLIENT-MANAGED (default_options ``store=False``, UDR-0123);
        the ONLY server-managed path is a background run, which sets ``store=True``
        explicitly (AG-UI endpoint). Anchoring on ``store is True`` -- rather than
        on ``store is False`` -- makes the client-managed decision robust to the
        flag not propagating: a lost/absent ``store`` degrades to client-managed
        (no chaining, full transcript replayed) instead of to a broken chain.
        """
        if store is not True:
            return None
        return super()._get_conversation_id(response, store)  # type: ignore[misc]


def _report_pairing(run_options: dict[str, Any]) -> None:
    """Post-repair self-verification, BOTH directions (PRP-0149 C3, UDR-0126 D6/D8).

    The seam has printed the PRE-repair verdict (UDR-0126 D5) and applied its
    repairs. It now checks its own work, so the next provider rejection on this path
    arrives PRE-EXPLAINED, one line above the traceback, instead of being deduced
    afterwards from a log that already contained the answer (RES-0003 Finding B).

    Removing unanswered CALLS runs in REPORT-ONLY mode. PRP-0148 Section 6.4 makes an
    approval-gated wire trace a release GATE for that removal, because a wrong orphan
    rule breaks FEAT-0028 for every gated tool. Reporting collects that evidence from
    real traffic at zero risk; enabling the removal is then one change at this call
    site, taken on measured data rather than on reasoning about a seam that has already
    produced two wrong fixes.

    The OUTPUT direction is different and is judged the same way here (UDR-0126 D8).
    Before PRP-0149 this function judged on ``unanswered_calls()`` alone, so a request
    carrying two outputs with no call -- ids the verdict string had already named --
    was logged as ``post-repair: ...`` at INFO and posted, and the provider rejected
    it one line later. A check that verifies one direction has not verified pairing.
    """
    try:
        items = run_options.get("input")
        if not isinstance(items, list):
            return
        verdict = wire_pairing_report(items)
        if pairing_undecidable(items):
            # UDR-0126 D6: refuse to judge rather than guess at a key.
            logger.warning(
                "[wire] post-repair: NOT CHECKED -- the request contains an item with no "
                "matchable call id (local_shell_call_output); pairing is undecidable"
            )
            return
        bare_calls = unanswered_calls(items)
        stray_outputs = orphan_outputs(items)
        if not bare_calls and not stray_outputs:
            logger.info("[wire] post-repair: %s", verdict)
            return
        defects: list[str] = []
        if bare_calls:
            defects.append(
                f"{len(bare_calls)} unanswered call(s) "
                f"[REPORT ONLY, PRP-0148 6.4 gate]: " + "; ".join(f"{cid}:{name}" for cid, name in bare_calls)
            )
        if stray_outputs:
            # C2 removes these, so reaching here means a shape the removal did not
            # recognise. That must be loud, not absorbed.
            defects.append(
                f"{len(stray_outputs)} orphan output(s) SURVIVED the C2 removal: "
                + "; ".join(f"{cid}:{itype}" for cid, itype in stray_outputs)
            )
        logger.error(
            "[wire] POST-REPAIR VERDICT NOT OK -- this request is expected to be rejected: %s\n"
            "  %s\n  full structural dump (ids and shapes only, untruncated):\n  %s",
            verdict,
            "\n  ".join(defects),
            " | ".join(describe_wire_input_full(items)),
        )
    except Exception:  # self-verification must never break the request
        logger.exception("[wire] post-repair verification failed")


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
    # Responses API stores server-side by default and chains via previous_response_id
    # (PRP-0142). Inherited by OpenAIProvider and FoundryProvider.
    stores_responses_server_side = True

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
        #
        # Catalog routing (PRP-0113, UDR-0094): `model` is the offering id, so the
        # connector `model=` uses the offering's model_ref (real deployment name).
        # The endpoint may be per-offering; when omitted it falls back to the SHARED
        # Azure substrate `AZURE_OPENAI_ENDPOINT` (UDR-0094 D6, retained), and an
        # offering-referenced API key wins over the shared credential lane. A model
        # not in the catalog (defensive) resolves to model_ref=model + shared lane.
        offering = models_catalog.offering_for(model)
        model_ref = offering.model_ref if offering is not None else model
        endpoint = (
            offering.endpoint if offering is not None and offering.endpoint else settings.azure_openai_endpoint
        ) or None
        if offering is not None and offering.api_key_env:
            cred_kwargs: dict[str, Any] = {"api_key": offering.api_key() or ""}
        else:
            cred_kwargs = get_chat_client_credential_kwargs()
        return _structured_openai_client_class()(
            model=model_ref,
            azure_endpoint=endpoint,
            **cred_kwargs,
        )

    def model_options_catalog(self, model: str) -> dict[str, Any]:
        # Generalized per-model option catalog (PRP-0081, UDR-0057 D2/D4). gpt-5.x
        # advertises reasoning effort + text verbosity. It does NOT advertise
        # temperature / top_p / top_k: they are not part of a reasoning model's
        # request (reasoning-only policy, UDR-0047 D3 / UDR-0057 D3). Fixed,
        # backend-owned allowed lists + defaults; the operator picks per message.
        # A catalog `family: bare` override advertises no options (PRP-0109,
        # UDR-0087 D6) so a non-reasoning gateway model renders no control.
        if models_catalog.offering_family(model) == "bare":
            return {"options": []}
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
        # GET /api/model reasoning_options map, CTR-0069 v4). A catalog
        # `family: bare` override advertises no effort axis (PRP-0109, UDR-0087 D6).
        if models_catalog.offering_family(model) == "bare":
            return {"allowed": [], "default": None}
        return {"allowed": list(OPENAI_EFFORT_LEVELS), "default": OPENAI_EFFORT_DEFAULT}

    def build_model_options(self, model: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        # A catalog `family: bare` override builds a BARE request -- no reasoning,
        # no verbosity -- for a non-reasoning gateway model (PRP-0109, UDR-0087 D6).
        if models_catalog.offering_family(model) == "bare":
            return {}
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

    def web_search_tool(self, model: str) -> Any | None:
        # PRP-0129 / UDR-0112 D1: an offering may declare that its deployment cannot
        # serve the hosted tool. Undeclared is unchanged -- the tool is supplied.
        if hosted_tool_withheld(model, "web_search"):
            return None
        return openai_web_search_tool()

    def structured_output_support(self, model: str) -> dict[str, Any]:
        # Structured output (PRP-0082, UDR-0058 D1/D6). gpt-5.x supports the OpenAI
        # Responses API `text.format` json_schema control with `strict: true`, which
        # the API GUARANTEES conforms to the schema (modulo truncation / refusal).
        # There is NO fallback for a non-native model (PRP-0131, UDR-0058 D10):
        # `supported` follows `native`, so the surface omits the feature instead.
        #
        # PRP-0130 / UDR-0112 D10: an offering may declare that its DEPLOYMENT has no
        # native structured output. That DEGRADES to the fallback rather than
        # disabling the feature. Undeclared is unchanged.
        #
        # PRP-0131 / UDR-0058 D9: the default output schema is the OPEN object. It is
        # expressible here because the Responses API takes `strict: false`, which
        # lifts the closed-schema requirement -- the escape Anthropic does not have.
        native = not hosted_tool_withheld(model, "native_structured_output")
        return {
            "supported": native,
            "native": native,
            "fallback": "none",
            "default_schema": self.default_output_schema(model),
        }

    def default_output_schema(self, model: str) -> dict[str, Any]:
        return GENERIC_OBJECT_SCHEMA

    def build_structured_output(self, model: str, schema: dict[str, Any] | None, mode: str) -> dict[str, Any]:
        # Native: OpenAI Responses API `text.format` (PRP-0082, UDR-0058 D2). Both
        # the explicit-schema and the generic modes use the `json_schema` format
        # type. Merged into the run options next to `text.verbosity` (the AG-UI
        # endpoint deep-merges `text`).
        #
        # The legacy `json_object` format type is intentionally NOT used: the
        # Responses API rejects it (HTTP 400 "Response input messages must contain
        # the word 'json' ...") unless an input message literally contains the word
        # "json", which our assembled system prompt (Identity -> Memory ->
        # capabilities) does not guarantee. `json_schema` carries no such
        # requirement, so the generic mode is expressed as a wide-open object schema
        # with strict=false -- functionally equivalent to json_object (any JSON
        # object) but without the prompt-content constraint.
        eff = effective_schema(schema, mode, self.default_output_schema(model))
        if eff is None:
            return {}
        # PRP-0131 / UDR-0058 D10: the forced-tool-use fallback is gone (it was never
        # MAF-compatible). A withheld native path now reports supported=False, so this
        # branch is unreachable from the UI; emit nothing rather than an invalid shape.
        if not self.structured_output_support(model)["native"]:
            return {}
        explicit = mode == "json_schema" and isinstance(schema, dict) and bool(schema)
        return {
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": STRUCTURED_OUTPUT_NAME,
                    "schema": eff,
                    # strict=true gives the conformance guarantee for an explicit
                    # schema; the generic open schema uses strict=false (an open
                    # object cannot satisfy strict's closed-schema requirements).
                    "strict": explicit,
                }
            }
        }
