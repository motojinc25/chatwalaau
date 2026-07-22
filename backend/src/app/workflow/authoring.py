"""Declarative workflow authoring: canonical serialization + jailed writes (CTR-0183).

Persists a ``kind: Workflow`` YAML under ``DECLARATIVE_AGENTS_DIR`` so the DAG editor
(CTR-0184) can CREATE / EDIT / DELETE workflows that auto-register into the CTR-0182
inventory (PRP-0118, UDR-0101 D9/D10). Writes go through the SAME realpath jail the
loader reads through (the CTR-0031 technique); one canonical serializer owns YAML
output (``kind`` fixed ``Workflow``; ``instructions``/text as block scalars). A name
collision is rejected against EVERY declarative YAML (agents and workflows share the
tree). Never activates / runs a workflow (that stays CTR-0182 / CTR-0181).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from app.agent.declarative.authoring import _Literal, sanitize_stem
from app.agent.declarative.authoring import authoring_status as _agent_authoring_status
from app.core.config import settings
from app.workflow.loader import all_yaml_stems, validate_workflow_text
from app.workflow.spec import WORKFLOW_KIND, WorkflowError


def _configured_dir() -> Path | None:
    raw = (settings.declarative_agents_dir or "").strip()
    return Path(raw) if raw else None


def authoring_status() -> tuple[Path | None, bool]:
    """Return (dir, writable). Workflows share the agents dir + writability rule."""
    return _agent_authoring_status()


def _jail_ok(root: Path, candidate: Path) -> bool:
    try:
        root_real = os.path.realpath(root)
        cand_real = os.path.realpath(candidate)
    except OSError:
        return False
    return cand_real == root_real or cand_real.startswith(root_real + os.sep)


def _path_for_id(root: Path, workflow_id: str) -> Path:
    """Resolve an existing workflow id to its file path within the jail."""
    rel = Path(*workflow_id.split("/"))
    for suffix in (".yaml", ".yml"):
        candidate = root / rel.with_suffix(suffix)
        if not _jail_ok(root, candidate):
            raise WorkflowError("Invalid workflow id (path escapes the workflows directory).")
        if candidate.is_file():
            return candidate
    raise WorkflowError(f"Unknown workflow id: {workflow_id!r}")


# ---------------------------------------------------------------------------
# Canonical serialization (UDR-0101 D9 / the UDR-0100 D8 pattern)
# ---------------------------------------------------------------------------
def build_workflow_yaml(document: dict[str, Any]) -> str:
    """Serialize a structured authoring document to canonical workflow YAML.

    The DAG editor flattens its graph to an ordered ``actions`` list; this serializer
    fixes ``kind: Workflow`` and field order (kind, name, description, maxTurns,
    actions) and renders long text (e.g. SendActivity text) as a block scalar. Actions
    are passed through structurally (the editor authors only mapped kinds); unknown
    top-level keys are ignored.
    """
    doc: dict[str, Any] = {"kind": WORKFLOW_KIND}
    name = str(document.get("name") or "").strip()
    if name:
        doc["name"] = name
    display_name = str(document.get("displayName") or "").strip()
    if display_name:
        doc["displayName"] = display_name
    description = str(document.get("description") or "").strip()
    if description:
        doc["description"] = description
    max_turns = document.get("maxTurns")
    if isinstance(max_turns, int) and max_turns > 0:
        doc["maxTurns"] = max_turns

    actions = document.get("actions")
    if isinstance(actions, list) and actions:
        doc["actions"] = [_normalize_action(a) for a in actions if isinstance(a, dict)]

    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    """Pass an action through, rendering a long ``text`` field as a block scalar."""
    out: dict[str, Any] = {}
    for key, value in action.items():
        if key == "text" and isinstance(value, str) and "\n" in value:
            out[key] = _Literal(value)
        elif isinstance(value, dict):
            out[key] = _normalize_action(value)
        elif isinstance(value, list):
            out[key] = [_normalize_action(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def _yaml_from_body(body: dict[str, Any]) -> str:
    """Return the YAML to persist: verbatim ``yaml`` or serialized ``document``."""
    if isinstance(body.get("yaml"), str) and body["yaml"].strip():
        return body["yaml"]
    document = body.get("document")
    if isinstance(document, dict):
        return build_workflow_yaml(document)
    raise WorkflowError("Provide either a 'yaml' string or a 'document' object.")


# ---------------------------------------------------------------------------
# Validation + CRUD
# ---------------------------------------------------------------------------
def validate_document(body: dict[str, Any]) -> dict[str, Any]:
    """Dry-run: serialize (if needed) then map + reference-validate; never persists."""
    try:
        text = _yaml_from_body(body)
    except WorkflowError as exc:
        return {"valid": False, "error": str(exc), "warnings": [], "yaml": None}
    result = validate_workflow_text(text)
    result["yaml"] = text
    return result


def document_from_yaml(text: str) -> dict[str, Any]:
    """Best-effort reshape of a workflow YAML into the structured authoring document."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "name": str(data.get("name") or ""),
        "displayName": str(data.get("displayName") or ""),
        "description": str(data.get("description") or ""),
        "maxTurns": data.get("maxTurns"),
        "actions": data.get("actions") if isinstance(data.get("actions"), list) else [],
    }


def read_source(workflow_id: str) -> str:
    directory, _ = authoring_status()
    root = directory or _configured_dir()
    if root is None:
        raise WorkflowError("DECLARATIVE_AGENTS_DIR is not configured.")
    path = _path_for_id(root, workflow_id)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"Could not read {workflow_id}: {exc}") from exc


def _require_dir() -> Path:
    directory, writable = authoring_status()
    if directory is None:
        raise WorkflowError("DECLARATIVE_AGENTS_DIR is not configured; authoring is unavailable.")
    if not writable:
        raise WorkflowError("The declarative agents directory is not writable (authoring is read-only).")
    return directory


def create_workflow(body: dict[str, Any]) -> str:
    """Create a new workflow file; validate the mapping, reject a name collision."""
    directory = _require_dir()
    text = _yaml_from_body(body)
    # Validate the mapping up front (rejects unmapped actions / bad kind). Reference
    # warnings are allowed at save time; the operator resolves them before running.
    from app.workflow.loader import map_workflow_document

    map_workflow_document(text, workflow_id="_new", source="custom")

    data = yaml.safe_load(text)
    stem = sanitize_stem(str(body.get("name") or (data or {}).get("name") or ""))
    if stem in all_yaml_stems():
        raise WorkflowError(f"A declarative file named {stem!r} already exists; choose a different name.")
    path = directory / f"{stem}.yaml"
    if not _jail_ok(directory, path):
        raise WorkflowError("Invalid workflow name (path escapes the workflows directory).")
    _atomic_write(path, text)
    return stem


def update_workflow(workflow_id: str, body: dict[str, Any]) -> str:
    directory = _require_dir()
    path = _path_for_id(directory, workflow_id)
    text = _yaml_from_body(body)
    from app.workflow.loader import map_workflow_document

    map_workflow_document(text, workflow_id=workflow_id, source="custom")  # validate before overwrite
    _atomic_write(path, text)
    return workflow_id


def delete_workflow(workflow_id: str) -> None:
    directory = _require_dir()
    path = _path_for_id(directory, workflow_id)
    try:
        path.unlink()
    except OSError as exc:
        raise WorkflowError(f"Could not delete {workflow_id}: {exc}") from exc


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "authoring_status",
    "build_workflow_yaml",
    "create_workflow",
    "delete_workflow",
    "document_from_yaml",
    "read_source",
    "update_workflow",
    "validate_document",
]
