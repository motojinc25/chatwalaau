"""Harness agent discovery + inventory (CTR-0192, PRP-0135, UDR-0119).

Discovers ``kind: Harness`` YAML from ``DECLARATIVE_AGENTS_DIR`` -- the same tree
as Prompt agents and Workflows, dispatched by top-level ``kind`` through the
single ``read_top_kind()`` seam (UDR-0119 D1) -- via the loader's realpath jail
(the CTR-0031 technique). Maps each file to a :class:`HarnessAgentSpec` and
annotates live-state warnings (unknown offering, unrecognized tool identifier).
ANY warning blocks run-target selection (UDR-0119 D8).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app import providers
from app.agent.declarative.loader import _agents_dir, _jail_ok, read_top_kind
from app.agent.harness.mapping import HARNESS_MAX_ITERATIONS, map_document
from app.agent.harness.spec import HARNESS_KIND, HarnessAgentError, HarnessAgentSpec
from app.core.config import settings

logger = logging.getLogger(__name__)


def discover_files() -> list[tuple[str, Path, tuple[str, ...]]]:
    """Discover ``kind: Harness`` YAML files as (agent_id, path, group_path).

    ``agent_id`` is the POSIX relative path without extension (the declarative
    loader convention); other kinds are skipped (they belong to CTR-0142 /
    CTR-0180). Sorted for stable ordering.
    """
    root = _agents_dir()
    if root is None:
        return []
    out: list[tuple[str, Path, tuple[str, ...]]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".yaml", ".yml"):
            continue
        if not _jail_ok(root, path):
            logger.warning("Harness agent file outside jail ignored: %s", path)
            continue
        if read_top_kind(path) != HARNESS_KIND:
            continue
        rel = path.relative_to(root)
        parts = rel.with_suffix("").parts
        out.append(("/".join(parts), path, tuple(parts[:-1])))
    return out


def _annotate_model_warnings(spec: HarnessAgentSpec) -> HarnessAgentSpec:
    """Blocking warning when ``model.id`` is not a configured chat offering.

    Unlike the Prompt lane's preferred-default hint (UDR-0072 D4), a harness
    agent BINDS its one client to this offering (UDR-0119 D2), so an unknown id
    can never be run and must block selection.
    """
    if not spec.model_id:
        return spec
    from app.demo import is_demo_mode

    if is_demo_mode():
        return spec
    configured = {model for model, _ in providers.resolve_models()}
    if spec.model_id not in configured:
        spec.warnings.append(
            f"model.id {spec.model_id!r} is not a configured offering. Add it in "
            "model_offerings.jsonc (or via the Model Settings screen) to run this agent."
        )
    return spec


def _annotate_tool_warnings(spec: HarnessAgentSpec) -> HarnessAgentSpec:
    """Blocking warning for an allow-list id not recognized by live state.

    Mirrors the Prompt lane (UDR-0100 D3): the mapping records structural
    problems; here each identifier is checked against the CTR-0178 inventory
    (built-in function tools + MCP). Best-effort: an inventory read failure
    leaves the spec unchanged.
    """
    if not spec.tool_allowlist:
        return spec
    try:
        from app.agent.declarative.tool_inventory import known_tool_ids

        known = known_tool_ids()
    except Exception:  # never break resolution on an inventory read problem
        logger.debug("Tool inventory read failed during harness allow-list validation", exc_info=True)
        return spec
    unknown = [i for i in spec.tool_allowlist if i not in known]
    if unknown:
        spec.warnings.append(
            f"tool(s) {unknown} are not recognized (check the tool name / MCP server "
            "against the tool inventory). Fix or remove them to run this agent."
        )
    return spec


def _annotate_budget_warnings(spec: HarnessAgentSpec) -> HarnessAgentSpec:
    """Re-check ``maxOutputTokens`` against the RESOLVED context window (D9).

    The mapping can only compare an explicit pair; when the window is inherited
    from the catalog (``max_context_window_tokens is None``) the check runs here
    against ``catalog_context_window`` via the providers seam.
    """
    if spec.max_output_tokens is None or spec.max_context_window_tokens is not None:
        return spec
    if not spec.model_id:
        return spec
    resolved_window = providers.get_max_context_tokens(spec.model_id)
    if spec.max_output_tokens >= resolved_window:
        spec.warnings.append(
            f"compaction.maxOutputTokens ({spec.max_output_tokens}) must be smaller than the "
            f"offering's context window ({resolved_window})."
        )
    return spec


def resolve_spec(agent_id: str) -> HarnessAgentSpec:
    """Resolve a harness agent id to a mapped spec, raising on parse failure."""
    for cid, path, group_path in discover_files():
        if cid == agent_id:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise HarnessAgentError(f"Could not read {agent_id}: {exc}") from exc
            spec = map_document(text, agent_id=cid, group_path=group_path, default_name=path.stem)
            _annotate_model_warnings(spec)
            _annotate_tool_warnings(spec)
            _annotate_budget_warnings(spec)
            return spec
    raise HarnessAgentError(f"Unknown harness agent id: {agent_id!r}")


def policy_summary(spec: HarnessAgentSpec) -> dict:
    """Resolved-policy summary for the inventory / detail views (CTR-0194).

    Read-only; surfaces the effective gates and workspace capabilities (UDR-0119
    D5/D7) so the operator sees what a run would actually wire up. The web-search
    gate is INFORMATIONAL here -- a withheld offering forces
    ``disable_web_search`` at build time (forced-safe, never a blocking warning).
    """
    workspace = (settings.coding_workspace_dir or "").strip()
    skills_dir = (settings.skills_dir or "").strip()
    web_search: str = "disabled" if spec.web_search_disabled else "enabled"
    if not spec.web_search_disabled and spec.model_id:
        try:
            if providers.web_search_tool(spec.model_id) is None:
                web_search = "withheld"  # per-offering gate (UDR-0119 D5)
        except Exception:
            logger.debug("Web search gate probe failed for %s", spec.model_id, exc_info=True)
    return {
        "model": spec.model_id,
        "web_search": web_search,
        "file_memory": bool(workspace) and not spec.file_memory_disabled,
        "file_access": bool(workspace),
        "shell": bool(workspace),
        "skills": bool(skills_dir and Path(skills_dir).is_dir()),
        "todo": not spec.todo_disabled,
        "mode": not spec.mode_disabled,
        "mode_initial": spec.mode_initial,
        "compaction": not spec.compaction_disabled,
        "loop_max_iterations": min(spec.loop_max_iterations or HARNESS_MAX_ITERATIONS, HARNESS_MAX_ITERATIONS),
        "write_tool_approval": not spec.file_access_disable_write_tool_approval,
    }


def load_inventory() -> dict:
    """Return the harness management inventory (CTR-0194). Never raises.

    Every entry is mapped so a broken YAML is flagged (``loaded=false`` +
    ``error``) before the operator tries to select it. ``runnable`` is false on
    ANY warning (UDR-0119 D8) and always false under DEMO_MODE (D7).
    """
    from app.demo import is_demo_mode

    demo = is_demo_mode()
    agents: list[dict] = []
    for cid, path, group_path in discover_files():
        entry: dict = {
            "id": cid,
            "name": path.stem,
            "display_name": "",
            "description": "",
            "group_path": list(group_path),
            "kind": HARNESS_KIND,
            "loaded": False,
            "error": None,
            "warnings": [],
            "runnable": False,
            "editable": True,
            "policy": None,
        }
        try:
            text = path.read_text(encoding="utf-8")
            spec = map_document(text, agent_id=cid, group_path=group_path, default_name=path.stem)
            _annotate_model_warnings(spec)
            _annotate_tool_warnings(spec)
            _annotate_budget_warnings(spec)
            entry["name"] = spec.name
            entry["display_name"] = spec.display_name
            entry["description"] = spec.description
            entry["loaded"] = True
            entry["warnings"] = spec.warnings
            entry["runnable"] = not spec.warnings and not demo
            entry["policy"] = policy_summary(spec)
        except HarnessAgentError as exc:
            entry["error"] = str(exc)
        except OSError as exc:
            entry["error"] = f"Could not read file: {exc}"
        agents.append(entry)
    return {"harness_dir": str(_agents_dir() or ""), "demo_mode": demo, "agents": agents}


__all__ = [
    "discover_files",
    "load_inventory",
    "policy_summary",
    "resolve_spec",
]
