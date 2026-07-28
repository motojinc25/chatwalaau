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

from copy import deepcopy
import logging
from typing import TYPE_CHECKING, Any

import yaml

from app.agent.declarative.loader import _agents_dir, _jail_ok, read_top_kind
from app.core.config import settings
from app.workflow.spec import WORKFLOW_KIND, WorkflowError, WorkflowSpec

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# The full mapped action surface (PRP-0121, UDR-0104 D1 -- supersedes the UDR-0101 D6
# curated subset). An action OUTSIDE this set is still a blocking warning (the D6
# mechanism, preserved). The three boundary-crossing kinds (InvokeFunctionTool /
# InvokeMcpTool / HttpRequestAction) compile via ChatWalaʻau-owned jailed handlers
# (CTR-0186) and are additionally a blocking warning when their opt-in class is OFF
# (handlers.boundary_action_warnings). Keep in sync with the authoring palette (CTR-0184).
ALLOWED_ACTION_KINDS = frozenset(
    {
        # Variable
        "SetVariable",
        "SetMultipleVariables",
        "SetTextVariable",
        "SetValue",  # alias retained
        "ResetVariable",
        "ClearAllVariables",
        "ParseValue",
        "EditTableV2",
        # Control Flow
        "If",
        "ConditionGroup",
        "Foreach",
        "BreakLoop",
        "ContinueLoop",
        "GotoAction",
        "Join",
        # Output
        "SendActivity",
        # Agent
        "InvokeAzureAgent",
        # Tool + HTTP (jailed + opt-in, CTR-0186 / UDR-0104 D2)
        "InvokeFunctionTool",
        "InvokeMcpTool",
        "HttpRequestAction",
        # Human-in-the-Loop
        "Question",
        "RequestExternalInput",
        # Workflow Control
        "EndWorkflow",
        "EndConversation",
        "CreateConversation",
        "AddConversationMessage",
    }
)

# Nesting keys under which child actions live (walked to collect kinds / agent refs).
_NESTED_ACTION_KEYS = ("actions", "then", "else", "elseActions")

# The MAF builder requires ``serverUrl`` on an InvokeMcpTool action at COMPILE time,
# but the jail (CTR-0186) deliberately keeps raw serverUrl out of the authored surface
# and re-points by ``serverLabel`` at invocation. So the backend injects this sentinel
# for any InvokeMcpTool that omits serverUrl -- the jailed handler ALWAYS overrides it
# with the configured server's real URL, so the sentinel is never dialed (UDR-0104 D2).
_MCP_SENTINEL_URL = "https://workflow-mcp.local/"


# ---------------------------------------------------------------------------
# Action field normalization (PRP-0122, UDR-0105 D3/D6)
# ---------------------------------------------------------------------------
# The INSTALLED agent-framework-declarative executor is the field contract (UDR-0105
# D1): the authoring layer must emit the keys the executor actually reads. This module
# owns the ONE normalizer that (a) migrates superseded authoring keys forward and (b)
# normalizes a namespace-less variable path to ``Local.``. It is invoked from exactly
# three places -- the compile preparation hook below, the canonical serializer, and the
# read-side document reshaping (both in app.workflow.authoring) -- so a workflow behaves
# identically whether it was authored in the GUI, hand-written, or upgraded in place
# without ever being opened.

# Namespaces the MAF state layer recognizes (_state.py). An unrecognized prefix becomes
# a CUSTOM namespace, which is legitimate and MUST NOT be rewritten (UDR-0105 D4).
_WORKFLOW_SUB_NAMESPACES = ("Inputs", "Outputs")

# Per-kind fields whose value is a state path WRITTEN to by the action. Only write
# destinations are normalized; a read-side ``conversationId`` (InvokeAzureAgent,
# InvokeMcpTool, HttpRequestAction) is an expression, not a destination.
_WRITE_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "SetVariable": ("variable",),
    "SetValue": ("variable", "path"),
    "SetTextVariable": ("variable",),
    "ResetVariable": ("variable",),
    "ParseValue": ("variable",),
    "EditTableV2": ("table", "variable"),
    "Question": ("variable",),
    "RequestExternalInput": ("variable",),
    "CreateConversation": ("conversationId",),
    "HttpRequestAction": ("response", "responseHeaders"),
}

# Destinations nested under an action's ``output`` mapping (agent / tool invocations).
_OUTPUT_WRITE_KEYS = ("responseObject", "messages", "result")

# Foreach loop-variable NAMES. MAF prefixes these with ``Local.`` itself
# (_executors_control_flow.py:179), so a stored ``Local.x`` would become
# ``Local.Local.x``; normalization STRIPS the prefix here rather than adding one.
_LOOP_NAME_FIELDS = ("itemName", "indexName")


def _normalize_write_path(value: Any) -> Any:
    """Return a write path with a namespace: a bare name gains ``Local.``.

    An expression (``=...``), a non-string, an empty string, and an already-namespaced
    path (anything containing a dot, INCLUDING a custom namespace -- UDR-0105 D4) are
    returned unchanged.
    """
    if not isinstance(value, str):
        return value
    path = value.strip()
    if not path or path.startswith("=") or "." in path:
        return value
    return f"Local.{path}"


def _strip_local_prefix(value: Any) -> Any:
    """Return a Foreach loop-variable NAME without a redundant ``Local.`` prefix."""
    if not isinstance(value, str):
        return value
    name = value.strip()
    return name[len("Local.") :] if name.startswith("Local.") else value


def _migrate_legacy_keys(action: dict[str, Any]) -> None:
    """Migrate superseded authoring keys to the executor's own names (UDR-0105 D6).

    Each migration applies ONLY when the modern key is absent, so a correct action is
    never touched and repeated application is idempotent.
    """
    kind = str(action.get("kind") or "")
    if kind == "SetTextVariable":
        # The executor reads ``text`` (_executors_basic.py:136); the editor emitted ``value``.
        if "text" not in action and "value" in action:
            action["text"] = action.pop("value")
    elif kind == "SetMultipleVariables":
        # The executor reads an ``assignments`` LIST (_executors_basic.py:157); the editor
        # emitted a ``variables`` MAP.
        if "assignments" not in action and isinstance(action.get("variables"), dict):
            action["assignments"] = [{"variable": k, "value": v} for k, v in action.pop("variables").items()]
    elif kind == "ParseValue":
        # The executor reads ``value`` (_executors_basic.py:469); the editor emitted ``source``.
        if "value" not in action and "source" in action:
            action["value"] = action.pop("source")
    # The executor reads ``item`` / ``value`` plus ``key`` / ``index``
    # (_executors_basic.py:363); the editor emitted a ``row: {key, value}`` pair whose key
    # names the record field and whose value is that field's value.
    elif kind == "EditTableV2" and "item" not in action and isinstance(action.get("row"), dict):
        row = action.pop("row")
        field_name = row.get("key")
        field_value = row.get("value")
        if isinstance(field_name, str) and field_name.strip():
            action["item"] = {field_name: field_value}
            action.setdefault("key", field_name)
        elif field_value is not None:
            action["item"] = field_value


def normalize_workflow_actions(actions: Any) -> None:
    """Normalize an action tree IN PLACE (legacy keys + write-path namespaces).

    The single normalizer of UDR-0105 D3/D6. Idempotent: applying it twice yields the
    same tree, so it is safe on every compile, every save, and every read.
    """
    for action in _walk_actions(actions):
        _migrate_legacy_keys(action)
        kind = str(action.get("kind") or "")
        for name in _WRITE_PATH_FIELDS.get(kind, ()):
            if name in action:
                action[name] = _normalize_write_path(action[name])
        if kind == "SetMultipleVariables":
            for assignment in action.get("assignments") or []:
                if isinstance(assignment, dict) and "variable" in assignment:
                    assignment["variable"] = _normalize_write_path(assignment["variable"])
        if kind == "Foreach":
            for name in _LOOP_NAME_FIELDS:
                if name in action:
                    action[name] = _strip_local_prefix(action[name])
        if kind == "Question":
            # ``allowFreeText`` is ALWAYS stated in the YAML rather than left implicit,
            # so the authored document says outright whether a typed answer is accepted.
            # The value written matches the executor's own default (True).
            action.setdefault("allowFreeText", True)
        output = action.get("output")
        if isinstance(output, dict):
            for name in _OUTPUT_WRITE_KEYS:
                if name in output:
                    output[name] = _normalize_write_path(output[name])


def _write_path_warnings(actions: Any) -> list[str]:
    """Return a blocking warning per write path the MAF state layer will reject.

    Runs on the NORMALIZED tree, so it reports only what will actually reach the state
    layer at run time: an empty path, ``Workflow`` alone, the read-only
    ``Workflow.Inputs.*``, an unknown ``Workflow.<x>`` sub-namespace, and (defensively)
    a bare path that somehow escaped normalization. Each of these raises ValueError in
    ``_state.py`` mid-run today (UDR-0105 D5). A CUSTOM namespace is NOT reported
    (UDR-0105 D4).
    """
    out: list[str] = []

    def check(action_kind: str, field_name: str, value: Any) -> None:
        if not isinstance(value, str):
            return
        path = value.strip()
        where = f"{action_kind}.{field_name}"
        if not path:
            out.append(f"{where}: the variable path is empty; the action would do nothing.")
            return
        if path.startswith("="):
            return
        parts = path.split(".")
        if len(parts) == 1:
            out.append(
                f"{where}: {path!r} has no namespace; use 'Local.{path}' "
                "(the workflow runtime rejects a namespace-less path at run time)."
            )
            return
        if parts[0] != "Workflow":
            return
        if parts[1] == "Inputs":
            out.append(f"{where}: {path!r} is read-only; write to 'Local.*' or 'Workflow.Outputs.*' instead.")
        elif parts[1] not in _WORKFLOW_SUB_NAMESPACES:
            out.append(f"{where}: {path!r} is an unknown Workflow namespace; use 'Workflow.Outputs.*'.")

    for action in _walk_actions(actions):
        kind = str(action.get("kind") or "")
        for name in _WRITE_PATH_FIELDS.get(kind, ()):
            if name in action:
                check(kind, name, action[name])
        if kind == "SetMultipleVariables":
            for assignment in action.get("assignments") or []:
                if isinstance(assignment, dict):
                    check(kind, "assignments[].variable", assignment.get("variable"))
        output = action.get("output")
        if isinstance(output, dict):
            for name in _OUTPUT_WRITE_KEYS:
                if name in output:
                    check(kind, f"output.{name}", output[name])
    return out


def _prepare_compile_text(text: str) -> str:
    """Return YAML text ready for the MAF builder (normalize + MCP serverUrl sentinel).

    Applies the shared normalizer (UDR-0105 D3/D6) so an EXISTING file that is never
    re-saved still runs with the corrected field semantics, then injects the MCP
    serverUrl sentinel. Returns the original text byte-for-byte when neither step
    changed anything, so a already-canonical workflow compiles unchanged.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    if not isinstance(data, dict):
        return text
    before = deepcopy(data.get("actions"))
    normalize_workflow_actions(data.get("actions"))
    changed = data.get("actions") != before
    for action in _walk_actions(data.get("actions")):
        if str(action.get("kind") or "") == "InvokeMcpTool" and not action.get("serverUrl"):
            label = str(action.get("serverLabel") or "server").strip() or "server"
            action["serverUrl"] = _MCP_SENTINEL_URL + label
            changed = True
    if not changed:
        return text
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _max_iterations() -> int:
    """Clamp the operator's WORKFLOW_MAX_ITERATIONS to a sane bound (UDR-0101 D10)."""
    return max(1, min(int(settings.workflow_max_iterations or 100), 100_000))


def _new_factory(agents: dict[str, Any] | None = None):
    """Build a ``WorkflowFactory`` with the jailed boundary-crossing handlers (CTR-0186).

    The HTTP / MCP handlers and the function-tool registry are ALWAYS injected so a
    workflow that uses those actions COMPILES; the opt-in is enforced by (a) a blocking
    warning the mapper adds for any disabled-class action, and (b) each handler
    re-checking its flag at invocation time (UDR-0104 D2, defense in depth).
    """
    from agent_framework_declarative import WorkflowFactory

    from app.workflow import handlers

    factory = WorkflowFactory(
        agents=agents or {},
        max_iterations=_max_iterations(),
        http_request_handler=handlers.build_http_request_handler(),
        mcp_tool_handler=handlers.build_mcp_tool_handler(),
    )
    handlers.register_function_tools(factory)
    return factory


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
    """Return the agent name an InvokeAzureAgent action references (literal or expr).

    Mirrors the FULL MAF resolution order (_executors_agents.py:430-453): ``agent`` as a
    plain string, ``agent.name``, ``agent.agentName``, then ``agentName``. Missing
    ``agent.name`` here previously let a workflow authored in the documented
    ``agent: {name: X}`` shape pass validation with no warning and then fail at run time
    with "not found in registry" (PRP-0122 FACT 3).
    """
    agent = action.get("agent")
    candidates: list[Any] = []
    if isinstance(agent, str):
        candidates.append(agent)
    elif isinstance(agent, dict):
        candidates.extend((agent.get("name"), agent.get("agentName")))
    candidates.append(action.get("agentName"))
    for name in candidates:
        if isinstance(name, str) and name.strip():
            return name
    return None


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

    name = str(data.get("name") or data.get("displayName") or default_name or workflow_id).strip()
    display_name = str(data.get("displayName") or "").strip()
    description = str(data.get("description") or "").strip()

    # Normalize the parsed tree before inspecting it (PRP-0122, UDR-0105 D3/D6) so the
    # warnings below describe what will ACTUALLY run -- the same normalization
    # _prepare_compile_text applies to the text handed to the MAF builder.
    normalize_workflow_actions(data.get("actions"))

    referenced: list[str] = []
    dynamic: list[str] = []
    action_kinds: list[str] = []
    action_ids: list[str] = []
    node_labels: dict[str, str] = {}
    warnings: list[str] = []
    for action in _walk_actions(data.get("actions")):
        kind = str(action.get("kind") or "").strip()
        if kind:
            action_kinds.append(kind)
        aid = action.get("id")
        if isinstance(aid, str) and aid.strip():
            action_ids.append(aid.strip())
            label = action.get("displayName")
            if isinstance(label, str) and label.strip():
                node_labels[aid.strip()] = label.strip()
        if kind and kind not in ALLOWED_ACTION_KINDS:
            warnings.append(
                f"action kind {kind!r} is not supported; remove it or use a mapped action "
                "(Variable / control-flow / SendActivity / InvokeAzureAgent / tool / HTTP / "
                "HITL / workflow-control)."
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

    # A boundary-crossing action (InvokeFunctionTool / InvokeMcpTool / HttpRequestAction)
    # whose opt-in class is OFF is a blocking warning (UDR-0104 D2). The action still
    # COMPILES (the jailed handlers are injected below) so the operator sees a clean
    # warning rather than a hard compile error.
    from app.workflow.handlers import boundary_action_warnings

    warnings.extend(boundary_action_warnings(action_kinds))

    # A write path the MAF state layer is certain to reject mid-run (read-only
    # Workflow.Inputs, the Workflow root, an unknown Workflow sub-namespace, an empty
    # path) blocks activation instead of failing during the run (UDR-0105 D5).
    warnings.extend(_write_path_warnings(data.get("actions")))

    # Duplicate action ids break GotoAction targeting and are HARD-rejected by the MAF
    # builder; detect them first and raise a clean, friendly message instead of the raw
    # builder stack (v0.115.2).
    _dupe_ids = sorted({aid for aid in action_ids if action_ids.count(aid) > 1})
    if _dupe_ids:
        raise WorkflowError(f"Duplicate action id(s) {_dupe_ids}: each action id must be unique.")

    # Power Fx syntax check of every ``=`` expression (v0.115.2). Syntax-only, so a
    # reference to an undefined workflow variable is not a false positive; a no-op when
    # the .NET-backed Power Fx engine is unavailable.
    from app.workflow.powerfx import powerfx_warnings

    warnings.extend(powerfx_warnings(data.get("actions")))

    # Structural compile with NO agents: validates the graph + rejects unmapped actions
    # without building any LLM client. The jailed CTR-0186 handlers are injected so the
    # boundary-crossing actions compile; the runtime compile (compile_for_run) adds the
    # real node agents.
    try:
        _new_factory().create_workflow_from_yaml(_prepare_compile_text(text))
    except Exception as exc:  # DeclarativeWorkflowError / ValueError from the builder
        raise WorkflowError(f"Workflow could not be compiled: {exc}") from exc

    # Dedup preserving order.
    referenced = list(dict.fromkeys(referenced))
    action_kinds = list(dict.fromkeys(action_kinds))
    return WorkflowSpec(
        id=workflow_id,
        name=name or workflow_id,
        display_name=display_name,
        description=description,
        source=source,
        group_path=group_path,
        referenced_agents=referenced,
        dynamic_agent_refs=dynamic,
        action_kinds=action_kinds,
        node_labels=node_labels,
        warnings=warnings,
    )


def node_labels_for(workflow_id: str) -> dict[str, str]:
    """Return ``action id -> displayName`` for a workflow (progress labels, UDR-0105 D7).

    A parse-only read (no compile, no agent build) used by the run lanes to label
    progress events in the author's own words. Never raises: an unreadable or malformed
    file simply yields no labels and the runtime falls back to the action id.
    """
    for wid, path, _gp in _discover_workflow_files():
        if wid != workflow_id:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}
        if not isinstance(data, dict):
            return {}
        labels: dict[str, str] = {}
        for action in _walk_actions(data.get("actions")):
            aid = action.get("id")
            label = action.get("displayName")
            if isinstance(aid, str) and aid.strip() and isinstance(label, str) and label.strip():
                labels[aid.strip()] = label.strip()
        return labels
    return {}


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
            "display_name": "",
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
                display_name=spec.display_name,
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
        raise WorkflowError("Workflow cannot run until its warnings are resolved: " + "; ".join(spec.warnings))

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

    try:
        return _new_factory(agents=agents).create_workflow_from_yaml(_prepare_compile_text(text))
    except Exception as exc:
        raise WorkflowError(f"Workflow could not be compiled: {exc}") from exc


__all__ = [
    "ALLOWED_ACTION_KINDS",
    "all_yaml_stems",
    "annotate_agent_ref_warnings",
    "compile_for_run",
    "load_workflow_inventory",
    "map_workflow_document",
    "node_labels_for",
    "normalize_workflow_actions",
    "resolve_workflow",
    "validate_workflow_text",
]
