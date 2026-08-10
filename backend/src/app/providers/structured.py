"""Shared structured-output helpers (CTR-0102 v5, PRP-0082, UDR-0058).

Pure, provider-agnostic helpers used by the azure-openai / anthropic providers and
by the AG-UI endpoint. The per-provider NATIVE request shapes live in each provider
module; everything here is provider-neutral: the two DEFAULT schemas a provider may
choose between (UDR-0058 D9), the explicit-vs-default schema resolution (UDR-0058 D3),
and the soft, non-blocking final-message validation (UDR-0058 D4).
"""

from __future__ import annotations

import json
from typing import Any

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


def resolve_request(state: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """Resolve an AG-UI ``state`` into ``(schema, mode)`` (UDR-0058 D3).

    - A non-empty ``state.output_schema`` object -> (schema, "json_schema").
    - Else ``state.output_format == "json_object"`` -> (None, "json_object").
    - Else -> (None, "none") = structured output off (default).

    Never raises; an unexpected shape resolves to off.
    """
    if not isinstance(state, dict):
        return None, MODE_NONE
    raw_schema = state.get("output_schema")
    if isinstance(raw_schema, dict) and raw_schema:
        return raw_schema, MODE_JSON_SCHEMA
    if state.get("output_format") == MODE_JSON_OBJECT:
        return None, MODE_JSON_OBJECT
    return None, MODE_NONE


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


# The tools MAF's SkillsProvider injects at run time (progressive disclosure:
# load_skill / read_skill_resource / run_skill_script). They are NOT part of the agent's
# static tool list -- the context provider merges them into the request's `tools` just
# before the call (agent_framework/_agents.py:1550-1554), which is why the request
# assembly is the one place that can see and gate them.
SKILL_TOOL_NAMES = frozenset({"load_skill", "read_skill_resource", "run_skill_script"})


def is_skill_tool(tool: Any) -> bool:
    """True when ``tool`` is one of the Agent Skills progressive-disclosure tools."""
    name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
    return isinstance(name, str) and name in SKILL_TOOL_NAMES


def strip_skill_tools(run_options: dict[str, Any]) -> dict[str, Any]:
    """Remove the Agent Skills tools from a request's ``tools`` list in place.

    Skills are INCOMPATIBLE with a background run (PRP-0134, v0.127.0). A background
    response is produced server-side and resumed later by id; the framework releases its
    background continuation token only on its non-streaming path, so a resumed turn that
    called a skill re-read the finished response instead of POSTing the tool result and
    the service answered ``No tool call found for function call id call_...``.

    Rather than work around that, the two features are made mutually exclusive: Background
    takes precedence for the turn and the skill tools are dropped. This follows the
    UDR-0058 D2 discipline the hosted web-search strip already uses -- an incompatible
    tool is DROPPED, never allowed to error the turn -- and it is announced in the UI
    rather than being a silent removal.

    Drops the ``tools`` key entirely when nothing remains. Function tools, MCP tools and
    hosted tools are unaffected. Mutates and returns ``run_options``.
    """
    tools = run_options.get("tools")
    if isinstance(tools, list):
        kept = [t for t in tools if not is_skill_tool(t)]
        if kept:
            run_options["tools"] = kept
        else:
            run_options.pop("tools", None)
    return run_options


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
