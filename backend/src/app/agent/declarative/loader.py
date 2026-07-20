"""Declarative agent discovery + inventory + CORE (CTR-0142, PRP-0094, UDR-0072).

The bundled CORE agent reproduces current ChatWalaʻau behavior (all-``None`` spec)
and is always present and active by default (UDR-0072 D7/D13). Custom agents are
discovered from ``DECLARATIVE_AGENTS_DIR`` (nested folders allowed) through a
realpath jail rooted at that directory (the CTR-0031 technique, a different root;
UDR-0072 D11). Only ``*.yaml`` / ``*.yml`` are loaded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app import providers
from app.agent.declarative.mapping import map_document
from app.agent.declarative.spec import (
    CORE_AGENT_ID,
    DeclarativeAgentError,
    DeclarativeAgentSpec,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

# Bundled CORE declarative agent (UDR-0072 D2/D13). ``instructions: "=Identity"``
# is the sentinel that maps to the runtime Global Agent Identity, so an
# operator-edited .agent/IDENTITY.md still applies; no model / options means the
# spec is all-``None`` and the registry build is byte-for-byte the imperative path.
CORE_AGENT_YAML = """kind: Prompt
name: ChatWalaʻau Core
description: >-
  The default ChatWalaʻau agent. Persona from the Global Agent Identity
  (.agent/IDENTITY.md), all configured models, and every enabled capability /
  tool. This is the built-in agent; it is always available and active by default.
instructions: "=Identity"
"""


def core_spec() -> DeclarativeAgentSpec:
    """Return the CORE spec (always mappable; reproduces current behavior)."""
    return map_document(
        CORE_AGENT_YAML,
        agent_id=CORE_AGENT_ID,
        source="core",
        default_name="ChatWalaʻau Core",
    )


def _agents_dir() -> Path | None:
    """Return the configured custom-agents directory, or None when unset/missing."""
    raw = (settings.declarative_agents_dir or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    return path


def _jail_ok(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves inside ``root`` (symlink-escape safe)."""
    try:
        root_real = os.path.realpath(root)
        cand_real = os.path.realpath(candidate)
    except OSError:
        return False
    return cand_real == root_real or cand_real.startswith(root_real + os.sep)


def _discover_files() -> list[tuple[str, Path, tuple[str, ...]]]:
    """Discover custom YAML files as (agent_id, path, group_path).

    ``agent_id`` is the POSIX relative path without extension (e.g. "support/triage");
    ``group_path`` is the tuple of parent folders. Sorted for stable ordering.
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
            logger.warning("Declarative agent file outside jail ignored: %s", path)
            continue
        rel = path.relative_to(root)
        parts = rel.with_suffix("").parts
        agent_id = "/".join(parts)
        group_path = tuple(parts[:-1])
        out.append((agent_id, path, group_path))
    return out


def _annotate_model_warnings(spec: DeclarativeAgentSpec) -> DeclarativeAgentSpec:
    """Append a non-fatal warning when a model.id is not configured (UDR-0072 D4).

    model.id is a preferred-default hint, not a hard filter, so an unconfigured model
    is never fatal; it is surfaced as a visible warning instead. Skipped in DEMO_MODE
    (the demo model set is fixed).
    """
    if not spec.model_filter:
        return spec
    from app.demo import is_demo_mode

    if is_demo_mode():
        return spec
    configured = {model for model, _ in providers.resolve_models()}
    missing = [m for m in spec.model_filter if m not in configured]
    if missing:
        spec.warnings.append(
            f"model id(s) {missing} are not configured; the agent will use the default "
            "model. Add them as offerings in model_offerings.jsonc (or via `chatwalaau "
            "models add` / the Model Settings screen) to make them selectable."
        )
    return spec


def _annotate_tool_warnings(spec: DeclarativeAgentSpec) -> DeclarativeAgentSpec:
    """Append a warning for any tool_allowlist id that is not recognized (UDR-0100 D3).

    Mirrors ``_annotate_model_warnings``: the mapping records structural problems, and
    here we validate each identifier against live state (known built-in / connected or
    configured MCP / discovered skill). An unrecognized identifier is a typo and blocks
    activation (UDR-0072 D9); a recognized-but-gated-off tool is fine (it simply adds no
    tool at build time). Best-effort: any inventory read failure leaves the spec unchanged.
    """
    if not spec.tool_allowlist:
        return spec
    try:
        from app.agent.declarative.tool_inventory import known_tool_ids

        known = known_tool_ids()
    except Exception:  # never break resolution on an inventory read problem
        logger.debug("Tool inventory read failed during allow-list validation", exc_info=True)
        return spec
    unknown = [i for i in spec.tool_allowlist if i not in known]
    if unknown:
        spec.warnings.append(
            f"tool(s) {unknown} are not recognized (check the tool name / MCP server / "
            "skill against the tool inventory). Fix or remove them to activate the agent."
        )
    return spec


def resolve_spec(agent_id: str) -> DeclarativeAgentSpec:
    """Resolve an agent id to a mapped spec, raising on parse/mapping failure.

    CORE is special-cased; custom ids are resolved against the jailed directory.
    """
    if agent_id == CORE_AGENT_ID:
        return core_spec()
    for cid, path, group_path in _discover_files():
        if cid == agent_id:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise DeclarativeAgentError(f"Could not read {agent_id}: {exc}") from exc
            spec = map_document(
                text,
                agent_id=cid,
                source="custom",
                group_path=group_path,
                default_name=path.stem,
            )
            _annotate_model_warnings(spec)
            _annotate_tool_warnings(spec)
            return spec
    raise DeclarativeAgentError(f"Unknown declarative agent id: {agent_id!r}")


def load_inventory(active_id: str) -> dict:
    """Return the management inventory (CTR-0143): CORE + custom tree with state.

    Every entry is mapped so a broken YAML is flagged (``loaded=false`` + ``error``)
    BEFORE the operator tries to activate it (UDR-0072 D9). Never raises.
    """
    agents: list[dict] = []

    core = core_spec()
    agents.append(
        {
            "id": core.id,
            "name": core.name,
            "description": core.description,
            "group_path": [],
            "source": "core",
            "active": active_id == core.id,
            "loaded": True,
            "error": None,
            "warnings": core.warnings,
            # CTR-0143 v2 (PRP-0117): the bundled CORE agent is not a file on disk, so
            # it can be neither edited nor deleted; it never restricts its tool surface.
            "editable": False,
            "tool_allowlist": None,
        }
    )

    for cid, path, group_path in _discover_files():
        entry: dict = {
            "id": cid,
            "name": path.stem,
            "description": "",
            "group_path": list(group_path),
            "source": "custom",
            "active": active_id == cid,
            "loaded": False,
            "error": None,
            "warnings": [],
            # CTR-0143 v2 (PRP-0117): a custom agent is a YAML file, so the management
            # modal offers Edit / Delete even when it currently fails to map.
            "editable": True,
            "tool_allowlist": None,
        }
        try:
            text = path.read_text(encoding="utf-8")
            spec = map_document(text, agent_id=cid, source="custom", group_path=group_path, default_name=path.stem)
            _annotate_model_warnings(spec)
            _annotate_tool_warnings(spec)
            entry["name"] = spec.name
            entry["description"] = spec.description
            entry["loaded"] = True
            entry["warnings"] = spec.warnings
            entry["tool_allowlist"] = spec.tool_allowlist
        except DeclarativeAgentError as exc:
            entry["error"] = str(exc)
        except OSError as exc:
            entry["error"] = f"Could not read file: {exc}"
        agents.append(entry)

    return {"active": active_id, "agents_dir": str(_agents_dir() or ""), "agents": agents}


__all__ = [
    "CORE_AGENT_YAML",
    "core_spec",
    "load_inventory",
    "resolve_spec",
]
