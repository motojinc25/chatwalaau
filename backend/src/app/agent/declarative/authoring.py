"""Declarative agent authoring: canonical serialization + jailed writes (CTR-0177).

Persists a single ``kind: Prompt`` declarative agent YAML under
``DECLARATIVE_AGENTS_DIR`` so the GUI (CTR-0179) can CREATE / EDIT / DELETE agents
that auto-register into the CTR-0143 inventory (PRP-0117, FEAT-0061, UDR-0100
D5/D8). Writes go through the SAME realpath jail rooted at the agents dir that the
loader reads through (the CTR-0031 technique); a canonical serializer owns YAML
output (``instructions`` as a block scalar, ``kind`` fixed to ``Prompt``).

The runtime Agent is still realized by ChatWalaʻau (CTR-0142 mapping); this module
only parses/validates for authoring and persists text. It never activates an agent
(that stays CTR-0143 ``PUT /api/agents/active``).
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

import yaml

from app.agent.declarative.loader import resolve_spec
from app.agent.declarative.mapping import map_document
from app.agent.declarative.spec import CORE_AGENT_ID, DeclarativeAgentError
from app.core.config import settings


class _Literal(str):
    """A str rendered as a YAML block scalar (``|``) by the canonical dumper."""


def _literal_representer(dumper: yaml.Dumper, data: _Literal) -> Any:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.SafeDumper.add_representer(_Literal, _literal_representer)


# ---------------------------------------------------------------------------
# Directory + jail
# ---------------------------------------------------------------------------
def _configured_dir() -> Path | None:
    raw = (settings.declarative_agents_dir or "").strip()
    return Path(raw) if raw else None


def authoring_status() -> tuple[Path | None, bool]:
    """Return (agents_dir, writable). Creates the dir if the parent allows it.

    Authoring is available only when ``DECLARATIVE_AGENTS_DIR`` is set to a WRITABLE
    directory (UDR-0100 D5); otherwise the surface is read-only. DEMO_MODE is treated
    as read-only regardless (a public deploy must not accept agent writes).
    """
    from app.demo import is_demo_mode

    directory = _configured_dir()
    if directory is None or is_demo_mode():
        return directory, False
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return directory, False
    return directory, os.access(directory, os.W_OK)


def _jail_ok(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves inside ``root`` (symlink-escape safe)."""
    try:
        root_real = os.path.realpath(root)
        cand_real = os.path.realpath(candidate)
    except OSError:
        return False
    return cand_real == root_real or cand_real.startswith(root_real + os.sep)


def sanitize_stem(name: str) -> str:
    """Derive a safe, single-segment filename stem from an agent name.

    Keeps unicode letters/digits (so a Japanese name stays readable), collapses
    whitespace to ``-``, drops path separators and unsafe characters, and trims to a
    sane length. Raises ``DeclarativeAgentError`` when nothing usable remains.
    """
    base = (name or "").strip()
    base = re.sub(r"\s+", "-", base)
    # Remove anything that is not a word char (unicode), dash, or underscore.
    base = re.sub(r"[^\w\-]", "", base, flags=re.UNICODE)
    base = base.strip("-_.")[:80]
    if not base:
        raise DeclarativeAgentError("Agent name does not yield a valid file name; use letters or digits.")
    return base


def _existing_ids() -> set[str]:
    from app.agent.declarative.loader import _discover_files

    return {cid for cid, _, _ in _discover_files()}


def _path_for_id(root: Path, agent_id: str) -> Path:
    """Resolve an existing agent id to its file path within the jail.

    ``agent_id`` is the POSIX relative path without extension (e.g. "support/triage").
    Tries ``.yaml`` then ``.yml``. Raises on jail escape or a missing file.
    """
    if agent_id == CORE_AGENT_ID:
        raise DeclarativeAgentError("The bundled CORE agent cannot be edited or deleted.")
    rel = Path(*agent_id.split("/"))
    for suffix in (".yaml", ".yml"):
        candidate = root / rel.with_suffix(suffix)
        if not _jail_ok(root, candidate):
            raise DeclarativeAgentError("Invalid agent id (path escapes the agents directory).")
        if candidate.is_file():
            return candidate
    raise DeclarativeAgentError(f"Unknown declarative agent id: {agent_id!r}")


# ---------------------------------------------------------------------------
# Canonical serialization (UDR-0100 D8)
# ---------------------------------------------------------------------------
def build_agent_yaml(document: dict[str, Any]) -> str:
    """Serialize a structured authoring document to canonical agent YAML.

    Field order is fixed (kind, name, displayName, description, instructions, model,
    tools, outputSchema); ``instructions`` renders as a block scalar; ``kind`` is
    always ``Prompt`` (single-agent scope). Unknown keys are ignored -- the GUI only
    authors the mapped subset (UDR-0100 D4/D8). Provider / connection / sampling are
    never emitted.
    """
    doc: dict[str, Any] = {"kind": "Prompt"}

    name = str(document.get("name") or "").strip()
    if name:
        doc["name"] = name
    display_name = str(document.get("displayName") or "").strip()
    if display_name:
        doc["displayName"] = display_name
    description = str(document.get("description") or "").strip()
    if description:
        doc["description"] = description

    instructions = document.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        doc["instructions"] = _Literal(instructions) if "\n" in instructions else instructions

    model_in = document.get("model") or {}
    model_out: dict[str, Any] = {}
    if isinstance(model_in, dict):
        model_id = str(model_in.get("id") or "").strip()
        if model_id:
            model_out["id"] = model_id
        opts_in = model_in.get("options") or {}
        opts_out: dict[str, Any] = {}
        if isinstance(opts_in, dict):
            for key in ("effort", "verbosity"):
                val = str(opts_in.get(key) or "").strip()
                if val:
                    opts_out[key] = val
        if opts_out:
            model_out["options"] = opts_out
    if model_out:
        doc["model"] = model_out

    tools_in = document.get("tools")
    if isinstance(tools_in, list) and tools_in:
        tools_out: list[dict[str, Any]] = []
        for entry in tools_in:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "function").strip().lower()
            tname = str(entry.get("name") or "").strip()
            if not tname:
                continue
            item: dict[str, Any] = {"kind": kind, "name": tname}
            allowed = entry.get("allowedTools") or entry.get("allowed_tools")
            if kind == "mcp" and isinstance(allowed, list) and allowed:
                item["allowedTools"] = [str(a).strip() for a in allowed if str(a).strip()]
            tools_out.append(item)
        if tools_out:
            doc["tools"] = tools_out

    schema_in = document.get("outputSchema")
    if isinstance(schema_in, dict):
        props_in = schema_in.get("properties")
        if isinstance(props_in, dict) and props_in:
            props_out: dict[str, Any] = {}
            for prop_name, desc in props_in.items():
                if not isinstance(desc, dict):
                    continue
                # Field order matches the declarative sample: type, required, description.
                prop: dict[str, Any] = {"type": str(desc.get("type") or "string")}
                if desc.get("required"):
                    prop["required"] = True
                pdesc = str(desc.get("description") or "").strip()
                if pdesc:
                    prop["description"] = pdesc
                props_out[str(prop_name)] = prop
            if props_out:
                doc["outputSchema"] = {"properties": props_out}

    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def document_from_yaml(text: str) -> dict[str, Any]:
    """Best-effort reshape of an agent YAML into the structured authoring document.

    Lets the GUI seed its form/canvas from an existing file for editing without a
    client-side YAML parser (UDR-0100 D7). Tolerant: a field it cannot read is
    omitted / defaulted. The monaco raw-edit escape hatch always has the verbatim
    ``yaml`` regardless.
    """
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return {}

    model_raw = data.get("model")
    model_out: dict[str, Any] = {"id": "", "options": {"effort": "", "verbosity": ""}}
    if isinstance(model_raw, dict):
        model_out["id"] = str(model_raw.get("id") or "")
        opts = model_raw.get("options")
        if isinstance(opts, dict):
            model_out["options"]["effort"] = str(opts.get("effort") or opts.get("reasoningEffort") or "")
            model_out["options"]["verbosity"] = str(opts.get("verbosity") or opts.get("textVerbosity") or "")
    elif model_raw is not None:
        model_out["id"] = str(model_raw)

    tools_out: list[dict[str, Any]] = []
    tools_raw = data.get("tools")
    if isinstance(tools_raw, list):
        for entry in tools_raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            item: dict[str, Any] = {"kind": str(entry.get("kind") or "function").strip().lower(), "name": name}
            allowed = entry.get("allowedTools") or entry.get("allowed_tools")
            if isinstance(allowed, list) and allowed:
                item["allowedTools"] = [str(a).strip() for a in allowed if str(a).strip()]
            tools_out.append(item)

    schema_out: dict[str, Any] | None = None
    schema_raw = data.get("outputSchema") or data.get("output_schema")
    if isinstance(schema_raw, dict) and isinstance(schema_raw.get("properties"), dict):
        props: dict[str, Any] = {}
        for pname, desc in schema_raw["properties"].items():
            if isinstance(desc, dict):
                props[str(pname)] = {
                    "type": str(desc.get("type") or "string"),
                    "description": str(desc.get("description") or ""),
                    "required": bool(desc.get("required")),
                }
        if props:
            schema_out = {"properties": props}

    instructions = data.get("instructions")
    return {
        "name": str(data.get("name") or ""),
        "displayName": str(data.get("displayName") or ""),
        "description": str(data.get("description") or ""),
        "instructions": instructions if isinstance(instructions, str) else "",
        "model": model_out,
        "tools": tools_out,
        "outputSchema": schema_out,
    }


def _yaml_from_body(body: dict[str, Any]) -> str:
    """Return the YAML text to persist: verbatim ``yaml`` or serialized ``document``.

    The form/canvas path sends ``document`` (backend owns serialization, UDR-0100 D8);
    the monaco raw-edit escape hatch sends ``yaml`` (persisted verbatim after validation).
    """
    if isinstance(body.get("yaml"), str) and body["yaml"].strip():
        return body["yaml"]
    document = body.get("document")
    if isinstance(document, dict):
        return build_agent_yaml(document)
    raise DeclarativeAgentError("Provide either a 'yaml' string or a 'document' object.")


# ---------------------------------------------------------------------------
# Validation + CRUD
# ---------------------------------------------------------------------------
def validate_document(body: dict[str, Any]) -> dict[str, Any]:
    """Dry-run: serialize (if needed), parse + map, and return a summary (no persist).

    Never raises for a mapping problem; returns ``valid``/``error``/``warnings`` so
    the GUI can render them inline with the same rules the backend enforces at
    activation (UDR-0100 D6). Model-id / tool-id live-state warnings are appended so
    the preview matches what activation would report.
    """
    try:
        text = _yaml_from_body(body)
    except DeclarativeAgentError as exc:
        return {"valid": False, "error": str(exc), "warnings": [], "yaml": None}

    try:
        spec = map_document(text, agent_id="_preview", source="custom")
    except DeclarativeAgentError as exc:
        return {"valid": False, "error": str(exc), "warnings": [], "yaml": text}

    # Live-state warnings (unconfigured model / unrecognized tool) mirror the loader.
    from app.agent.declarative.loader import _annotate_model_warnings, _annotate_tool_warnings

    _annotate_model_warnings(spec)
    _annotate_tool_warnings(spec)

    return {
        "valid": True,
        "error": None,
        "warnings": spec.warnings,
        "yaml": text,
        "summary": {
            "name": spec.name,
            "description": spec.description,
            "model_filter": spec.model_filter,
            "model_options": spec.model_options_override,
            "tool_allowlist": spec.tool_allowlist,
            "structured_output": bool(spec.structured_output),
        },
    }


def read_source(agent_id: str) -> str:
    """Return the raw YAML text of a custom agent for editing."""
    directory, _ = authoring_status()
    root = directory or _configured_dir()
    if root is None:
        raise DeclarativeAgentError("DECLARATIVE_AGENTS_DIR is not configured.")
    path = _path_for_id(root, agent_id)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeclarativeAgentError(f"Could not read {agent_id}: {exc}") from exc


def create_agent(body: dict[str, Any]) -> str:
    """Create a new top-level agent file from ``yaml``/``document``; return its id.

    Validates the mapping (a mapping error -> DeclarativeAgentError -> 400), derives a
    safe filename from the agent name, rejects a collision, and writes atomically-ish
    (write + os.replace). Does NOT rebuild -- the caller rebuilds so the new agent
    auto-registers (UDR-0100 D5).
    """
    directory, writable = authoring_status()
    if directory is None:
        raise DeclarativeAgentError("DECLARATIVE_AGENTS_DIR is not configured; authoring is unavailable.")
    if not writable:
        raise DeclarativeAgentError("The declarative agents directory is not writable (authoring is read-only).")

    text = _yaml_from_body(body)
    data = yaml.safe_load(text)  # raises on malformed YAML -> caught by the router as 400
    if not isinstance(data, dict):
        raise DeclarativeAgentError("Declarative agent document must be a YAML mapping.")
    # Validate the mapping up front (rejects e.g. temperature). Warnings are allowed
    # at save time; the operator resolves them before activation (UDR-0072 D9).
    map_document(text, agent_id="_new", source="custom")

    stem = sanitize_stem(str(body.get("name") or data.get("name") or data.get("displayName") or ""))
    if stem in _existing_ids():
        raise DeclarativeAgentError(f"An agent named {stem!r} already exists; choose a different name.")
    path = directory / f"{stem}.yaml"
    if not _jail_ok(directory, path):
        raise DeclarativeAgentError("Invalid agent name (path escapes the agents directory).")

    _atomic_write(path, text)
    return stem


def update_agent(agent_id: str, body: dict[str, Any]) -> str:
    """Overwrite an existing agent file in place (id/filename stays stable)."""
    directory, writable = authoring_status()
    if directory is None:
        raise DeclarativeAgentError("DECLARATIVE_AGENTS_DIR is not configured; authoring is unavailable.")
    if not writable:
        raise DeclarativeAgentError("The declarative agents directory is not writable (authoring is read-only).")

    path = _path_for_id(directory, agent_id)
    text = _yaml_from_body(body)
    map_document(text, agent_id=agent_id, source="custom")  # validate before overwrite
    _atomic_write(path, text)
    return agent_id


def delete_agent(agent_id: str) -> None:
    """Delete a custom agent file (jail-checked)."""
    directory, writable = authoring_status()
    if directory is None:
        raise DeclarativeAgentError("DECLARATIVE_AGENTS_DIR is not configured; authoring is unavailable.")
    if not writable:
        raise DeclarativeAgentError("The declarative agents directory is not writable (authoring is read-only).")
    path = _path_for_id(directory, agent_id)
    try:
        path.unlink()
    except OSError as exc:
        raise DeclarativeAgentError(f"Could not delete {agent_id}: {exc}") from exc


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# Re-export so tests / callers can assert on the resolver used by the API.
__all__ = [
    "authoring_status",
    "build_agent_yaml",
    "create_agent",
    "delete_agent",
    "document_from_yaml",
    "read_source",
    "resolve_spec",
    "sanitize_stem",
    "update_agent",
    "validate_document",
]
