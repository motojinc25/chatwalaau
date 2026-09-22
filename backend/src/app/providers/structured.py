"""Shared structured-output helpers (CTR-0102 v5, PRP-0082, UDR-0058).

Pure, provider-agnostic helpers used by the azure-openai / anthropic providers and
by the AG-UI endpoint. The per-provider NATIVE request shapes live in each provider
module; everything here is provider-neutral: the two DEFAULT schemas a provider may
choose between (UDR-0058 D9), the explicit-vs-default schema resolution (UDR-0058 D3),
and the soft, non-blocking final-message validation (UDR-0058 D4).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Name of the ephemeral tool used by the OpenAI-native json_schema format. Kept stable
# so the endpoint can recognise it.
STRUCTURED_OUTPUT_NAME = "structured_output"

# Generic permissive schema used for the json_object / no-schema mode (UDR-0058 D3).
#
# PRP-0131 / UDR-0058 D9: this shape is EXPRESSIBLE only where the provider can lift
# its closed-schema requirement -- OpenAI does so with `strict: false`. Anthropic
# cannot: `additionalProperties` must be `false` on every object and may not be
# omitted, and `output_config.format` has no strict toggle. A provider in that
# position returns the CLOSED default below from `default_output_schema` instead.
GENERIC_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}

# The default for a provider that CANNOT express an open object (PRP-0131, UDR-0058
# D11). Anthropic requires `additionalProperties: false` on every object and does not
# allow it to be omitted, so "any JSON object" has no representation there -- but a
# minimal CLOSED object does. This is the same shape the schema editor already offers
# as its placeholder, so the default a model falls back to is the example operators
# are shown.
#
# It carries a real semantic difference from the open schema and that is deliberate,
# not hidden: on such a model "no schema" means an object with one `answer` string,
# and the capability map publishes this so the surface can say so. An explicit schema
# always overrides it.
CLOSED_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

# Accepted output-format modes. "none" means structured output is OFF (default).
MODE_NONE = "none"
MODE_JSON_SCHEMA = "json_schema"
MODE_JSON_OBJECT = "json_object"
VALID_MODES = frozenset({MODE_JSON_SCHEMA, MODE_JSON_OBJECT})


# ``resolve_request(state)`` -- which turned an AG-UI ``state.output_schema`` /
# ``state.output_format`` pair into (schema, mode) -- was REMOVED by PRP-0184
# (UDR-0166 D10). Structured output is configured on the run-target now, so those
# request keys are accepted and ignored and nothing parses them. The function is
# gone rather than left unused: an unused parser is an invitation to wire the
# second lane back up.


def effective_schema(
    schema: dict[str, Any] | None,
    mode: str,
    default: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the schema to constrain against, or None when not requested.

    ``json_schema`` mode uses the explicit ``schema`` (falling back to the DEFAULT
    when it is missing/empty); ``json_object`` uses the default; any other mode
    returns None (UDR-0058 D3).

    ``default`` is the PROVIDER's default output schema (UDR-0058 D9) -- open where
    the provider can express one, closed where it cannot. It defaults to the open
    schema so a caller that does not care keeps the pre-PRP-0131 behavior.
    """
    if mode == MODE_JSON_SCHEMA and isinstance(schema, dict) and schema:
        return schema
    if mode in VALID_MODES:
        return default if default is not None else GENERIC_OBJECT_SCHEMA
    return None


# REMOVED: `forced_tool_use_fragment` (PRP-0131, UDR-0058 D10).
#
# It was declared "the universal fallback" by UDR-0058 D2 and never executed -- its own
# docstring said "No current model takes this path ... the fragment is shape-tested",
# and the shape it was tested against was ours, not MAF's. PRP-0130 made the non-native
# path reachable and the first real use raised
# `ContentError: tool_choice dict must contain 'mode' key`.
#
# It was incompatible in TWO independent ways, so the error was not the whole problem:
#   (a) MAF requires tool_choice == {"mode": auto|required|none, ...}
#       (agent_framework/_types.py:3744) -- ours was OpenAI-shaped.
#   (b) MAF connectors read each tool's schema from `tool.parameters()` on a
#       FunctionTool (agent_framework_anthropic/_chat_client.py:982-989) -- a raw
#       OpenAI-shaped Mapping matches no branch, so fixing (a) would have forced a
#       tool_choice naming a tool that was never sent.
#
# A working fallback means building a real MAF FunctionTool with an invocation
# handler. That is a feature, and it has no consumer today: both shipped providers are
# native. Where nothing can constrain the answer the provider now reports
# `supported: false` and the surface omits the feature -- see UDR-0058 D10.


def is_web_search_tool(tool: Any) -> bool:
    """True when ``tool`` is a hosted web-search tool (OpenAI / Anthropic).

    OpenAI uses ``web_search`` / ``web_search_preview``; Anthropic uses
    ``web_search_20250305``. Both serialize to an object with a ``type`` field, so
    match by the ``web_search`` prefix on either a dict or an SDK object.
    """
    t = tool.get("type") if isinstance(tool, dict) else getattr(tool, "type", None)
    return isinstance(t, str) and t.startswith("web_search")


def strip_web_search(run_options: dict[str, Any]) -> dict[str, Any]:
    """Remove the hosted web-search tool from a request's ``tools`` list in place.

    Hosted web search is INCOMPATIBLE with JSON / structured-output mode: OpenAI
    returns ``400 "Web Search cannot be used with JSON mode."`` Structured output
    takes precedence over web search for that turn (UDR-0058 D2: never error). Drops
    the ``tools`` key entirely when nothing remains. Function tools (coding, weather,
    etc.) are JSON-mode compatible and are kept. Mutates and returns ``run_options``.
    """
    tools = run_options.get("tools")
    if isinstance(tools, list):
        kept = [t for t in tools if not is_web_search_tool(t)]
        if kept:
            run_options["tools"] = kept
        else:
            run_options.pop("tools", None)
    return run_options


# ---------------------------------------------------------------------------
# Harness loop marker leakage (PRP-0151 C4, UDR-0129 D8; UDR-0113 posture)
# ---------------------------------------------------------------------------


_MISSING = object()


def _loop_iteration_key() -> str:
    """MAF's harness-loop marker key, DERIVED rather than mirrored.

    UDR-0109 D4 / UDR-0129 D2: never hardcode a value while claiming to mirror a
    constant. The literal is only the fallback for a MAF that stops exporting it --
    and if that happens the strip becomes a no-op against a key nobody sets, which
    is the safe direction.
    """
    try:
        from agent_framework._agents import _LOOP_ITERATION_TOKEN_KEY

        return str(_LOOP_ITERATION_TOKEN_KEY)
    except Exception:  # pragma: no cover - a MAF that no longer exports the marker
        return "_agent_loop_iteration"


def strip_loop_iteration_marker(run_options: dict[str, Any]) -> bool:
    """Remove MAF's internal harness-loop marker from an outgoing request.

    Returns True when a marker was removed.

    MAF 1.15.0's ``AgentLoopMiddleware`` stamps ``_agent_loop_iteration`` into
    ``context.options`` for the duration of a harness turn (``_harness/_loop.py``),
    as a private signal read by ``_agents.py``. No connector filters it back out:
    ``RawOpenAIChatClient._prepare_options`` seeds ``run_options`` from a DENYLIST
    (``exclude_keys``) that does not list it, and the Anthropic connector does the
    same, so the marker is forwarded as a raw request kwarg and the SDK rejects it::

        TypeError: AsyncResponses.create() got an unexpected keyword argument
                   '_agent_loop_iteration'

    That killed the FIRST model call of every Harness run-target turn on the OpenAI,
    Azure OpenAI and Foundry lanes, and would have done the same on Anthropic.

    This is an UPSTREAM defect corrected at the seam ChatWalaʻau already owns
    (UDR-0113): the lane mixins hold the single request-assembly chokepoint, the
    removal is one named key, and stripping it from ``run_options`` does NOT disturb
    ``context.options`` -- a different object -- so the loop's own use of the marker
    is untouched. Inert (byte-for-byte) on any request that does not carry it.
    """
    return run_options.pop(_loop_iteration_key(), _MISSING) is not _MISSING


# ---------------------------------------------------------------------------
# Identity uniqueness of the request input (PRP-0147, UDR-0126 D3/D4)
# ---------------------------------------------------------------------------
#
# Which field carries the provider-assigned identity, per item type. An item type
# absent from this map has NO such identity and is NEVER de-duplicated -- notably
# `message`, where identical content is legitimate (a user can send the same text
# twice) and removing it would delete a real message (UDR-0126 D4).
_IDENTITY_FIELD: dict[str, str] = {
    "function_call": "call_id",
    # PRP-0182 (UDR-0164 D5): MAF 1.19.0 replays a local harness shell call as the
    # provider's own `shell_call` item, so it is de-duplicated like a function call.
    "shell_call": "call_id",
    "function_call_output": "call_id",
    "shell_call_output": "call_id",
    "local_shell_call_output": "id",
    "reasoning": "id",
    "mcp_approval_request": "id",
    "mcp_approval_response": "approval_request_id",
}


def _identity(item: Any) -> tuple[str, str] | None:
    """The (type, provider-assigned id) key an item is de-duplicated on, or None."""
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return None
    field = _IDENTITY_FIELD.get(item_type)
    if field is None:
        return None
    value = item.get(field)
    if not isinstance(value, str) or not value:
        return None
    return (item_type, value)


def dedupe_wire_input(run_options: dict[str, Any]) -> int:
    """Make ``run_options["input"]`` identity-unique in place; return items removed.

    The Responses API assigns each call, reasoning block and approval request a unique
    id; two items in one request carrying the same one is invalid by construction. A
    harness turn nevertheless shipped fifty duplicated ``call_id``s for ~120 consecutive
    rounds (PRP-0147 Finding B) because the function-invocation loop re-submits its whole
    transcript as unattributed input into a history middleware that de-duplicates nothing
    (RES-0002 F1). That PRODUCER is not fixed here; this stops the invalid request from
    leaving the process while it stands.

    Three properties, all load-bearing (UDR-0126 D4):

    * Keyed on provider-assigned IDENTITY, never on content. An item with no such id --
      ``message`` above all -- is never touched.
    * The FIRST occurrence is kept and the order of survivors is unchanged. The first
      copy sits adjacent to the ``reasoning`` item that produced it, and that adjacency
      is what the API relies on when encrypted reasoning is replayed; keeping the last
      would relocate a call after its own output.
    * Two items sharing an identity should be byte-identical. Where they are NOT, the
      survivor keeps the first occurrence's POSITION and adopts the more AUTHORITATIVE
      field value (UDR-0126 D7, PRP-0148 C3): D4's "keep the first" governs position,
      not content. Only a difference with no defined resolution is a WARNING.

    Inert and silent on a request with no duplicates. Never raises: a repair at the
    request seam must not become a new way for a turn to fail.
    """
    try:
        items = run_options.get("input")
        if not isinstance(items, list):
            return 0
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        kept: list[Any] = []
        removed = 0
        merged_ids = 0
        for item in items:
            key = _identity(item)
            if key is None:
                kept.append(item)
                continue
            first = seen.get(key)
            if first is None:
                seen[key] = item
                kept.append(item)
                continue
            removed += 1
            if item != first:
                merged_ids += _merge_into_first(first, item, key)
        if merged_ids:
            # UDR-0126 D7: a known, explained, benign difference with a defined
            # resolution. Logged at DEBUG with a count rather than as a WARNING per
            # item -- eight warnings on every harness request would bury the D5
            # signal beside them, and a warning that fires every time is one that
            # stops being read (RES-0002 F0 is what that costs).
            logger.debug("[wire dedup] adopted %d provider-issued item id(s) from duplicates", merged_ids)
        if removed:
            run_options["input"] = kept
        return removed
    except Exception:  # a repair must never break the request
        logger.exception("[wire dedup] failed; sending the input unchanged")
        return 0


def _is_synthesized_fc_id(item: dict[str, Any]) -> bool:
    """True when a ``function_call``'s ``id`` is the ``fc_<call_id>`` fallback.

    The serializer prefers the provider's own item id when the content still carries
    it, and falls back to synthesizing one otherwise
    (``agent_framework_openai/_chat_client.py:1887-1899``):

        fc_id = content.additional_properties.get("fc_id") or content.call_id
        if not fc_id.startswith("fc_"):
            fc_id = f"fc_{fc_id}"

    So a copy that round-tripped through the history store (losing
    ``additional_properties``) is recognisable by construction. RES-0003 F4: keeping
    whichever copy came first discarded the id upstream deliberately prefers.
    """
    call_id = item.get("call_id")
    item_id = item.get("id")
    return isinstance(call_id, str) and isinstance(item_id, str) and item_id == f"fc_{call_id}"


def _merge_into_first(first: dict[str, Any], other: dict[str, Any], key: tuple[str, str]) -> int:
    """Adopt authoritative values from a dropped duplicate. Returns ids adopted.

    UDR-0126 D7. Mutates ``first`` in place -- it is already in the kept list, so the
    survivor keeps its POSITION while gaining the better content.
    """
    differing = sorted(k for k in set(first) | set(other) if first.get(k) != other.get(k))
    adopted = 0
    if differing == ["id"] and key[0] == "function_call":
        # The one difference with a defined resolution: a provider-issued item id
        # beats the locally synthesized fallback, regardless of which came first.
        if _is_synthesized_fc_id(first) and not _is_synthesized_fc_id(other):
            first["id"] = other["id"]
            adopted = 1
        return adopted
    # WARNING is reserved for differences that remain UNEXPLAINED. Such a pair is not
    # the known duplication and must surface rather than be absorbed.
    logger.warning(
        "[wire dedup] %s %s appears twice with DIFFERING fields %s; keeping the first. "
        "This is not the known duplication.",
        key[0],
        key[1],
        differing,
    )
    return adopted


# ---------------------------------------------------------------------------
# Pairing validity of the request input (PRP-0148 C2, UDR-0126 D6)
# ---------------------------------------------------------------------------
#
# Identity uniqueness and pairing validity are DIFFERENT properties and the provider
# rejects on both. A `function_call` with no matching output fails the whole request:
#
#   400 'No tool output found for function call call_E1izDLYx26D2x7hhyJJHVKtj.'
#
# In the reported incident (RES-0003) the seam had already computed that exact id and
# printed it in the pairing verdict, and sent the request anyway.

# Items that ARE a call, keyed by `call_id`. `shell_call` joined in PRP-0182 (UDR-0164
# D5): from MAF 1.19.0 (#8294) a local harness shell call is replayed as the provider's
# own `shell_call` item instead of a `function_call`. Before this, the orphan-output
# repair below saw its `shell_call_output` as answering NOTHING and deleted it, and
# Azure then rejected the request with
#   400 'No tool output found for shell call call_...'
# (measured live, PRP-0182 ML-1).
_CALL_TYPES = ("function_call", "shell_call")
# Items that ANSWER a call, keyed by the field carrying the call's id.
_ANSWER_BY_CALL_ID = ("function_call_output", "shell_call_output")
# An approval-gated call is answered by an approval item, NOT by an output. These two
# clauses are load-bearing: dropping a gated call would break FEAT-0028 for every
# gated tool (PRP-0148 Section 2.2).
_ANSWER_APPROVAL_BY_ID = ("mcp_approval_request",)
_ANSWER_APPROVAL_BY_REQUEST_ID = ("mcp_approval_response",)
# Carries the originating item's `id` and no `call_id`, so it cannot be matched to a
# call. Its presence makes the request UNDECIDABLE (PRP-0148 Section 2.3).
_UNDECIDABLE = ("local_shell_call_output",)


def pairing_undecidable(items: Any) -> bool:
    """True when the request contains an item that makes pairing unmatchable.

    UDR-0126 D1's "say what you cannot check" binds the repair, not only the
    diagnostic: where this is True the orphan analysis MUST be skipped for the whole
    request rather than guessing at a key.
    """
    if not isinstance(items, list):
        return False
    return any(isinstance(i, dict) and i.get("type") in _UNDECIDABLE for i in items)


def unanswered_calls(items: Any) -> list[tuple[str, str]]:
    """``(call_id, tool_name)`` for every ``function_call`` nothing in the request answers.

    Answered means ANY of: a ``function_call_output`` / ``shell_call_output`` with the
    same ``call_id``; an ``mcp_approval_request`` whose ``id`` is the call id; or an
    ``mcp_approval_response`` whose ``approval_request_id`` is the call id.

    Returns an empty list when the request is undecidable (see ``pairing_undecidable``)
    so a caller cannot act on a partial answer. Never raises.
    """
    try:
        if not isinstance(items, list) or pairing_undecidable(items):
            return []
        answered: set[str] = set()
        calls: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in _CALL_TYPES and isinstance(item.get("call_id"), str):
                calls.append((item["call_id"], str(item.get("name") or itype)))
            elif itype in _ANSWER_BY_CALL_ID and isinstance(item.get("call_id"), str):
                answered.add(item["call_id"])
            elif itype in _ANSWER_APPROVAL_BY_ID and isinstance(item.get("id"), str):
                answered.add(item["id"])
            elif itype in _ANSWER_APPROVAL_BY_REQUEST_ID and isinstance(item.get("approval_request_id"), str):
                answered.add(item["approval_request_id"])
        seen: set[str] = set()
        orphans: list[tuple[str, str]] = []
        for call_id, name in calls:
            if call_id in answered or call_id in seen:
                continue
            seen.add(call_id)
            orphans.append((call_id, name))
        return orphans
    except Exception:  # analysis must never break the request
        logger.exception("[wire pairing] failed to analyse call pairing")
        return []


def orphan_outputs(items: Any) -> list[tuple[str, str]]:
    """``(call_id, item_type)`` for every output item nothing in the request declares.

    The MIRROR of ``unanswered_calls`` (PRP-0149 C2, UDR-0126 D8). The provider rejects
    on both directions and phrases each differently:

        400 'No tool output found for function call call_...'      <- unanswered call
        400 'No tool call found for shell call output ... call_...' <- orphan output

    An output is orphaned when no call (``function_call`` or ``shell_call``,
    ``_CALL_TYPES``) in the SAME request carries its ``call_id``. Approval items are irrelevant here: an approval answers a CALL and is
    never expressed as a bare output, which is why this direction carries none of the
    FEAT-0028 risk that keeps ``unanswered_calls`` removal gated.

    Returns an empty list when the request is undecidable (``pairing_undecidable``).
    Never raises.
    """
    try:
        if not isinstance(items, list) or pairing_undecidable(items):
            return []
        declared: set[str] = set()
        outputs: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in _CALL_TYPES and isinstance(item.get("call_id"), str):
                declared.add(item["call_id"])
            elif itype in _ANSWER_BY_CALL_ID and isinstance(item.get("call_id"), str):
                outputs.append((item["call_id"], str(itype)))
        seen: set[str] = set()
        orphans: list[tuple[str, str]] = []
        for call_id, itype in outputs:
            if call_id in declared or call_id in seen:
                continue
            seen.add(call_id)
            orphans.append((call_id, itype))
        return orphans
    except Exception:  # analysis must never break the request
        logger.exception("[wire pairing] failed to analyse orphan outputs")
        return []


def drop_orphan_outputs(run_options: dict[str, Any]) -> int:
    """Remove outputs whose call is absent; return the number removed (UDR-0126 D8).

    Unlike the unanswered-CALL removal -- still REPORT ONLY behind the PRP-0148
    Section 6.4 gate, because an approval item can be a gated call's only answer --
    this direction ships active. Removing an orphan output can do exactly one thing:
    convert a request the provider is CERTAIN to reject into one it can serve.

    Inert and silent on a valid request. Skipped entirely where pairing is undecidable
    (UDR-0126 D1). Never raises: a repair at the request seam must not become a new way
    for a turn to fail.
    """
    try:
        items = run_options.get("input")
        if not isinstance(items, list):
            return 0
        orphans = {call_id for call_id, _ in orphan_outputs(items)}
        if not orphans:
            return 0
        kept = [
            item
            for item in items
            if not (
                isinstance(item, dict) and item.get("type") in _ANSWER_BY_CALL_ID and item.get("call_id") in orphans
            )
        ]
        removed = len(items) - len(kept)
        if removed:
            run_options["input"] = kept
        return removed
    except Exception:  # a repair must never break the request
        logger.exception("[wire pairing] failed to drop orphan outputs; sending the input unchanged")
        return 0


def summarize_removed(before: list[Any], after: list[Any]) -> str:
    """``50 function_call, 3 reasoning`` -- what the dedup pass removed, by type."""
    counts: dict[str, int] = {}
    kept_ids = {id(i) for i in after}
    for item in before:
        if id(item) in kept_ids:
            continue
        key = _identity(item)
        if key is not None:
            counts[key[0]] = counts.get(key[0], 0) + 1
    return ", ".join(f"{n} {t}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def soft_validate(text: str | None) -> dict[str, Any]:
    """Non-blocking validation of the final structured text (UDR-0058 D4).

    NEVER raises and NEVER rejects/regenerates. Returns a status dict
    ``{"parsed": bool, "reason"?: str}`` used only to surface an indicator on the
    usage event. A ``json`` code fence (if the model wrapped the answer) is stripped
    before parsing. Detects emptiness (refusal / no output) and unparseable JSON
    (truncation / a non-native fallback that did not conform).
    """
    s = (text or "").strip()
    if not s:
        return {"parsed": False, "reason": "empty"}
    if s.startswith("```"):
        # Strip a leading ```json / ``` fence and the trailing fence, best-effort.
        body = s[3:]
        if body[:4].lower() == "json":
            body = body[4:]
        body = body.strip()
        if body.endswith("```"):
            body = body[:-3].strip()
        s = body or s
    try:
        json.loads(s)
        return {"parsed": True}
    except (ValueError, TypeError):
        return {"parsed": False, "reason": "unparseable"}
