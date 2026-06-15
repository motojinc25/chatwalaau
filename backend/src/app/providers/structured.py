"""Shared structured-output helpers (CTR-0102 v5, PRP-0082, UDR-0058).

Pure, provider-agnostic helpers used by the azure-openai / anthropic providers and
by the AG-UI endpoint. The per-provider NATIVE request shapes live in each provider
module; everything here is provider-neutral: the generic permissive schema, the
explicit-vs-generic schema resolution (UDR-0058 D3), the universal forced-tool-use
fallback fragment (UDR-0058 D2), and the soft, non-blocking final-message validation
(UDR-0058 D4).
"""

from __future__ import annotations

import json
from typing import Any

# Name of the single ephemeral tool used by the forced-tool-use fallback and the
# OpenAI-native json_schema format. Kept stable so the endpoint can recognise it.
STRUCTURED_OUTPUT_NAME = "structured_output"

# Generic permissive schema used for the json_object / no-schema mode (UDR-0058 D3).
GENERIC_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}

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


def effective_schema(schema: dict[str, Any] | None, mode: str) -> dict[str, Any] | None:
    """Return the schema to constrain against, or None when not requested.

    ``json_schema`` mode uses the explicit ``schema`` (falling back to the generic
    schema when it is missing/empty); ``json_object`` uses the generic schema; any
    other mode returns None (UDR-0058 D3).
    """
    if mode == MODE_JSON_SCHEMA and isinstance(schema, dict) and schema:
        return schema
    if mode in VALID_MODES:
        return GENERIC_OBJECT_SCHEMA
    return None


def forced_tool_use_fragment(schema: dict[str, Any]) -> dict[str, Any]:
    """Run-options fragment forcing a single schema-shaped tool call (UDR-0058 D2).

    The universal fallback for a model whose provider reports ``native=False``: the
    answer is produced as the arguments of one forced tool call whose ``parameters``
    are the JSON Schema. Both first-party MAF connectors accept ``tools`` +
    ``tool_choice`` in run options. No current model takes this path (both shipped
    providers are native); the fragment is shape-tested and activates only when a
    non-native model is configured.
    """
    return {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": STRUCTURED_OUTPUT_NAME,
                    "description": "Return the final answer as a JSON object matching the schema.",
                    "parameters": schema,
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": STRUCTURED_OUTPUT_NAME}},
    }


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
