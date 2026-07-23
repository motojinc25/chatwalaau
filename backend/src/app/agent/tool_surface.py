"""Tool surface report for the Prompt Dump (PRP-0119, CTR-0009 v18, UDR-0102).

Composes ONE read model describing the tools of the agent that is about to run:
every built-in function tool, every MCP server / tool, every Skill, and the
provider-supplied web search -- each with a status and, when excluded, the REASON.

The report reconciles two INDEPENDENTLY derived views (UDR-0102 D4):

  * ACTUAL    -- what the built ``Agent`` will actually offer the model, read from
                 the instance itself (UDR-0102 D1);
  * PREDICTED -- what the configuration says should be offered: the settings gates,
                 the active declarative agent's ``tool_allowlist`` (CTR-0142), and
                 the MCP / Skills override stores (CTR-0121 / CTR-0123).

A disagreement the reason taxonomy cannot explain is reported as ``MISMATCH``
rather than silently resolved in favour of either side. That is the point of the
facility: a report generated from the configuration alone could never reveal a
configuration-vs-build divergence.

The report is READ-ONLY over runtime state (UDR-0102 D7): it MUST NOT assign
``allowed_tools``, MUST NOT touch MCP connections or lifecycle, MUST NOT trigger an
agent rebuild, and MUST NOT mutate any store. The defect that motivated this module
is an accidental write to shared MCP state; an observability facility that repeats
it would be worse than none.

Best-effort (UDR-0102 D8): every failure path returns a partial report or ``None``;
nothing here may raise into the chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from typing import Any

from app.agent.declarative.tool_ids import ResolvedAllowlist, parse_allowlist
from app.agent.declarative.tool_inventory import BUILTIN_FUNCTION_TOOLS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status + reason vocabulary
# ---------------------------------------------------------------------------
STATUS_ACTIVE = "ACTIVE"
STATUS_EXCLUDED = "EXCLUDED"
STATUS_WARNING = "WARNING"
STATUS_MISMATCH = "MISMATCH"

# Closed exclusion-reason taxonomy (UDR-0102 D3), evaluated in this precedence.
# ``skills override`` is the implementation-time addition to the four reasons named
# in PRP-0119: the Skills manager (CTR-0123) is a distinct gate from the MCP manager
# (CTR-0121) and reusing the MCP token for it would misdirect the operator to the
# wrong management surface. See PRP-0119 "Implementation notes".
REASON_SETTINGS = "settings"
REASON_ALLOWLIST = "allowlist"
REASON_MCP_OVERRIDE = "mcp override"
REASON_SKILLS_OVERRIDE = "skills override"
REASON_NOT_CONNECTED = "not connected"

# Per-tool detail for the ``settings`` reason, so the row names the gate the
# operator has to flip rather than just asserting "unavailable".
_SETTINGS_DETAIL: dict[str, str] = {
    "file_read": "CODING_ENABLED=false",
    "file_write": "CODING_ENABLED=false",
    "bash_execute": "CODING_ENABLED=false",
    "file_glob": "CODING_ENABLED=false",
    "file_grep": "CODING_ENABLED=false",
    "rag_search": "CHROMA_DIR unset or no embeddings offering",
    "generate_image": "no image offering",
    "edit_image": "no image offering",
    "manage_user_memory": "USER_PROFILE_ENABLED=false",
    "manage_memory": "AGENT_MEMORY_ENABLED=false",
    "manage_cron": "CRON_ENABLED=false",
    "manage_pipeline": "PIPELINE_ENABLED=false",
    "manage_webhook": "WEBHOOK_ENABLED=false",
    "query_ontology": "ONTOLOGY_ENABLED=false",
}


@dataclass(frozen=True)
class ToolRow:
    """One reported tool (or MCP server, which carries its tools as children)."""

    name: str
    status: str
    reason: str = ""
    note: str = ""
    children: tuple[ToolRow, ...] = ()


@dataclass
class ToolSurfaceReport:
    """The composed tool surface of one agent, ready for rendering."""

    agent_label: str = ""
    model: str = ""
    digest: str = ""
    functions: list[ToolRow] = field(default_factory=list)
    mcp: list[ToolRow] = field(default_factory=list)
    skills: list[ToolRow] = field(default_factory=list)
    provider_supplied: list[ToolRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def _all_rows(self) -> list[ToolRow]:
        rows: list[ToolRow] = []
        for group in (self.functions, self.mcp, self.skills):
            for row in group:
                rows.append(row)
                rows.extend(row.children)
        return rows

    def counts(self) -> dict[str, int]:
        """Aggregate status counts over every row, children included."""
        out = {STATUS_ACTIVE: 0, STATUS_EXCLUDED: 0, STATUS_WARNING: 0, STATUS_MISMATCH: 0}
        for row in self._all_rows():
            if row.status in out:
                out[row.status] += 1
        return out

    def active_identifiers(self) -> list[str]:
        """Stable identifiers of everything ACTIVE (the digest input)."""
        ids: list[str] = [f"function:{r.name}" for r in self.functions if r.status == STATUS_ACTIVE]
        for server in self.mcp:
            ids.extend(f"mcp:{server.name}/{c.name}" for c in server.children if c.status == STATUS_ACTIVE)
            if server.status == STATUS_WARNING:
                # Not connected: the whole server will be exposed on connect.
                ids.append(f"mcp:{server.name}/*")
        ids.extend(f"skill:{r.name}" for r in self.skills if r.status == STATUS_ACTIVE)
        return sorted(ids)


# ---------------------------------------------------------------------------
# ACTUAL: read the BUILT agent (UDR-0102 D1)
# ---------------------------------------------------------------------------
def _tool_name(obj: Any) -> str:
    """Resolve a MAF tool object's name.

    MAF converts a plain callable passed to ``Agent(tools=...)`` into a
    ``FunctionTool`` carrying ``.name``; an approval-wrapped tool
    (``wrap_with_approval`` -> ``@tool(approval_mode=...)``) is already a
    ``FunctionTool``. A bare function keeps ``__name__``. Both shapes are handled so
    the match never depends on which path a tool took (PRP-0119 risk note).
    """
    return str(getattr(obj, "name", None) or getattr(obj, "__name__", None) or "")


def _actual_function_tools(agent: Any) -> dict[str, str]:
    """Return {tool name: approval_mode} for the agent's non-MCP tools.

    MAF stores them under ``Agent.default_options["tools"]`` (there is no public
    ``Agent.tools`` attribute); MCP tools live separately in ``Agent.mcp_tools``.
    """
    out: dict[str, str] = {}
    options = getattr(agent, "default_options", None) or {}
    try:
        entries = options.get("tools") or []
    except AttributeError:
        return out
    for entry in entries:
        name = _tool_name(entry)
        if name:
            out[name] = str(getattr(entry, "approval_mode", "") or "")
    return out


def _actual_mcp_servers(agent: Any) -> dict[str, list[str] | None]:
    """Return {server name: live allowed_tools} for the agent's MCP tools.

    ``None`` means the whole server is exposed. Read only -- the value is never
    assigned here (UDR-0102 D7).
    """
    out: dict[str, list[str] | None] = {}
    for entry in getattr(agent, "mcp_tools", None) or []:
        name = _tool_name(entry)
        if not name:
            continue
        allowed = getattr(entry, "allowed_tools", None)
        out[name] = list(allowed) if allowed is not None else None
    return out


def _skills_provider(agent: Any) -> Any | None:
    """Find the SkillsProvider among the agent's context providers, if attached."""
    for provider in getattr(agent, "context_providers", None) or []:
        if type(provider).__name__.endswith("SkillsProvider"):
            return provider
    return None


async def _actual_skill_names(provider: Any) -> set[str] | None:
    """Resolve the skill names the provider will actually advertise.

    Skills are NOT in the agent's tool list: MAF injects the skill tools at run time
    through the context provider (UDR-0102 D5). The provider's source is asked
    directly; ``None`` is returned when it cannot be read, and the caller then falls
    back to the predicted set with a note rather than reporting false exclusions.
    """
    source = getattr(provider, "_source", None)
    if source is None:
        return None
    try:
        skills = await source.get_skills()
    except Exception:
        logger.debug("Could not read skills from the provider source", exc_info=True)
        return None
    names: set[str] = set()
    for skill in skills or []:
        name = getattr(getattr(skill, "frontmatter", None), "name", None)
        if name:
            names.add(str(name))
    return names


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _function_rows(agent: Any, allow: ResolvedAllowlist | None) -> tuple[list[ToolRow], list[ToolRow]]:
    """Build the built-in function rows and the provider-supplied rows."""
    actual = _actual_function_tools(agent)
    rows: list[ToolRow] = []
    known: set[str] = set()

    for builtin in BUILTIN_FUNCTION_TOOLS:
        name = builtin.name
        known.add(name)
        try:
            gate_open = bool(builtin.available())
        except Exception:
            logger.debug("Availability probe failed for %s", name, exc_info=True)
            gate_open = False
        selected = allow is None or allow.allows_function(name)
        predicted = gate_open and selected
        present = name in actual

        note = ""
        approval = actual.get(name, "")
        if present and approval and approval != "never_require":
            note = f"approval: {approval}"

        if present and predicted:
            rows.append(ToolRow(name, STATUS_ACTIVE, note=note))
        elif not present and not predicted:
            if not gate_open:
                detail = _SETTINGS_DETAIL.get(name, "")
                reason = f"{REASON_SETTINGS} ({detail})" if detail else REASON_SETTINGS
            else:
                reason = REASON_ALLOWLIST
            rows.append(ToolRow(name, STATUS_EXCLUDED, reason=reason))
        elif present and not predicted:
            why = REASON_SETTINGS if not gate_open else REASON_ALLOWLIST
            rows.append(ToolRow(name, STATUS_MISMATCH, reason=f"built but {why} predicted exclusion", note=note))
        else:
            rows.append(ToolRow(name, STATUS_MISMATCH, reason="predicted active but absent from the built agent"))

    provider_rows = [
        ToolRow(name, STATUS_ACTIVE, note="per-model, not selectable per agent")
        for name in sorted(actual)
        if name not in known
    ]
    return rows, provider_rows


def _mcp_rows(agent: Any, allow: ResolvedAllowlist | None) -> list[ToolRow]:
    """Build the MCP server rows, mirroring the agent factory's gate order."""
    from app.mcp.lifecycle import get_mcp_tool_inventory
    from app.mcp.overrides import get_override_store

    actual = _actual_mcp_servers(agent)
    store = get_override_store()
    rows: list[ToolRow] = []

    for server in get_mcp_tool_inventory():
        name = str(server.get("name") or "")
        if not name:
            continue
        exposed = [str(t.get("name") or "") for t in server.get("tools", []) if t.get("name")]
        present = name in actual

        # PREDICTED, in the agent factory's own precedence (agent_factory.py:262-302).
        if store.server_disabled(name):
            predicted: set[str] = set()
            reason = REASON_MCP_OVERRIDE
        elif allow is not None and not allow.mcp_server_selected(name):
            predicted = set()
            reason = f"{REASON_ALLOWLIST} (server not selected)"
        else:
            disabled = store.disabled_tools_for(name)
            override_enabled = [n for n in exposed if n not in disabled]
            if allow is not None:
                predicted = set(allow.mcp_allowed_tools(name, override_enabled) or [])
            else:
                predicted = set(override_enabled)
            # An empty prediction has three distinct causes and each points at a
            # different surface (UDR-0102 D3): the server has no known tools because
            # it is not connected; the MCP manager disabled all of them; or the
            # allow-list selected none of them. Attributing all three to the MCP
            # manager would send the operator to the wrong place.
            if predicted:
                reason = ""
            elif not exposed:
                reason = REASON_NOT_CONNECTED
            elif not override_enabled:
                reason = REASON_MCP_OVERRIDE
            else:
                reason = f"{REASON_ALLOWLIST} (no tool of this server selected)"

        if not present:
            status = STATUS_EXCLUDED if not predicted else STATUS_MISMATCH
            if status == STATUS_MISMATCH:
                reason = "predicted active but absent from the built agent"
            rows.append(ToolRow(name, status, reason=reason or REASON_NOT_CONNECTED))
            continue

        if not exposed:
            # Server prepared but not connected: get_server_tool_names() is empty, so
            # the factory falls back to allowed_tools=None -- a fail-OPEN widening of
            # the allow-list (agent_factory.py:298-300). Surfaced, not changed here;
            # the behaviour decision is PRP-0120.
            rows.append(
                ToolRow(
                    name,
                    STATUS_WARNING,
                    reason=REASON_NOT_CONNECTED,
                    note="allow-list not applied; all tools will be exposed on connect",
                )
            )
            continue

        allowed = actual.get(name)
        actual_tools = set(exposed) if allowed is None else set(allowed)
        disabled = store.disabled_tools_for(name)

        children: list[ToolRow] = []
        for tool_name in exposed:
            in_actual = tool_name in actual_tools
            in_predicted = tool_name in predicted
            if in_actual and in_predicted:
                children.append(ToolRow(tool_name, STATUS_ACTIVE))
            elif not in_actual and not in_predicted:
                child_reason = REASON_MCP_OVERRIDE if tool_name in disabled else REASON_ALLOWLIST
                children.append(ToolRow(tool_name, STATUS_EXCLUDED, reason=child_reason))
            elif in_actual:
                children.append(ToolRow(tool_name, STATUS_MISMATCH, reason="exposed but predicted excluded"))
            else:
                children.append(ToolRow(tool_name, STATUS_MISMATCH, reason="predicted active but not exposed"))

        active_count = sum(1 for c in children if c.status == STATUS_ACTIVE)
        server_status = STATUS_MISMATCH if any(c.status == STATUS_MISMATCH for c in children) else STATUS_ACTIVE
        rows.append(
            ToolRow(
                name,
                server_status,
                note=f"{active_count}/{len(exposed)} tools",
                children=tuple(children),
            )
        )

    return rows


async def _skill_rows(agent: Any, allow: ResolvedAllowlist | None, notes: list[str]) -> list[ToolRow]:
    """Build the Skills rows from the SkillsProvider lane (UDR-0102 D5)."""
    from app.skills.inventory import get_skills_inventory

    try:
        inventory = await get_skills_inventory()
    except Exception:
        logger.debug("Could not read the skills inventory", exc_info=True)
        return []

    entries: list[tuple[str, bool, bool]] = []
    for group in inventory.get("groups", []):
        for skill in group.get("skills", []):
            name = str(skill.get("name") or "")
            if name:
                entries.append((name, bool(skill.get("enabled")), bool(skill.get("loaded"))))
    if not entries:
        return []

    provider = _skills_provider(agent)
    if provider is None:
        notes.append("no SkillsProvider attached (SKILLS_DIR missing or unreadable)")
        return [
            ToolRow(name, STATUS_EXCLUDED, reason=f"{REASON_SETTINGS} (no SkillsProvider attached)")
            for name, _enabled, _loaded in entries
        ]

    actual = await _actual_skill_names(provider)
    if actual is None:
        notes.append("skills read from configuration (provider source unreadable)")

    rows: list[ToolRow] = []
    for name, enabled, loaded in entries:
        selected = allow is None or allow.allows_skill(name)
        predicted = enabled and loaded and selected
        present = predicted if actual is None else name in actual

        if present and predicted:
            rows.append(ToolRow(name, STATUS_ACTIVE, note="injected at run time"))
        elif not present and not predicted:
            if not loaded:
                reason = f"{REASON_SETTINGS} (not loaded; Reload to apply)"
            elif not enabled:
                reason = REASON_SKILLS_OVERRIDE
            else:
                reason = REASON_ALLOWLIST
            rows.append(ToolRow(name, STATUS_EXCLUDED, reason=reason))
        elif present:
            rows.append(ToolRow(name, STATUS_MISMATCH, reason="advertised but predicted excluded"))
        else:
            rows.append(ToolRow(name, STATUS_MISMATCH, reason="predicted active but not advertised"))
    return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def describe_tool_surface(
    agent: Any,
    *,
    spec: Any = None,
    model: str = "",
) -> ToolSurfaceReport | None:
    """Compose the tool surface report for ``agent`` (CTR-0009 v18, UDR-0102).

    ``spec`` pins the declarative agent whose ``tool_allowlist`` supplies the
    PREDICTED view; omitted, the globally active spec is used -- matching what the
    agent factory read when it built this agent.

    Returns ``None`` on any failure (logged at WARNING). Never raises, never mutates
    runtime state.
    """
    try:
        if spec is None:
            from app.agent.declarative.store import active_spec

            spec = active_spec()
        allow = parse_allowlist(getattr(spec, "tool_allowlist", None))

        notes: list[str] = []
        report = ToolSurfaceReport(model=model)
        report.functions, report.provider_supplied = _function_rows(agent, allow)
        report.mcp = _mcp_rows(agent, allow)
        report.skills = await _skill_rows(agent, allow, notes)
        report.notes = notes

        spec_id = str(getattr(spec, "id", "") or "")
        spec_name = str(getattr(spec, "name", "") or spec_id)
        scope = "all tools inherited" if allow is None else "tool allow-list active"
        label = spec_name if spec_name == spec_id else f"{spec_name} ({spec_id})"
        report.agent_label = f"{label} -- {scope}" if spec_id else scope

        digest_input = "|".join(report.active_identifiers()).encode("utf-8")
        report.digest = hashlib.sha256(digest_input).hexdigest()[:8]
        return report
    except Exception:
        logger.warning("Could not compose the tool surface report", exc_info=True)
        return None


__all__ = [
    "REASON_ALLOWLIST",
    "REASON_MCP_OVERRIDE",
    "REASON_NOT_CONNECTED",
    "REASON_SETTINGS",
    "REASON_SKILLS_OVERRIDE",
    "STATUS_ACTIVE",
    "STATUS_EXCLUDED",
    "STATUS_MISMATCH",
    "STATUS_WARNING",
    "ToolRow",
    "ToolSurfaceReport",
    "describe_tool_surface",
]
