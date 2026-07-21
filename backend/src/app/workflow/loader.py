"""Declarative workflow discovery + mapping + compile (CTR-0180, PRP-0118, UDR-0101).

Discovers ``kind: Workflow`` YAML from ``DECLARATIVE_AGENTS_DIR`` (the same tree as
agents, dispatched by ``kind`` -- UDR-0101 D2) through the loader's realpath jail,
and compiles a workflow to a MAF ``Workflow`` via
``WorkflowFactory.create_workflow_from_yaml`` (the parse / validate / compile front
end -- UDR-0101 D1). Agent nodes are resolved to ChatWalaʻau-built declarative
``kind: Prompt`` agents (build_prompt_agent) and injected as ``agents={name:
instance}`` (UDR-0101 D4); an unresolved reference or an unmapped action is a
blocking warning (UDR-0101 D6).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

from app.agent.declarative.loader import _agents_dir, _jail_ok, read_top_kind
from app.core.config import settings
from app.workflow.spec import WORKFLOW_KIND, WorkflowError, WorkflowSpec

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# The curated action subset ChatWalaʻau maps for the first release (UDR-0101 D6).
# An action outside this set is a blocking warning (raw HttpRequest / InvokeMcpTool
# also fail the MAF build because no handler is supplied). Keep in sync with the
# authoring palette (CTR-0184).
ALLOWED_ACTION_KINDS = frozenset(
    {
        "SendActivity",
        "SetValue",
        "SetVariable",
        "SetTextVariable",
        "SetMultipleVariables",
        "ResetVariable",
        "ClearAllVariables",
        "CreateConversation",
        "InvokeAzureAgent",
        "If",
        "ConditionGroup",
        "Foreach",
        "BreakLoop",
        "ContinueLoop",
        "GotoAction",
        "Join",
        "EndWorkflow",
        "EndConversation",
    }
)

# Nesting keys under which child actions live (walked to collect kinds / agent refs).
_NESTED_ACTION_KEYS = ("actions", "then", "else", "elseActions")


def _max_iterations() -> int:
    """Clamp the operator's WORKFLOW_MAX_ITERATIONS to a sane bound (UDR-0101 D10)."""
    return max(1, min(int(settings.workflow_max_iterations or 100), 100_000))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_workflow_files() -> list[tuple[str, Path, tuple[str, ...]]]:
    """Discover ``kind: Workflow`` YAML as (workflow_id, path, group_path)."""
    root = _agents_dir()
    if root is None:
        return []
    out: list[tuple[str, Path, tuple[str, ...]]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".yaml", ".yml"):
            continue
        if not _jail_ok(root, path):
            continue
        if read_top_kind(path) != WORKFLOW_KIND:
            continue
        rel = path.relative_to(root)
        parts = rel.with_suffix("").parts
        out.append(("/".join(parts), path, tuple(parts[:-1])))
    return out


def all_yaml_stems() -> set[str]:
    """Return every discovered *.yaml/.yml id (any kind) for collision checks."""
    root = _agents_dir()
    if root is None:
        return set()
    stems: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".yaml", ".yml") and _jail_ok(root, path):
            stems.add("/".join(path.relative_to(root).with_suffix("").parts))
    return stems


# ---------------------------------------------------------------------------
# Action walk (referenced agents + action kinds)
# ---------------------------------------------------------------------------
def _walk_actions(actions: Any):
    """Yield every action dict in a (possibly nested) actions list."""
    if not isinstance(actions, list):
        return
    for action in actions:
        if not isinstance(action, dict):
            continue
        yield action
        for key in _NESTED_ACTION_KEYS:
            yield from _walk_actions(action.get(key))
        # ConditionGroup: each condition carries its own actions branch.
        for cond in action.get("conditions", []) if isinstance(action.get("conditions"), list) else []:
            if isinstance(cond, dict):
                yield from _walk_actions(cond.get("actions"))


def _agent_ref(action: dict[str, Any]) -> str | None:
    """Return the agentName an InvokeAzureAgent action references (literal or expr)."""
    name = action.get("agentName")
    if not isinstance(name, str):
        agent = action.get("agent")
        if isinstance(agent, dict):
            name = agent.get("agentName")
    return name if isinstance(name, str) and name.strip() else None


# ---------------------------------------------------------------------------
# Mapping (structural validate, no agent build)
# ---------------------------------------------------------------------------
def map_workflow_document(
    text: str,
    *,
    workflow_id: str,
    source: str = "custom",
    group_path: tuple[str, ...] = (),
    default_name: str | None = None,
) -> WorkflowSpec:
    """Parse + structurally validate a workflow YAML into a WorkflowSpec.

    Raises ``WorkflowError`` on a parse failure, a wrong ``kind``, or a MAF build
    failure (an unmapped action such as raw HttpRequest / InvokeMcpTool -- no handler
    is supplied, UDR-0101 D6). Otherwise returns a spec whose ``warnings`` list is
    populated with unmapped-kind and reference notes; ANY warning blocks activation
    (UDR-0101 D4/D6). Agent references are validated against the Prompt inventory
    separately (``annotate_agent_ref_warnings``) so this stays build-free.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"Invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError("Workflow document must be a YAML mapping.")
    if str(data.get("kind") or "").strip() != WORKFLOW_KIND:
        raise WorkflowError("Workflow document must set 'kind: Workflow'.")

    name = str(data.get("name") or default_name or workflow_id).strip()
    description = str(data.get("description") or "").strip()

    referenced: list[str] = []
    dynamic: list[str] = []
    action_kinds: list[str] = []
    warnings: list[str] = []
    for action in _walk_actions(data.get("actions")):
        kind = str(action.get("kind") or "").strip()
        if kind:
            action_kinds.append(kind)
        if kind and kind not in ALLOWED_ACTION_KINDS:
            warnings.append(
                f"action kind {kind!r} is not supported yet; remove it or use a mapped "
                "action (SendActivity / SetValue / InvokeAzureAgent / If / ConditionGroup "
                "/ Foreach / loop control / EndWorkflow)."
            )
        if kind == "InvokeAzureAgent":
            ref = _agent_ref(action)
            if ref is None:
                warnings.append("an InvokeAzureAgent action is missing an agentName.")
            elif ref.startswith("="):
                dynamic.append(ref)
                warnings.append(
                    f"agent reference {ref!r} is a dynamic expression and cannot be "
                    "statically resolved to a Prompt agent; use a literal agent name."
                )
            else:
                referenced.append(ref)

    # Structural compile with NO agents: validates the graph + rejects unmapped
    # actions (HttpRequest / InvokeMcpTool raise without a handler) without building
    # any LLM client. The runtime compile (compile_for_run) injects real agents.
    try:
        from agent_framework_declarative import WorkflowFactory

        WorkflowFactory(max_iterations=_max_iterations()).create_workflow_from_yaml(text)
    except Exception as exc:  # DeclarativeWorkflowError / ValueError from the builder
        raise WorkflowError(f"Workflow could not be compiled: {exc}") from exc

    # Dedup preserving order.
    referenced = list(dict.fromkeys(referenced))
    action_kinds = list(dict.fromkeys(action_kinds))
    return WorkflowSpec(
        id=workflow_id,
        name=name or workflow_id,
        description=description,
        source=source,
        group_path=group_path,
        referenced_agents=referenced,
        dynamic_agent_refs=dynamic,
        action_kinds=action_kinds,
        warnings=warnings,
    )


def _prompt_agent_lookup() -> dict[str, str]:
    """Map both id and name of every declarative Prompt agent to its id (for refs)."""
    from app.agent.declarative.loader import load_inventory
    from app.agent.declarative.store import get_active_store

    lookup: dict[str, str] = {}
    inv = load_inventory(get_active_store().active_id())
    for entry in inv.get("agents", []):
        aid = entry.get("id")
        if not aid:
            continue
        lookup[aid] = aid
        nm = entry.get("name")
        if isinstance(nm, str) and nm.strip():
            lookup.setdefault(nm, aid)
    return lookup


def annotate_agent_ref_warnings(spec: WorkflowSpec) -> WorkflowSpec:
    """Append a warning for any referenced agent that is not a known Prompt agent.

    UDR-0101 D4: an agent reference resolves ONLY to a declarative Prompt agent
    (CORE included). An unknown name blocks activation.
    """
    if not spec.referenced_agents:
        return spec
    try:
        lookup = _prompt_agent_lookup()
    except Exception:  # never break resolution on an inventory read problem
        logger.debug("Prompt inventory read failed during workflow ref validation", exc_info=True)
        return spec
    unknown = [r for r in spec.referenced_agents if r not in lookup]
    if unknown:
        spec.warnings.append(
            f"referenced agent(s) {unknown} are not declarative Prompt agents; a workflow "
            "may only invoke Prompt agents (check the name against the Agents inventory)."
        )
    return spec


# ---------------------------------------------------------------------------
# Resolve / inventory / validate
# ---------------------------------------------------------------------------
def resolve_workflow(workflow_id: str) -> WorkflowSpec:
    """Resolve a workflow id to a mapped + reference-validated spec (raises on failure)."""
    for wid, path, group_path in _discover_workflow_files():
        if wid == workflow_id:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise WorkflowError(f"Could not read {workflow_id}: {exc}") from exc
            spec = map_workflow_document(
                text, workflow_id=wid, source="custom", group_path=group_path, default_name=path.stem
            )
            annotate_agent_ref_warnings(spec)
            return spec
    raise WorkflowError(f"Unknown workflow id: {workflow_id!r}")


def load_workflow_inventory() -> dict:
    """Return the workflow inventory (CTR-0182): custom tree with loaded/error state."""
    workflows: list[dict] = []
    for wid, path, group_path in _discover_workflow_files():
        entry: dict = {
            "id": wid,
            "name": path.stem,
            "description": "",
            "group_path": list(group_path),
            "source": "custom",
            "loaded": False,
            "error": None,
            "warnings": [],
            "referenced_agents": [],
            "action_kinds": [],
            "editable": True,
        }
        try:
            text = path.read_text(encoding="utf-8")
            spec = map_workflow_document(
                text, workflow_id=wid, source="custom", group_path=group_path, default_name=path.stem
            )
            annotate_agent_ref_warnings(spec)
            entry.update(
                name=spec.name,
                description=spec.description,
                loaded=True,
                warnings=spec.warnings,
                referenced_agents=spec.referenced_agents,
                action_kinds=spec.action_kinds,
            )
        except WorkflowError as exc:
            entry["error"] = str(exc)
        except OSError as exc:
            entry["error"] = f"Could not read file: {exc}"
        workflows.append(entry)
    return {"workflows_dir": str(_agents_dir() or ""), "workflows": workflows}


def validate_workflow_text(text: str) -> dict:
    """Dry-run: map + reference-validate a workflow document (no persist, no build)."""
    try:
        spec = map_workflow_document(text, workflow_id="_preview", source="custom")
    except WorkflowError as exc:
        return {"valid": False, "error": str(exc), "warnings": []}
    annotate_agent_ref_warnings(spec)
    return {
        "valid": True,
        "error": None,
        "warnings": spec.warnings,
        "summary": {
            "name": spec.name,
            "description": spec.description,
            "referenced_agents": spec.referenced_agents,
            "action_kinds": spec.action_kinds,
        },
    }


# ---------------------------------------------------------------------------
# Compile for run (injects real Prompt agents)
# ---------------------------------------------------------------------------
def compile_for_run(workflow_id: str):
    """Compile the workflow to a runnable MAF ``Workflow`` with real node agents.

    Resolves every referenced Prompt agent to a ChatWalaʻau-built ``Agent``
    (build_prompt_agent) and injects them as ``agents={name: instance}`` so
    InvokeAzureAgent actions run the right agent (UDR-0101 D4). Raises
    ``WorkflowError`` if the spec has blocking warnings or a node agent cannot be
    built.
    """
    spec = resolve_workflow(workflow_id)
    if spec.warnings:
        raise WorkflowError(
            "Workflow cannot run until its warnings are resolved: " + "; ".join(spec.warnings)
        )

    from app.agent.declarative.spec import DeclarativeAgentError
    from app.workflow.builder import build_prompt_agent

    lookup = _prompt_agent_lookup()
    agents: dict[str, Any] = {}
    for ref in spec.referenced_agents:
        agent_id = lookup.get(ref)
        if agent_id is None:
            raise WorkflowError(f"Referenced agent {ref!r} is not a known Prompt agent.")
        try:
            agents[ref] = build_prompt_agent(agent_id)
        except DeclarativeAgentError as exc:
            raise WorkflowError(f"Could not build agent {ref!r}: {exc}") from exc

    # Re-read the source for the compile.
    for wid, path, _gp in _discover_workflow_files():
        if wid == workflow_id:
            text = path.read_text(encoding="utf-8")
            break
    else:
        raise WorkflowError(f"Unknown workflow id: {workflow_id!r}")

    from agent_framework_declarative import WorkflowFactory

    try:
        factory = WorkflowFactory(agents=agents, max_iterations=_max_iterations())
        return factory.create_workflow_from_yaml(text)
    except Exception as exc:
        raise WorkflowError(f"Workflow could not be compiled: {exc}") from exc


__all__ = [
    "ALLOWED_ACTION_KINDS",
    "all_yaml_stems",
    "annotate_agent_ref_warnings",
    "compile_for_run",
    "load_workflow_inventory",
    "map_workflow_document",
    "resolve_workflow",
    "validate_workflow_text",
]
