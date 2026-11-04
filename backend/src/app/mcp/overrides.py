"""MCP tool override store (CTR-0061, PRP-0086, PRP-0242, UDR-0223 D6).

Holds the operator's selection of which MCP servers / individual MCP tools are
DISABLED. The selection is saved in the MCP config file as ``"disabled": true`` and
``"disabled_tools"`` (UDR-0223 D6, superseding UDR-0064 D4): the lifecycle fills
this store from the file at startup and on every reload (``seed_from_configs``),
and PUT /api/mcp/tools writes the file before it updates the store. The store is
the fast in-process view the agent factory reads on every build.

The store records only the DISABLED identifiers; the live set of available
servers/tools comes from the connected MCP tool instances (CTR-0061
introspection), so the store never goes stale against a changed config.

Concurrency: a small ``threading.Lock`` guards the plain-set reads/writes.
The heavier apply path (agent rebuild) is serialised separately by the
AgentRegistry's asyncio lock (CTR-0070, UDR-0064 D5).
"""

from __future__ import annotations

import threading


class McpOverrideStore:
    """Process-local record of disabled MCP servers and tools."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._disabled_servers: set[str] = set()
        # server name -> set of disabled tool names (the tool's exposed name)
        self._disabled_tools: dict[str, set[str]] = {}

    def server_disabled(self, server: str) -> bool:
        """Return True when the whole server is disabled."""
        with self._lock:
            return server in self._disabled_servers

    def disabled_tools_for(self, server: str) -> set[str]:
        """Return a copy of the disabled tool names for one server."""
        with self._lock:
            return set(self._disabled_tools.get(server, set()))

    def is_tool_enabled(self, server: str, tool: str) -> bool:
        """Return True when a specific tool is currently exposed."""
        with self._lock:
            if server in self._disabled_servers:
                return False
            return tool not in self._disabled_tools.get(server, set())

    def set_selection(
        self,
        *,
        disabled_servers: set[str] | None = None,
        disabled_tools: dict[str, set[str]] | None = None,
    ) -> None:
        """Replace the whole selection atomically.

        ``disabled_servers`` is the set of fully-disabled server names.
        ``disabled_tools`` maps a server name to its disabled tool names;
        empty sets are dropped so the store stays minimal.
        """
        with self._lock:
            self._disabled_servers = set(disabled_servers or set())
            self._disabled_tools = {server: set(tools) for server, tools in (disabled_tools or {}).items() if tools}

    def seed_from_configs(self, configs: list) -> None:
        """Replace the selection with the one saved in the config file (UDR-0223 D6).

        ``configs`` are parsed ``MCPServerConfig`` entries; their ``disabled`` and
        ``disabled_tools`` fields are the saved selection.
        """
        self.set_selection(
            disabled_servers={c.name for c in configs if getattr(c, "disabled", False)},
            disabled_tools={c.name: set(getattr(c, "disabled_tools", []) or []) for c in configs},
        )

    def reset(self) -> None:
        """Clear all overrides (all MCP tools enabled). Used by tests."""
        with self._lock:
            self._disabled_servers = set()
            self._disabled_tools = {}

    def snapshot(self) -> dict[str, object]:
        """Return a deep-ish copy of the current selection for diagnostics."""
        with self._lock:
            return {
                "disabled_servers": set(self._disabled_servers),
                "disabled_tools": {s: set(t) for s, t in self._disabled_tools.items()},
            }


_store = McpOverrideStore()


def get_override_store() -> McpOverrideStore:
    """Return the process-wide MCP override store singleton."""
    return _store
