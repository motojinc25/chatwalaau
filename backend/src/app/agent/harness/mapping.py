"""Parse + validate + map a ``kind: Harness`` YAML (CTR-0192, PRP-0135, UDR-0119).

The schema is ChatWalaʻau-OWNED (UDR-0119 D1): no MAF declarative factory is
involved. Structural problems and unknown fields are recorded as warnings; ANY
warning blocks the agent from becoming a selectable run-target (UDR-0119 D8).
Credentials / provider / connection / sampling material is never honored (D10).
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from app.agent.harness.spec import (
    ALLOWED_INITIAL_MODES,
    ALLOWED_PLAN_APPROVALS,
    DEFAULT_PLAN_APPROVAL,
    HARNESS_KIND,
    HarnessAgentError,
    HarnessAgentSpec,
)
from app.providers.base import EFFORT_LEVELS

logger = logging.getLogger(__name__)

# Top-level keys the schema understands; anything else is ignored with a warning.
_KNOWN_TOP_KEYS = {
    "kind",
    "name",
    "displayName",
    "description",
    "model",
    "instructions",
    "tools",
    "compaction",
    "todo",
    "mode",
    "fileMemory",
    "fileAccess",
    "webSearch",
    # "toolApproval" is deliberately NOT here (UDR-0119 D6): it is reported by a
    # dedicated warning in map_document rather than the generic unknown-field one.
    "toolApproval",
    "loop",
}

# ChatWalaʻau coding tools are NOT mountable into a harness run -- the
# harness-internal file-access / shell tools own that surface (UDR-0119 D7).
CODING_TOOL_IDS = frozenset(
    f"function:{n}" for n in ("file_read", "file_write", "bash_execute", "file_glob", "file_grep")
)

# The MAF harness loop cap (UDR-0119 D4). Imported from the pinned package's
# harness loop module -- NOT the top-level agent_framework.DEFAULT_MAX_ITERATIONS,
# which is the WORKFLOW superstep cap (a different constant).
try:
    from agent_framework._harness._loop import DEFAULT_MAX_ITERATIONS as HARNESS_MAX_ITERATIONS
except Exception:  # pragma: no cover - pinned dependency; defensive fallback
    HARNESS_MAX_ITERATIONS = 10


def effective_loop_max_iterations(spec: HarnessAgentSpec) -> int:
    """The loop cap a harness actually runs with: the YAML value clamped to MAF's (D4).

    One definition for the factory (CTR-0193), the inventory (CTR-0192) and the progress
    report (CTR-0219), so the "n of N" a user sees is the N the loop enforces.
    """
    return min(spec.loop_max_iterations or HARNESS_MAX_ITERATIONS, HARNESS_MAX_ITERATIONS)


def parse_yaml(text: str) -> dict[str, Any]:
    """Parse a harness YAML document into a dict, raising on malformed input."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HarnessAgentError(f"Invalid YAML: {exc}") from exc
    if data is None:
        raise HarnessAgentError("Empty harness agent document.")
    if not isinstance(data, dict):
        raise HarnessAgentError("Harness agent document must be a YAML mapping.")
    return data


def _block(data: dict[str, Any], key: str, warnings: list[str]) -> dict[str, Any]:
    """Return a nested mapping block, warning (and ignoring) on a wrong shape."""
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        warnings.append(f"{key} ignored (expected a mapping).")
        return {}
    return raw


def _bool(block: dict[str, Any], key: str, prefix: str, warnings: list[str]) -> bool:
    raw = block.get(key)
    if raw is None:
        return False
    if not isinstance(raw, bool):
        warnings.append(f"{prefix}.{key} must be true or false.")
        return False
    return raw


def _positive_int(block: dict[str, Any], key: str, prefix: str, warnings: list[str]) -> int | None:
    raw = block.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        warnings.append(f"{prefix}.{key} must be a positive integer.")
        return None
    return raw


def _map_model(data: dict[str, Any], warnings: list[str]) -> tuple[str, str]:
    """Map ``model`` to (offering id, reasoning effort) (UDR-0119 D2/D10, UDR-0166 D11).

    ``model.options.effort`` is NEW in PRP-0184. Until then the harness factory never
    called ``providers.build_model_options()`` at all, so a harness run sent no
    reasoning effort, no thinking block and no output budget -- the run-target with the
    longest, most reasoning-bound work was the one using the provider's bare defaults.
    An empty effort means "the model family's default".
    """
    model = data.get("model")
    if model is None:
        warnings.append("model.id is required: name exactly one catalog offering.")
        return "", ""
    if isinstance(model, str):
        return model.strip(), ""
    if not isinstance(model, dict):
        warnings.append("model must be a mapping with an 'id' key.")
        return "", ""
    # Credentials / connection are NEVER honored (UDR-0119 D10).
    if "connection" in model:
        warnings.append("model.connection ignored; ChatWalaʻau resolves credentials and endpoints itself.")
    warnings.extend(
        f"model.{key} ignored (not a harness-mapped field)."
        for key in model
        if key not in ("id", "connection", "options")
    )
    raw_id = model.get("id")
    if isinstance(raw_id, list):
        warnings.append("model.id must name exactly ONE offering (a harness agent binds one client).")
        return "", ""
    model_id = str(raw_id or "").strip()
    if not model_id:
        warnings.append("model.id is required: name exactly one catalog offering.")
    return model_id, _map_model_effort(model, warnings)


def _map_model_effort(model: dict[str, Any], warnings: list[str]) -> str:
    """Map ``model.options.effort``; "" means the family default (UDR-0166 D11).

    Effort is the ONLY generation option a harness YAML may set: verbosity, the
    reasoning summary, the thinking mode and the output budget are all derived from it
    or fixed (UDR-0166 D6). An invalid value is a blocking warning, as ``model.id``
    already is (UDR-0119 D8) -- a typo must not silently become the default.
    """
    options = model.get("options")
    if options is None:
        return ""
    if not isinstance(options, dict):
        warnings.append("model.options ignored (expected a mapping).")
        return ""
    effort = ""
    for key, val in options.items():
        nk = str(key).lower().replace("_", "").replace("-", "")
        if nk in ("effort", "reasoningeffort"):
            sval = str(val).strip()
            if sval not in EFFORT_LEVELS:
                warnings.append(
                    f"model.options.{key}={sval!r} is not a valid reasoning effort "
                    f"(allowed: {', '.join(EFFORT_LEVELS)})."
                )
            else:
                effort = sval
        else:
            warnings.append(
                f"model.options.{key} ignored; effort is the only generation option a "
                "harness declares (verbosity and the output budget follow it)."
            )
    return effort


def _map_instructions(data: dict[str, Any], warnings: list[str]) -> tuple[str | None, str | None]:
    """Map ``instructions.harness`` / ``instructions.agent`` (both optional)."""
    block = data.get("instructions")
    if block is None:
        return None, None
    if not isinstance(block, dict):
        warnings.append("instructions ignored (expected a mapping with 'harness' / 'agent').")
        return None, None
    warnings.extend(
        f"instructions.{key} ignored (allowed: harness, agent)." for key in block if key not in ("harness", "agent")
    )
    harness = block.get("harness")
    agent = block.get("agent")
    harness_out = harness if isinstance(harness, str) and harness.strip() else None
    agent_out = agent if isinstance(agent, str) and agent.strip() else None
    return harness_out, agent_out


def _map_tools(data: dict[str, Any], warnings: list[str]) -> list[str]:
    """Map ``tools`` -- a flat list of CTR-0178 identifiers (UDR-0119 D2).

    Skills ride the shared ``SkillsProvider`` (CTR-0043, UDR-0130 D1), never this
    list; ChatWalaʻau coding tools are never mountable (UDR-0119 D7). The
    identifiers' existence against live state is validated in the loader.
    """
    raw = data.get("tools")
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("tools ignored (expected a list of tool identifiers).")
        return []
    ids: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry.strip():
            warnings.append(f"tools[{index}] ignored (expected an identifier string).")
            continue
        ident = entry.strip()
        if ident in CODING_TOOL_IDS:
            warnings.append(
                f"tools[{index}] {ident!r} is not mountable into a harness run: the harness "
                "brings its own file-access and shell tools (UDR-0119 D7)."
            )
            continue
        if ident.startswith("skill:"):
            warnings.append(
                f"tools[{index}] {ident!r} is not a harness tool: Agent Skills load via "
                "SKILLS_DIR (the harness skills provider), not the tools list."
            )
            continue
        if not (ident.startswith("function:") or ident.startswith("mcp:")):
            warnings.append(
                f"tools[{index}] {ident!r} is not a recognized identifier (use function:<name> or mcp:<server>)."
            )
            continue
        if ident.startswith("mcp:") and "/" in ident:
            # Per-tool MCP narrowing would require mutating the SHARED MCPTool
            # instance's allowed_tools (clobbering the registry agents). Phase 1
            # selects whole servers only; narrow via MCP Tool Management (CTR-0121).
            warnings.append(
                f"tools[{index}] {ident!r}: per-tool MCP selection is not supported for "
                "harness agents; select the whole server (mcp:<server>) and narrow via "
                "MCP Tool Management."
            )
            continue
        if ident not in ids:
            ids.append(ident)
    return ids


def map_document(
    text: str,
    *,
    agent_id: str,
    group_path: tuple[str, ...] = (),
    default_name: str | None = None,
) -> HarnessAgentSpec:
    """Parse + validate + map a harness YAML document to a spec (CTR-0192).

    Raises ``HarnessAgentError`` on malformed YAML / a non-Harness kind; records
    every other problem as a warning (ANY warning blocks run-target selection,
    UDR-0119 D8).
    """
    data = parse_yaml(text)

    kind = str(data.get("kind") or "").strip()
    if kind != HARNESS_KIND:
        raise HarnessAgentError(f"Expected kind: {HARNESS_KIND}, found {kind or '<missing>'!r}.")

    warnings: list[str] = [f"{key} ignored (not a harness schema field)." for key in data if key not in _KNOWN_TOP_KEYS]

    name = str(data.get("name") or "").strip()
    if not name:
        if default_name:
            name = default_name
        warnings.append("name is required.")
    display_name = str(data.get("displayName") or "").strip()
    description = str(data.get("description") or "")

    model_id, model_effort = _map_model(data, warnings)
    harness_instructions, agent_instructions = _map_instructions(data, warnings)
    tool_allowlist = _map_tools(data, warnings)

    compaction = _block(data, "compaction", warnings)
    max_window = _positive_int(compaction, "maxContextWindowTokens", "compaction", warnings)
    max_output = _positive_int(compaction, "maxOutputTokens", "compaction", warnings)
    # 0 < max_output_tokens < max_context_window_tokens (UDR-0119 D9). When the
    # window is inherited from the catalog the loader re-checks against the
    # resolved value; here we can only check an explicit pair.
    if max_output is not None and max_window is not None and max_output >= max_window:
        warnings.append("compaction.maxOutputTokens must be smaller than compaction.maxContextWindowTokens.")

    todo = _block(data, "todo", warnings)
    mode = _block(data, "mode", warnings)
    mode_initial_raw = mode.get("initial")
    mode_initial: str | None = None
    if mode_initial_raw is not None:
        mode_initial = str(mode_initial_raw).strip().lower()
        if mode_initial not in ALLOWED_INITIAL_MODES:
            warnings.append(f"mode.initial must be one of: {', '.join(ALLOWED_INITIAL_MODES)}.")
            mode_initial = None
    # Plan approval (PRP-0181, UDR-0163 D2): absent => skip; an invalid value is a
    # visible warning (UDR-0119 D8, like mode.initial) and falls back to the default.
    plan_approval = DEFAULT_PLAN_APPROVAL
    plan_approval_raw = mode.get("planApproval")
    if plan_approval_raw is not None:
        candidate = str(plan_approval_raw).strip().lower()
        if candidate in ALLOWED_PLAN_APPROVALS:
            plan_approval = candidate
        else:
            warnings.append(f"mode.planApproval must be one of: {', '.join(ALLOWED_PLAN_APPROVALS)}.")

    file_memory = _block(data, "fileMemory", warnings)
    file_access = _block(data, "fileAccess", warnings)
    web_search = _block(data, "webSearch", warnings)
    # `fileAccess.disableWriteToolApproval` is no longer read (PRP-0179, UDR-0161 D6):
    # no harness tool asks for approval, so the key asks for what the harness already
    # does. It is ignored WITHOUT a warning -- a warning would make the agent
    # non-runnable (UDR-0119 D8) for a key that is harmless.
    # `toolApproval` was removed from the schema in v0.128.0 (UDR-0119 D6); a stale
    # block is still reported rather than silently ignored. Since PRP-0179 no harness
    # tool asks for approval at all (UDR-0161 D1).
    if "toolApproval" in data:
        warnings.append(
            "toolApproval is no longer a harness field: harness tools run without an approval step. Remove the block."
        )

    loop = _block(data, "loop", warnings)
    loop_max = _positive_int(loop, "maxIterations", "loop", warnings)
    if loop_max is not None and loop_max > HARNESS_MAX_ITERATIONS:
        warnings.append(
            f"loop.maxIterations must not exceed {HARNESS_MAX_ITERATIONS} (the MAF harness cap, UDR-0119 D4)."
        )
        loop_max = None

    return HarnessAgentSpec(
        id=agent_id,
        name=name or agent_id,
        display_name=display_name,
        description=description,
        group_path=group_path,
        model_id=model_id,
        effort=model_effort,
        harness_instructions=harness_instructions,
        agent_instructions=agent_instructions,
        tool_allowlist=tool_allowlist,
        compaction_disabled=_bool(compaction, "disabled", "compaction", warnings),
        max_context_window_tokens=max_window,
        max_output_tokens=max_output,
        todo_disabled=_bool(todo, "disabled", "todo", warnings),
        mode_disabled=_bool(mode, "disabled", "mode", warnings),
        mode_initial=mode_initial,
        mode_plan_approval=plan_approval,
        file_memory_disabled=_bool(file_memory, "disabled", "fileMemory", warnings),
        file_access_disable_write_tools=_bool(file_access, "disableWriteTools", "fileAccess", warnings),
        web_search_disabled=_bool(web_search, "disabled", "webSearch", warnings),
        loop_max_iterations=loop_max,
        warnings=warnings,
    )


__all__ = [
    "CODING_TOOL_IDS",
    "HARNESS_MAX_ITERATIONS",
    "effective_loop_max_iterations",
    "map_document",
    "parse_yaml",
]
