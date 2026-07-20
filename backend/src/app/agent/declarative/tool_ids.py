"""Stable tool identifiers for the per-agent tool allow-list (PRP-0117, UDR-0100).

A declarative agent may carry a ``tool_allowlist`` -- a list of stable identifiers
that SUBSETS the globally available tool surface (UDR-0100 D1). This module is the
ONE definition of that identifier scheme, shared by:

  * the mapping (``app.agent.declarative.mapping._map_tools``) that turns a YAML
    ``tools:`` block into identifiers,
  * the tool inventory API (``app.agent.declarative.tool_inventory``, CTR-0178)
    that advertises the selectable identifiers to the authoring GUI, and
  * the assembly-chokepoint filter (``app.agui.agent_factory``) that applies the
    allow-list when building the per-model agents.

Keeping the scheme here guarantees an identifier a picker offers is exactly the
identifier the resolver matches, so a selection always round-trips (UDR-0100 D3).

Identifier grammar (all lowercase-kind prefixed)::

    function:<name>          a built-in FUNCTION tool by callable name
    mcp:<server>             a whole MCP server (all its exposed tools)
    mcp:<server>/<tool>      a single MCP tool of one server
    skill:<name>             an Agent Skill by name

``None`` as an allow-list (an absent YAML ``tools:`` block) means "inherit the full
shared surface" -- it is NOT represented here; the caller checks for ``None`` first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

KIND_FUNCTION = "function"
KIND_MCP = "mcp"
KIND_SKILL = "skill"


def function_id(name: str) -> str:
    """Identifier for a built-in function tool by callable name."""
    return f"{KIND_FUNCTION}:{name}"


def mcp_server_id(server: str) -> str:
    """Identifier for a whole MCP server (all exposed tools)."""
    return f"{KIND_MCP}:{server}"


def mcp_tool_id(server: str, tool: str) -> str:
    """Identifier for a single MCP tool of one server."""
    return f"{KIND_MCP}:{server}/{tool}"


def skill_id(name: str) -> str:
    """Identifier for an Agent Skill by name."""
    return f"{KIND_SKILL}:{name}"


@dataclass
class ResolvedAllowlist:
    """A parsed ``tool_allowlist`` split by kind for O(1) membership checks.

    ``mcp`` maps a server name to the set of allowed tool names, or ``None`` for
    "the whole server" (a bare ``mcp:<server>`` identifier). A server appearing
    both as a whole and with specific tools collapses to the whole server.
    """

    functions: set[str] = field(default_factory=set)
    skills: set[str] = field(default_factory=set)
    # server -> set(tool names) | None (None == whole server)
    mcp: dict[str, set[str] | None] = field(default_factory=dict)

    def allows_function(self, name: str) -> bool:
        return name in self.functions

    def allows_skill(self, name: str) -> bool:
        return name in self.skills

    def mcp_server_selected(self, server: str) -> bool:
        return server in self.mcp

    def mcp_allowed_tools(self, server: str, exposed: list[str]) -> list[str] | None:
        """Return the enabled subset for one server, or None when the server is not
        selected at all (caller drops it). An empty list means "server selected but
        none of its exposed tools matched" -- the caller also drops it.

        A whole-server selection (value ``None``) returns ``exposed`` verbatim.
        """
        if server not in self.mcp:
            return None
        wanted = self.mcp[server]
        if wanted is None:
            return list(exposed)
        return [t for t in exposed if t in wanted]


def parse_allowlist(ids: list[str] | None) -> ResolvedAllowlist | None:
    """Parse a raw ``tool_allowlist`` into a ``ResolvedAllowlist`` (None passes through).

    Unknown-shaped identifiers are ignored here (the mapping is responsible for
    warning on malformed YAML tool entries, UDR-0100 D3); this parser is defensive
    so a stray string never breaks agent construction.
    """
    if ids is None:
        return None
    resolved = ResolvedAllowlist()
    for raw in ids:
        ident = (raw or "").strip()
        if not ident or ":" not in ident:
            continue
        kind, rest = ident.split(":", 1)
        if kind == KIND_FUNCTION:
            if rest:
                resolved.functions.add(rest)
        elif kind == KIND_SKILL:
            if rest:
                resolved.skills.add(rest)
        elif kind == KIND_MCP:
            if not rest:
                continue
            if "/" in rest:
                server, tool = rest.split("/", 1)
                if not server or not tool:
                    continue
                existing = resolved.mcp.get(server, set())
                if existing is None:  # already whole-server; keep it whole
                    continue
                existing.add(tool)
                resolved.mcp[server] = existing
            else:
                resolved.mcp[rest] = None  # whole server (overrides any subset)
    return resolved


__all__ = [
    "KIND_FUNCTION",
    "KIND_MCP",
    "KIND_SKILL",
    "ResolvedAllowlist",
    "function_id",
    "mcp_server_id",
    "mcp_tool_id",
    "parse_allowlist",
    "skill_id",
]
