"""Parse + validate + map a declarative agent YAML (CTR-0142, PRP-0094, UDR-0072).

The YAML is a SPECIFICATION; ChatWalaʻau OWNS construction (UDR-0072 D1/D2). This
module:

1. Parses the YAML document (best-effort schema validation via the MAF
   ``AgentFactory`` when ``agent-framework-declarative`` is installed, else our own
   structural checks -- the runtime agent is built by ChatWalaʻau regardless,
   UDR-0072 D2).
2. Maps the fields ChatWalaʻau understands onto a ``DeclarativeAgentSpec``:
   ``instructions`` -> Identity slot #1 (D6); ``model.id`` -> model selection (D4);
   ``model.options`` -> reasoning effort / verbosity only (D5); ``outputSchema`` ->
   structured output (D5).
3. REJECTS incompatible model options (temperature / top_p / top_k / seed / ...)
   with a visible ``DeclarativeAgentError`` (D5), and IGNORES credentials /
   connection and unknown fields with a recorded warning (D3).
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from app.agent.declarative.spec import (
    IDENTITY_SENTINEL,
    DeclarativeAgentError,
    DeclarativeAgentSpec,
)
from app.agent.declarative.tool_ids import (
    function_id,
    mcp_server_id,
    mcp_tool_id,
    skill_id,
)

logger = logging.getLogger(__name__)

# Classic sampling / generation params ChatWalaʻau REJECTS (UDR-0072 D5). Azure
# gpt-5.5 / gpt-5.4 and Anthropic Opus 4.7 / 4.8 are reasoning-only; the Anthropic
# Messages API returns HTTP 400 on these. Compared after normalizing a key to
# lowercase with underscores removed, so "top_p" and "topP" both match.
_REJECTED_OPTION_KEYS = {
    "temperature",
    "topp",
    "topk",
    "seed",
    "frequencypenalty",
    "presencepenalty",
    "stop",
    "stopsequences",
    "logprobs",
    "toplogprobs",
}

# model.options keys ChatWalaʻau MAPS (everything else is ignored with a warning).
_EFFORT_KEYS = {"effort", "reasoningeffort"}
_VERBOSITY_KEYS = {"verbosity", "textverbosity"}

# Valid generation-option values (union across providers; see the EFFORT / VERBOSITY
# level constants in app.providers.{azure_openai,anthropic}). Validated here so a typo
# (e.g. effort: ultra) is reported as a warning -- which, per UDR-0072 D9, blocks
# activation -- instead of silently falling back to the model default at run time.
_ALLOWED_EFFORT = {"low", "medium", "high", "xhigh", "max"}
_ALLOWED_VERBOSITY = {"low", "medium", "high"}


def _norm_key(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def parse_yaml(text: str) -> dict[str, Any]:
    """Parse a declarative YAML document into a dict, raising on malformed input."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DeclarativeAgentError(f"Invalid YAML: {exc}") from exc
    if data is None:
        raise DeclarativeAgentError("Empty declarative agent document.")
    if not isinstance(data, dict):
        raise DeclarativeAgentError("Declarative agent document must be a YAML mapping.")
    return data


def _validate_with_maf(text: str) -> None:
    """Best-effort schema validation via the MAF declarative parser (UDR-0072 D2).

    ``agent-framework-declarative`` is the operator-named front end
    (``AgentFactory.create_agent_from_yaml``). We only use it to VALIDATE the
    document shape; the returned reference agent is discarded -- ChatWalaʻau builds
    the runtime agent. When the package is not installed we silently fall back to
    our own structural checks (this module's mapping is authoritative either way).
    """
    try:
        from agent_framework.declarative import AgentFactory  # type: ignore[import-not-found]
    except Exception:  # package optional; the mapping below is authoritative
        return
    try:
        # safe_mode is fixed True (UDR-0072 D2): never resolve a remote connection
        # or execute anything while merely validating the document shape.
        AgentFactory().validate_yaml(text)  # type: ignore[attr-defined]
    except AttributeError:
        # Older/newer SDKs may not expose a standalone validate; skip silently.
        return
    except Exception as exc:  # surface as a domain error
        raise DeclarativeAgentError(f"MAF declarative validation failed: {exc}") from exc


def _map_model(data: dict[str, Any], warnings: list[str]) -> tuple[list[str] | None, dict | None]:
    """Map the YAML ``model`` block to (model_filter, model_options_override).

    Honors ``model.id`` for selection (D4) and ``model.options`` for the compatible
    subset only (D5). Rejects sampling params; ignores connection / credentials and
    unknown options with a warning (D3). ``model.id`` may be a scalar or a list.
    """
    model = data.get("model")
    if model is None:
        return None, None
    if not isinstance(model, dict):
        # A bare scalar model id is allowed (model: gpt-5.5).
        return [str(model)], None

    # Selection (D4). Ignore PowerFx expressions (e.g. "=Env.AZURE_OPENAI_MODEL"):
    # ChatWalaʻau owns model selection; an unresolved expression means "inherit".
    model_filter: list[str] | None = None
    raw_id = model.get("id")
    if isinstance(raw_id, str) and raw_id and not raw_id.lstrip().startswith("="):
        model_filter = [raw_id.strip()]
    elif isinstance(raw_id, list):
        ids = [str(m).strip() for m in raw_id if str(m).strip() and not str(m).lstrip().startswith("=")]
        model_filter = ids or None
    elif isinstance(raw_id, str) and raw_id.lstrip().startswith("="):
        warnings.append(f"model.id PowerFx expression {raw_id!r} ignored; ChatWalaʻau owns model selection.")

    # Credentials / connection are NEVER honored (D3).
    if "connection" in model:
        warnings.append("model.connection ignored; ChatWalaʻau resolves credentials and endpoints itself.")

    # Options: map the compatible subset, reject sampling params, warn on the rest.
    options = model.get("options")
    selected: dict[str, Any] = {}
    if isinstance(options, dict):
        for key, val in options.items():
            nk = _norm_key(str(key))
            if nk in _REJECTED_OPTION_KEYS:
                raise DeclarativeAgentError(
                    f"model.options.{key} is not supported: ChatWalaʻau is reasoning-only "
                    "(Azure gpt-5.5/gpt-5.4, Anthropic Opus 4.7/4.8). Remove temperature / "
                    "top_p / top_k / seed and similar sampling parameters."
                )
            if nk in _EFFORT_KEYS:
                sval = str(val)
                if sval not in _ALLOWED_EFFORT:
                    warnings.append(
                        f"model.options.{key}={sval!r} is not a valid reasoning effort "
                        f"(allowed: {', '.join(sorted(_ALLOWED_EFFORT))})."
                    )
                else:
                    selected["effort"] = sval
            elif nk in _VERBOSITY_KEYS:
                sval = str(val)
                if sval not in _ALLOWED_VERBOSITY:
                    warnings.append(
                        f"model.options.{key}={sval!r} is not a valid verbosity "
                        f"(allowed: {', '.join(sorted(_ALLOWED_VERBOSITY))})."
                    )
                else:
                    selected["verbosity"] = sval
            else:
                warnings.append(f"model.options.{key} ignored (not a ChatWalaʻau-mapped option).")
    elif options is not None:
        warnings.append("model.options ignored (expected a mapping).")

    return model_filter, (selected or None)


def _map_output_schema(data: dict[str, Any], warnings: list[str]) -> dict | None:
    """Map the YAML ``outputSchema`` (simplified properties form) to a default
    structured-output spec (D5). Unmappable -> warn + ignore (not a hard error)."""
    raw = data.get("outputSchema") or data.get("output_schema")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.append("outputSchema ignored (expected a mapping).")
        return None
    props = raw.get("properties")
    if not isinstance(props, dict) or not props:
        # Enabled-but-no-usable-schema -> generic JSON object mode (UDR-0058 D3).
        return {"schema": None, "mode": "json_object"}
    json_props: dict[str, Any] = {}
    for name, desc in props.items():
        if not isinstance(desc, dict):
            warnings.append(f"outputSchema.properties.{name} ignored (expected a mapping).")
            continue
        prop: dict[str, Any] = {"type": str(desc.get("type", "string"))}
        if desc.get("description"):
            prop["description"] = str(desc["description"])
        json_props[str(name)] = prop
    if not json_props:
        return {"schema": None, "mode": "json_object"}
    # OpenAI strict json_schema (the explicit-schema path) REQUIRES the object to be
    # closed (additionalProperties: false) and EVERY property listed in `required` --
    # optional properties are not allowed under strict mode. We therefore close the
    # object and require all properties (the documented strict behavior; not a
    # warning, so it does not block activation under UDR-0072 D9).
    schema: dict[str, Any] = {
        "type": "object",
        "properties": json_props,
        "required": list(json_props.keys()),
        "additionalProperties": False,
    }
    return {"schema": schema, "mode": "json_schema"}


# YAML ``tools[].kind`` values ChatWalaʻau can SELECT (PRP-0117, UDR-0100 D1). The
# other MAF tool kinds (web_search / file_search / code_interpreter / custom /
# openapi) are either provider-supplied (web search) or unsupported as a per-agent
# selection here; they are recorded as a warning (which blocks activation, D9).
_SELECTABLE_TOOL_KINDS = {"function", "mcp", "skill"}


def _map_tools(data: dict[str, Any], warnings: list[str]) -> list[str] | None:
    """Map the YAML ``tools`` block to a ``tool_allowlist`` (PRP-0117, UDR-0100 D1).

    Returns a list of stable identifiers (see ``app.agent.declarative.tool_ids``)
    that SUBSETS the globally available tool surface, or ``None`` when there is no
    ``tools`` block (inherit the full surface -- CORE stays byte-for-byte). Structural
    problems (a non-list block, a non-mapping entry, a missing name, or an
    unsupported kind) are recorded as warnings; whether each identifier resolves to a
    tool that actually exists is validated later against live state in the loader
    (``_annotate_tool_warnings``), mirroring the model-id warning path (UDR-0072 D4).
    """
    raw = data.get("tools")
    if raw is None:
        return None
    if not isinstance(raw, list):
        warnings.append("tools ignored (expected a list); the agent will inherit all tools.")
        return None

    ids: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            warnings.append(f"tools[{index}] ignored (expected a mapping).")
            continue
        kind = str(entry.get("kind", "function")).strip().lower()
        name = str(entry.get("name") or "").strip()
        if kind not in _SELECTABLE_TOOL_KINDS:
            warnings.append(
                f"tools[{index}] kind {kind!r} is not selectable per-agent "
                "(allowed: function, mcp, skill; web_search is provider-supplied)."
            )
            continue
        if not name:
            warnings.append(f"tools[{index}] ignored (missing name).")
            continue
        if kind == "function":
            ids.append(function_id(name))
        elif kind == "skill":
            ids.append(skill_id(name))
        else:  # mcp
            allowed = entry.get("allowedTools") or entry.get("allowed_tools")
            if isinstance(allowed, list) and allowed:
                for tool_name in allowed:
                    tn = str(tool_name).strip()
                    if tn:
                        ids.append(mcp_tool_id(name, tn))
            else:
                ids.append(mcp_server_id(name))

    # De-duplicate while preserving order (a whole-server id is kept alongside any
    # per-tool ids; parse_allowlist collapses them to the whole server at build time).
    seen: set[str] = set()
    deduped = [i for i in ids if not (i in seen or seen.add(i))]
    return deduped or None


def map_document(
    text: str,
    *,
    agent_id: str,
    source: str,
    group_path: tuple[str, ...] = (),
    default_name: str | None = None,
) -> DeclarativeAgentSpec:
    """Parse + validate + map a declarative YAML document to a spec (CTR-0142).

    Raises ``DeclarativeAgentError`` on malformed YAML or a rejected option (D5/D9);
    records non-fatal notes (ignored connection / unknown options / unmappable
    outputSchema) on ``spec.warnings`` (D3).
    """
    _validate_with_maf(text)
    data = parse_yaml(text)

    warnings: list[str] = []

    name = str(data.get("name") or data.get("displayName") or default_name or agent_id)
    description = str(data.get("description") or "")

    # instructions -> Identity slot #1 (D6). The sentinel "=Identity" (and absence)
    # means "inherit the runtime Identity" so .agent/IDENTITY.md still applies.
    instr = data.get("instructions")
    instructions_override: str | None = None
    if isinstance(instr, str):
        stripped = instr.strip()
        if stripped and stripped != IDENTITY_SENTINEL:
            instructions_override = instr

    model_filter, model_options_override = _map_model(data, warnings)
    structured_output = _map_output_schema(data, warnings)
    tool_allowlist = _map_tools(data, warnings)

    return DeclarativeAgentSpec(
        id=agent_id,
        name=name,
        description=description,
        source=source,
        group_path=group_path,
        instructions_override=instructions_override,
        model_filter=model_filter,
        model_options_override=model_options_override,
        structured_output=structured_output,
        tool_allowlist=tool_allowlist,
        warnings=warnings,
    )


__all__ = ["map_document", "parse_yaml"]
