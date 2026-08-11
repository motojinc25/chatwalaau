"""Harness agent authoring: canonical serialization + jailed writes (CTR-0195).

Persists a ``kind: Harness`` YAML under ``DECLARATIVE_AGENTS_DIR`` so the GUI
(CTR-0196) can CREATE / EDIT / DELETE harness agents that auto-register into the
CTR-0194 inventory (PRP-0135, FEAT-0064, UDR-0119 D1/D7/D8). Writes go through
the SAME realpath jail + filename sanitization as the Prompt / Workflow lanes;
the canonical serializer owns YAML output (``kind`` fixed to ``Harness``,
instructions as block scalars). Validation is the single source of truth the GUI
calls (the UDR-0100 D6 pattern): CTR-0192 mapping + CTR-0193 preflight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Reuse the Prompt lane's jail / sanitization primitives (one technique, CTR-0031).
from app.agent.declarative.authoring import _atomic_write, _jail_ok, authoring_status, sanitize_stem
from app.agent.harness.factory import preflight
from app.agent.harness.loader import discover_files, resolve_spec
from app.agent.harness.mapping import map_document
from app.agent.harness.spec import HARNESS_KIND, HarnessAgentError


class _Literal(str):
    """A str rendered as a YAML block scalar (``|``) by the canonical dumper."""


def _literal_representer(dumper: yaml.Dumper, data: _Literal) -> Any:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.SafeDumper.add_representer(_Literal, _literal_representer)


def _scalar_or_literal(text: str) -> Any:
    return _Literal(text) if "\n" in text else text


# ---------------------------------------------------------------------------
# Canonical serialization (the UDR-0100 D8 pattern for the Harness kind)
# ---------------------------------------------------------------------------
def build_harness_yaml(document: dict[str, Any]) -> str:
    """Serialize a structured authoring document to canonical harness YAML.

    Field order is fixed (kind, name, displayName, description, model,
    instructions, tools, compaction, todo, mode, fileMemory, fileAccess,
    webSearch, loop); ``kind`` is always ``Harness``. Unknown keys
    are ignored -- the GUI only authors the mapped subset. Provider / connection
    / sampling are never emitted (UDR-0119 D10).
    """
    doc: dict[str, Any] = {"kind": HARNESS_KIND}

    for key in ("name", "displayName", "description"):
        val = str(document.get(key) or "").strip()
        if val:
            doc[key] = val

    model_in = document.get("model") or {}
    model_id = ""
    if isinstance(model_in, dict):
        model_id = str(model_in.get("id") or "").strip()
    elif isinstance(model_in, str):
        model_id = model_in.strip()
    if model_id:
        doc["model"] = {"id": model_id}

    instr_in = document.get("instructions") or {}
    instr_out: dict[str, Any] = {}
    if isinstance(instr_in, dict):
        for key in ("harness", "agent"):
            val = instr_in.get(key)
            if isinstance(val, str) and val.strip():
                instr_out[key] = _scalar_or_literal(val)
    if instr_out:
        doc["instructions"] = instr_out

    tools_in = document.get("tools")
    if isinstance(tools_in, list):
        tools_out = [str(t).strip() for t in tools_in if isinstance(t, str) and str(t).strip()]
        if tools_out:
            doc["tools"] = tools_out

    def _switch_block(key: str, fields: dict[str, Any]) -> None:
        block_in = document.get(key)
        if not isinstance(block_in, dict):
            return
        block_out: dict[str, Any] = {}
        for fname, default in fields.items():
            val = block_in.get(fname)
            if val is None or val == default:
                continue
            block_out[fname] = val
        if block_out:
            doc[key] = block_out

    _switch_block("compaction", {"disabled": False, "maxContextWindowTokens": None, "maxOutputTokens": None})
    _switch_block("todo", {"disabled": False})
    _switch_block("mode", {"disabled": False, "initial": None})
    _switch_block("fileMemory", {"disabled": False})
    _switch_block("fileAccess", {"disableWriteTools": False, "disableWriteToolApproval": False})
    _switch_block("webSearch", {"disabled": False})
    _switch_block("loop", {"maxIterations": None})

    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def document_from_yaml(text: str) -> dict[str, Any]:
    """Best-effort reshape of a harness YAML into the structured authoring document.

    Lets the GUI seed its form/canvas from an existing file (the UDR-0100 D7
    pattern). Tolerant: a field it cannot read is omitted / defaulted; the monaco
    raw-edit escape hatch always has the verbatim ``yaml`` regardless.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}

    def _block(key: str) -> dict[str, Any]:
        raw = data.get(key)
        return raw if isinstance(raw, dict) else {}

    model_raw = data.get("model")
    model_id = ""
    if isinstance(model_raw, dict):
        model_id = str(model_raw.get("id") or "")
    elif model_raw is not None:
        model_id = str(model_raw)

    instr = _block("instructions")
    tools_raw = data.get("tools")
    tools = [str(t) for t in tools_raw if isinstance(t, str)] if isinstance(tools_raw, list) else []

    compaction = _block("compaction")
    mode = _block("mode")
    file_access = _block("fileAccess")
    loop = _block("loop")

    return {
        "name": str(data.get("name") or ""),
        "displayName": str(data.get("displayName") or ""),
        "description": str(data.get("description") or ""),
        "model": {"id": model_id},
        "instructions": {
            "harness": instr.get("harness") if isinstance(instr.get("harness"), str) else "",
            "agent": instr.get("agent") if isinstance(instr.get("agent"), str) else "",
        },
        "tools": tools,
        "compaction": {
            "disabled": bool(compaction.get("disabled")),
            "maxContextWindowTokens": compaction.get("maxContextWindowTokens"),
            "maxOutputTokens": compaction.get("maxOutputTokens"),
        },
        "todo": {"disabled": bool(_block("todo").get("disabled"))},
        "mode": {"disabled": bool(mode.get("disabled")), "initial": mode.get("initial") or None},
        "fileMemory": {"disabled": bool(_block("fileMemory").get("disabled"))},
        "fileAccess": {
            "disableWriteTools": bool(file_access.get("disableWriteTools")),
            "disableWriteToolApproval": bool(file_access.get("disableWriteToolApproval")),
        },
        "webSearch": {"disabled": bool(_block("webSearch").get("disabled"))},
        "loop": {"maxIterations": loop.get("maxIterations")},
    }


def _yaml_from_body(body: dict[str, Any]) -> str:
    """Return the YAML text to persist: verbatim ``yaml`` or serialized ``document``."""
    if isinstance(body.get("yaml"), str) and body["yaml"].strip():
        return body["yaml"]
    document = body.get("document")
    if isinstance(document, dict):
        return build_harness_yaml(document)
    raise HarnessAgentError("Provide either a 'yaml' string or a 'document' object.")


# ---------------------------------------------------------------------------
# Validation + CRUD
# ---------------------------------------------------------------------------
def validate_document(body: dict[str, Any]) -> dict[str, Any]:
    """Dry-run: serialize (if needed), map + preflight, and return a report.

    Never raises for a mapping problem; returns ``valid`` / ``error`` /
    ``warnings`` so the GUI renders the same rules run-target selection enforces
    (UDR-0119 D8, the UDR-0100 D6 pattern).
    """
    try:
        text = _yaml_from_body(body)
    except HarnessAgentError as exc:
        return {"valid": False, "error": str(exc), "warnings": [], "yaml": None}

    try:
        spec = map_document(text, agent_id="_preview")
    except HarnessAgentError as exc:
        return {"valid": False, "error": str(exc), "warnings": [], "yaml": text}

    from app.agent.harness.loader import (
        _annotate_budget_warnings,
        _annotate_model_warnings,
        _annotate_tool_warnings,
    )

    _annotate_model_warnings(spec)
    _annotate_tool_warnings(spec)
    _annotate_budget_warnings(spec)

    return {
        "valid": True,
        "error": None,
        "warnings": spec.warnings,
        "yaml": text,
        "summary": {
            "name": spec.name,
            "description": spec.description,
            "model": spec.model_id,
            "tools": spec.tool_allowlist,
            **preflight(spec),
        },
    }


def _configured_dir() -> Path | None:
    directory, _ = authoring_status()
    return directory


def _existing_stems() -> set[str]:
    """Every discovered *.yaml/.yml id of ANY kind (cross-kind collision check)."""
    from app.workflow.loader import all_yaml_stems

    return all_yaml_stems()


def _path_for_id(root: Path, agent_id: str) -> Path:
    """Resolve an existing harness agent id to its file path within the jail."""
    rel = Path(*agent_id.split("/"))
    for suffix in (".yaml", ".yml"):
        candidate = root / rel.with_suffix(suffix)
        if not _jail_ok(root, candidate):
            raise HarnessAgentError("Invalid agent id (path escapes the agents directory).")
        if candidate.is_file():
            return candidate
    raise HarnessAgentError(f"Unknown harness agent id: {agent_id!r}")


def read_source(agent_id: str) -> str:
    """Return the raw YAML text of a harness agent for editing."""
    root = _configured_dir()
    if root is None:
        raise HarnessAgentError("DECLARATIVE_AGENTS_DIR is not configured.")
    # Restrict to files the harness loader claims (kind dispatch, UDR-0119 D1).
    known = {cid for cid, _, _ in discover_files()}
    if agent_id not in known:
        raise HarnessAgentError(f"Unknown harness agent id: {agent_id!r}")
    path = _path_for_id(root, agent_id)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessAgentError(f"Could not read {agent_id}: {exc}") from exc


def _require_writable() -> Path:
    directory, writable = authoring_status()
    if directory is None:
        raise HarnessAgentError("DECLARATIVE_AGENTS_DIR is not configured; authoring is unavailable.")
    if not writable:
        raise HarnessAgentError("The declarative agents directory is not writable (authoring is read-only).")
    return directory


def create_agent(body: dict[str, Any]) -> str:
    """Create a new top-level harness YAML; return its id (filename stem).

    Validates the mapping up front (a parse error -> 400); warnings are allowed
    at save time -- the agent stays non-runnable until they are resolved
    (UDR-0119 D8). Collision is checked across ALL kinds sharing the directory.
    """
    directory = _require_writable()
    text = _yaml_from_body(body)
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise HarnessAgentError("Harness agent document must be a YAML mapping.")
    map_document(text, agent_id="_new")  # raises on malformed / non-Harness kind

    stem = sanitize_stem(str(body.get("name") or data.get("name") or data.get("displayName") or ""))
    if stem in _existing_stems():
        raise HarnessAgentError(f"An agent or workflow named {stem!r} already exists; choose a different name.")
    path = directory / f"{stem}.yaml"
    if not _jail_ok(directory, path):
        raise HarnessAgentError("Invalid agent name (path escapes the agents directory).")
    _atomic_write(path, text)
    return stem


def update_agent(agent_id: str, body: dict[str, Any]) -> str:
    """Overwrite an existing harness YAML in place (id/filename stays stable)."""
    directory = _require_writable()
    known = {cid for cid, _, _ in discover_files()}
    if agent_id not in known:
        raise HarnessAgentError(f"Unknown harness agent id: {agent_id!r}")
    path = _path_for_id(directory, agent_id)
    text = _yaml_from_body(body)
    map_document(text, agent_id=agent_id)  # validate before overwrite
    _atomic_write(path, text)
    return agent_id


def delete_agent(agent_id: str) -> None:
    """Delete a harness YAML (jail-checked, kind-checked)."""
    directory = _require_writable()
    known = {cid for cid, _, _ in discover_files()}
    if agent_id not in known:
        raise HarnessAgentError(f"Unknown harness agent id: {agent_id!r}")
    path = _path_for_id(directory, agent_id)
    try:
        path.unlink()
    except OSError as exc:
        raise HarnessAgentError(f"Could not delete {agent_id}: {exc}") from exc


__all__ = [
    "build_harness_yaml",
    "create_agent",
    "delete_agent",
    "document_from_yaml",
    "read_source",
    "resolve_spec",
    "update_agent",
    "validate_document",
]
