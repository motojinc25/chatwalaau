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
from app.agent.wire_trace import describe_wire_input_full, log_wire_request, wire_pairing_report
from app.azure_credential import get_chat_client_credential_kwargs
from app.core.config import settings
from app.providers.base import (
    EFFORT_DEFAULT,
    EFFORT_LEVELS,
    hosted_tool_withheld,
    max_output_tokens_for,
    resolve_effort_level,
)
from app.providers.structured import (
    GENERIC_OBJECT_SCHEMA,
    STRUCTURED_OUTPUT_NAME,
    dedupe_wire_input,
    drop_orphan_outputs,
    effective_schema,
    orphan_outputs,
    pairing_undecidable,
    strip_loop_iteration_marker,
    strip_web_search,
    summarize_removed,
    unanswered_calls,
)

logger = logging.getLogger(__name__)

NAME = "azure-openai"

# Reasoning effort catalog (PRP-0071, UDR-0047 D2/D3; ladder unified by PRP-0184,
# UDR-0166 D5). The reasoning-only policy still hides none / minimal -- only low
# and above are offered (UDR-0047 D3) -- and the ladder now runs to `max`, the
# same five levels the Anthropic lane offers, with the same `xhigh` default.
#
# `max` is shipped WITHOUT a recorded live measurement, by decision (PRP-0184 Q5):
# the deployment operates only current-generation base models (GPT-6 Astra /
# GPT-5.6 Sol). Effort is sent on EVERY turn, so an offering whose endpoint does
# not accept `max` fails its first turn at that level rather than degrading --
# that 400 is the signal, and the answer is the offering's `family` (or a narrower
# ladder), not a silent fallback here.
OPENAI_EFFORT_LEVELS: tuple[str, ...] = EFFORT_LEVELS
OPENAI_EFFORT_DEFAULT = EFFORT_DEFAULT

# Text verbosity is DERIVED from the effort and is no longer selectable
# (PRP-0184, UDR-0166 D6). One axis, not two: "how hard it thinks" also decides
# "how much it says". The mapping saturates at `high`, so the three levels above
# `medium` all answer expansively -- the combination this gives up deliberately is
# "think hard, answer briefly" (PRP-0184 Q3).
OPENAI_VERBOSITY_LEVELS: tuple[str, ...] = ("low", "medium", "high")
OPENAI_VERBOSITY_FOR_EFFORT: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


# Structured output vs. hosted web search (PRP-0082, UDR-0058 D2). The OpenAI
# Responses API rejects the hosted web_search tool together with a JSON response
# format ("Web Search cannot be used with JSON mode."). We subclass the connector
# and, at the single request-assembly chokepoint, drop web_search whenever a JSON
# `text.format` is set. Always applied; inert (byte-for-byte) when no structured
# format is present, so non-structured turns are unchanged.


class _StructuredOutputMixin:
    async def _prepare_options(self, messages: Any, options: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # UDR-0164 D4 (PRP-0182): forward positional extras. MAF 1.19.0's Anthropic
        # connector began passing a per-request `request_state` POSITIONALLY; a signature
        # without *args failed every request with TypeError before it was sent.
        run_options = await super()._prepare_options(messages, options, *args, **kwargs)  # type: ignore[misc]
        # PRP-0151 C4 / UDR-0129 D8, UDR-0113 posture. MAF 1.15.0's AgentLoopMiddleware
        # stamps `_agent_loop_iteration` into context.options for the length of a
        # harness turn, and no connector filters it back out -- the Responses client
        # seeds run_options from a denylist that does not list it, so it reaches
        # `responses.create()` as a raw kwarg and the SDK raises TypeError. That killed
        # the FIRST model call of every Harness run-target turn on this lane. Removed
        # here, at the chokepoint this provider already owns; inert when absent.
        if strip_loop_iteration_marker(run_options):
            logger.debug("[wire] removed MAF's internal harness-loop marker from the request")
        text_cfg = run_options.get("text")
        if isinstance(text_cfg, dict) and text_cfg.get("format") is not None:
            strip_web_search(run_options)
        # Wire tracing (PRP-0141 follow-up; app.agent.wire_trace since PRP-0179): the
        # request as it will go on the wire, ids only.
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
        # in the same request is a guaranteed 400, and no item legitimately answers a
        # call with a bare output. So this direction is REMOVED, not merely reported.
        orphaned = drop_orphan_outputs(run_options)
        if orphaned:
            logger.info("[wire pairing] removed %d orphan output item(s) with no matching call", orphaned)
        _report_pairing(run_options)
        return run_options

    def _get_conversation_id(self, response: Any, store: Any) -> str | None:
        """Never treat a response as a resumable server-side conversation unless the
        request explicitly asked for server-side storage (PRP-0142 follow-up, UDR-0123).

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

    def _shell_item_to_contents(self, item: Any, local_shell_tool_name: str | None) -> list[Any]:
        """Keep the harness shell LOCAL on Azure (PRP-0182 D5.3, UDR-0164 D5).

        MAF 1.19.0 (#8294, openai 1.14.4) turns a Responses ``shell_call`` into a LOCAL
        ``function_call`` -- the one ``WorkspaceShellTool`` executes -- only when the item
        says ``environment.type == "local"``; anything else becomes a hosted
        ``shell_tool_call`` that nothing here runs, and the NEXT request fails with
        ``400 No tool output found for shell call``. Measured live (PRP-0182 ML-1): Azure
        OpenAI returns ``environment: null`` for a shell tool declared
        ``{"type": "shell", "environment": {"type": "local"}}``.

        So, exactly the case MAF 1.18.0 treated as local -- a ``shell_call`` with NO
        environment while a local shell tool is configured -- is handed to upstream with
        the local environment filled in, and nothing else is touched (a hosted
        environment stays hosted). Stateless replay then re-sends that item, local
        environment included; Azure accepts it (measured live, PRP-0182 ML-1b), so no
        framework field is read or rewritten here (UDR-0131 D4 stands).

        Private-seam override: listed in the UDR-0110 D2 residue inventory
        (test_prp0182_maf_119_upgrade.py).
        """
        if (
            local_shell_tool_name
            and getattr(item, "type", None) == "shell_call"
            and getattr(item, "environment", None) is None
            and hasattr(item, "model_copy")
        ):
            from openai.types.responses.response_local_environment import ResponseLocalEnvironment

            item = item.model_copy(update={"environment": ResponseLocalEnvironment(type="local")})
        return super()._shell_item_to_contents(item, local_shell_tool_name)  # type: ignore[misc]


def _report_pairing(run_options: dict[str, Any]) -> None:
    """Post-repair self-verification, BOTH directions (PRP-0149 C3, UDR-0126 D6/D8).

    The seam has printed the PRE-repair verdict (UDR-0126 D5) and applied its
    repairs. It now checks its own work, so the next provider rejection on this path
    arrives PRE-EXPLAINED, one line above the traceback, instead of being deduced
    afterwards from a log that already contained the answer (RES-0003 Finding B).

    Removing unanswered CALLS runs in REPORT-ONLY mode (PRP-0148 Section 6.4, UDR-0132
    D3). Its original reason -- a wrong orphan rule would break the tool-approval flow
    -- went away with that flow (PRP-0179, UDR-0161 D9), but the gate is kept on
    purpose: enabling the removal is one change at this call site, to be taken on
    measured traffic rather than on reasoning about a seam that has already produced
    two wrong fixes.

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
    # Responses API stores server-side by default and chains via previous_response_id
    # (PRP-0142). Inherited by OpenAIProvider and FoundryProvider.
    stores_responses_server_side = True
    # The Responses API counts `input_tokens_details.cached_tokens` INSIDE
    # `usage.input_tokens`, so the cached prefix is already part of the reported
    # input and must be SUBTRACTED to obtain the full-price (uncached) input
    # (PRP-0157, UDR-0135 D5). Inherited by OpenAIProvider and FoundryProvider,
    # which are the same API surface.
    input_tokens_include_cache_read = True

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
        # Generalized per-model option catalog (PRP-0081, UDR-0057 D2/D4), reduced
        # to ONE axis by PRP-0184 (UDR-0166 D6): effort is the only selectable
        # generation option, and verbosity / summary / output budget are derived
        # from it in build_model_options. It does NOT advertise temperature /
        # top_p / top_k: they are not part of a reasoning model's request
        # (reasoning-only policy, UDR-0047 D3 / UDR-0057 D3).
        #
        # The catalog is consumed by the RUN-TARGET authoring surfaces (the agent
        # card, the agent / harness detail screens, the narrow run-target picker);
        # since PRP-0184 it is no longer a per-message chat control (UDR-0166 D1).
        #
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
        # The selection comes from the RUN-TARGET definition, not from the request
        # (PRP-0184, UDR-0166 D1); an unknown value resolves to the shared default.
        effort = resolve_effort_level(selected)
        options: dict[str, Any] = {
            # `summary: detailed` stays fixed and unselectable (UDR-0166 D6). It is
            # entangled with the Foundry lane's `include: reasoning.encrypted_content`
            # assertion (UDR-0128 D3), a seam that has already produced two wrong
            # fixes; PRP-0184 deliberately leaves it alone.
            "reasoning": {"effort": effort, "summary": "detailed"},
            # Text verbosity, DERIVED from the effort and ALWAYS sent (UDR-0166 D6).
            # Before PRP-0184 this was a second selectable axis sent only when it
            # differed from the API default; with one axis there is no "unchanged
            # default" left to preserve, so the key is always stated.
            "text": {"verbosity": OPENAI_VERBOSITY_FOR_EFFORT[effort]},
            # Output budget, DERIVED from the effort (UDR-0166 D6). MAF maps
            # ChatOptions `max_tokens` onto the Responses API `max_output_tokens`,
            # which counts reasoning AND visible text, so the budget scales with the
            # effort for the same reason the Anthropic lane's always has.
            #
            # NOTE: this lane sent NO cap before PRP-0184 -- the model's own ceiling
            # applied. A long generation at `low` now stops at 16000 where it used to
            # run further; that is the accepted cost of one shared table (PRP-0184 Q4),
            # and `stop_reason: max_output_tokens` at low effort is its signature.
            "max_tokens": max_output_tokens_for(effort),
        }
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
