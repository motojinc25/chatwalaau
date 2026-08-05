"""Jailed handlers for boundary-crossing workflow actions (CTR-0186, PRP-0121, UDR-0104).

The three declarative actions that cross the credential / provider / network boundary
-- ``InvokeFunctionTool`` / ``InvokeMcpTool`` / ``HttpRequestAction`` -- are honored
ONLY through the ChatWalaʻau-owned handlers built here, never from a raw YAML binding
(UDR-0104 D2). Each class is OFF by default and enabled independently via CTR-0006:

- ``WORKFLOW_FUNCTION_ACTIONS_ENABLED`` -> InvokeFunctionTool exposes only the
  deployment's agent-equivalent function tools.
- ``WORKFLOW_MCP_ACTIONS_ENABLED`` -> InvokeMcpTool reaches only MCP servers already
  configured (mcp_servers.jsonc) AND enabled in the runtime gating store (FEAT-0045).
- ``WORKFLOW_HTTP_ACTIONS_ENABLED`` -> HttpRequestAction runs behind a host allow-list
  + SSRF guard.

Defense in depth: the loader adds a BLOCKING WARNING for any occurrence of a disabled
class (so the workflow cannot be activated), and each handler ALSO re-checks its flag
at invocation time and refuses -- so even a stale compiled graph can never call out
through a disabled class. With all three flags unset the behavior is exactly UDR-0101
D6 (the action is a blocking warning; nothing is honored).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app.core.config import settings

if TYPE_CHECKING:
    # PUBLIC since the agent-framework-declarative 1.0.1 GA (PRP-0127, UDR-0110 D1).
    # These are the SAME objects the package previously exposed only under
    # `_workflows._http_handler` / `_workflows._mcp_handler`; an invariant test
    # pins that identity, so the move carried no behavior delta.
    from agent_framework_declarative import (
        HttpRequestInfo,
        HttpRequestResult,
        MCPToolInvocation,
        MCPToolResult,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Opt-in state (CTR-0006)
# ---------------------------------------------------------------------------
def enabled_action_classes() -> dict[str, bool]:
    """Return the opt-in state of the three boundary-crossing action classes."""
    return {
        "function": bool(settings.workflow_function_actions_enabled),
        "mcp": bool(settings.workflow_mcp_actions_enabled),
        "http": bool(settings.workflow_http_actions_enabled),
    }


# action kind -> (class key, env var name) for the blocking-warning message.
_BOUNDARY_ACTIONS = {
    "InvokeFunctionTool": ("function", "WORKFLOW_FUNCTION_ACTIONS_ENABLED"),
    "InvokeMcpTool": ("mcp", "WORKFLOW_MCP_ACTIONS_ENABLED"),
    "HttpRequestAction": ("http", "WORKFLOW_HTTP_ACTIONS_ENABLED"),
}


def boundary_action_warnings(action_kinds: list[str]) -> list[str]:
    """Return a blocking warning for each DISABLED boundary class present (UDR-0104 D2)."""
    enabled = enabled_action_classes()
    out: list[str] = []
    seen: set[str] = set()
    for kind in action_kinds:
        entry = _BOUNDARY_ACTIONS.get(kind)
        if entry is None:
            continue
        cls, env = entry
        if not enabled[cls] and kind not in seen:
            seen.add(kind)
            out.append(
                f"action kind {kind!r} crosses the credential / provider / network boundary "
                f"and is disabled by default; set {env}=true in the backend .env to enable it "
                "(runs only through the ChatWalaʻau jail)."
            )
    return out


# ---------------------------------------------------------------------------
# HTTP jail (HttpRequestAction)
# ---------------------------------------------------------------------------
def _allowed_hosts() -> set[str]:
    raw = (settings.workflow_http_allowed_hosts or "").strip()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_ssrf_target(host: str) -> bool:
    """True when ``host`` resolves to a loopback / private / link-local / metadata IP."""
    candidates: list[str] = []
    try:  # a literal IP host
        ipaddress.ip_address(host)
        candidates.append(host)
    except ValueError:
        # Cloud metadata endpoint by name is always refused.
        if host in {"metadata.google.internal", "metadata"}:
            return True
        try:
            infos = socket.getaddrinfo(host, None)
            candidates = [info[4][0] for info in infos]
        except OSError:
            # Cannot resolve -> treat as unsafe (fail closed).
            return True
    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
        # AWS/GCP/Azure IMDS
        if str(ip) == "169.254.169.254":
            return True
    return False


class _JailedHttpRequestHandler:
    """HttpRequestHandler that enforces opt-in + host allow-list + SSRF guard (UDR-0104 D2)."""

    def __init__(self) -> None:
        from agent_framework_declarative import DefaultHttpRequestHandler

        # Composition, NOT inheritance (UDR-0110 D5): the jail REPLACES the default
        # handler and delegates to it only after every gate has passed. Subclassing
        # would risk inheriting an unjailed code path through a method this class
        # never thought to override.
        self._default = DefaultHttpRequestHandler()

    async def send(self, info: HttpRequestInfo) -> HttpRequestResult:
        from agent_framework_declarative import HttpRequestResult

        def _deny(message: str) -> HttpRequestResult:
            logger.warning("workflow HttpRequestAction denied: %s (%s)", message, info.url)
            return HttpRequestResult(status_code=403, is_success_status_code=False, body=message)

        if not settings.workflow_http_actions_enabled:
            return _deny("HttpRequestAction is disabled (WORKFLOW_HTTP_ACTIONS_ENABLED=false).")

        host = (urlsplit(info.url).hostname or "").lower()
        if not host:
            return _deny("HttpRequestAction URL has no host.")
        allowed = _allowed_hosts()
        if host not in allowed:
            return _deny(f"host {host!r} is not in WORKFLOW_HTTP_ALLOWED_HOSTS (add it to allow this request).")
        if _is_ssrf_target(host):
            return _deny(f"host {host!r} resolves to a private / loopback / metadata address (blocked).")

        # Enforce the operator timeout when the YAML omits one.
        if info.timeout_ms is None:
            info.timeout_ms = max(1, int(settings.workflow_http_timeout_ms or 10000))
        return await self._default.send(info)


def build_http_request_handler() -> _JailedHttpRequestHandler:
    """Return the jailed HTTP handler (always injected so the workflow compiles; the

    handler refuses at call time when the class is disabled -- UDR-0104 D2).
    """
    return _JailedHttpRequestHandler()


# ---------------------------------------------------------------------------
# MCP jail (InvokeMcpTool)
# ---------------------------------------------------------------------------
def _configured_http_mcp_servers() -> dict[str, Any]:
    """Return {server_name: MCPServerConfig} for configured HTTP MCP servers."""
    try:
        from app.mcp.config import parse_mcp_config
        from app.mcp.lifecycle import _resolve_mcp_config_path

        path = _resolve_mcp_config_path()
        if path is None:
            return {}
        return {s.name: s for s in parse_mcp_config(path) if s.transport == "http" and s.url}
    except Exception:  # never break a run on a config read problem
        logger.debug("could not read MCP config for workflow jail", exc_info=True)
        return {}


class _JailedMcpToolHandler:
    """MCPToolHandler that reaches ONLY configured + gating-enabled servers (UDR-0104 D2).

    The ``server_url`` / ``headers`` from the YAML are IGNORED; the handler resolves the
    server strictly by ``server_label`` against mcp_servers.jsonc and substitutes the
    configured url + headers, then checks the runtime gating store (FEAT-0045) before
    delegating to the default MCP transport.
    """

    def __init__(self) -> None:
        from agent_framework_declarative import DefaultMCPToolHandler

        # Composition, NOT inheritance -- see _JailedHttpRequestHandler (UDR-0110 D5).
        self._default = DefaultMCPToolHandler()

    async def invoke_tool(self, invocation: MCPToolInvocation) -> MCPToolResult:
        from agent_framework import Content
        from agent_framework_declarative import MCPToolInvocation, MCPToolResult

        def _error(message: str) -> MCPToolResult:
            logger.warning("workflow InvokeMcpTool denied: %s", message)
            return MCPToolResult(outputs=[Content.from_text(f"Error: {message}")], is_error=True, error_message=message)

        if not settings.workflow_mcp_actions_enabled:
            return _error("InvokeMcpTool is disabled (WORKFLOW_MCP_ACTIONS_ENABLED=false).")

        label = (invocation.server_label or "").strip()
        if not label:
            return _error(
                "InvokeMcpTool requires a serverLabel naming a configured MCP server (raw serverUrl is not honored)."
            )

        servers = _configured_http_mcp_servers()
        server = servers.get(label)
        if server is None:
            return _error(f"MCP server {label!r} is not configured (only configured HTTP MCP servers may be invoked).")

        from app.mcp.overrides import get_override_store

        store = get_override_store()
        if store.server_disabled(label):
            return _error(f"MCP server {label!r} is disabled in the tool manager.")
        if not store.is_tool_enabled(label, invocation.tool_name):
            return _error(f"MCP tool {invocation.tool_name!r} on {label!r} is disabled in the tool manager.")

        # Re-point at the CONFIGURED url + headers (ignore any YAML-supplied ones).
        jailed = MCPToolInvocation(
            server_url=server.url,
            tool_name=invocation.tool_name,
            server_label=label,
            arguments=invocation.arguments,
            headers=dict(server.headers or {}),
        )
        return await self._default.invoke_tool(jailed)


def build_mcp_tool_handler() -> _JailedMcpToolHandler:
    """Return the jailed MCP handler (always injected; refuses at call time when disabled)."""
    return _JailedMcpToolHandler()


# ---------------------------------------------------------------------------
# Function-tool jail (InvokeFunctionTool)
# ---------------------------------------------------------------------------
def _agent_equivalent_tools() -> dict[str, Any]:
    """Return {name: callable} for the deployment's agent-equivalent function tools.

    Reuses the AgentRegistry tool-assembly chokepoint (the SAME surface an agent would
    call, incl. the CODING_ENABLED gate) and keeps only plain function tools (skips
    MCP / RAG / context-provider objects). Each is registered under its exposed name.
    """
    out: dict[str, Any] = {}
    try:
        from app.agui.agent_factory import _build_tools_and_instructions

        tools, _cp, _instr, _mw = _build_tools_and_instructions(
            include_mcp=False, include_rag=False, apply_approval=False
        )
    except Exception:
        logger.debug("could not assemble function tools for workflow jail", exc_info=True)
        return out
    for tool in tools:
        name = getattr(tool, "__name__", None) or getattr(tool, "name", None)
        if isinstance(name, str) and name and callable(tool):
            out.setdefault(name, tool)
    return out


def register_function_tools(factory: Any) -> None:
    """Register the deployment's function tools on ``factory`` for InvokeFunctionTool.

    Each tool is wrapped so that a call is refused when the class is disabled at
    invocation time (UDR-0104 D2 defense in depth). A no-op when no function tool can
    be exposed.
    """
    for name, tool in _agent_equivalent_tools().items():

        def _guarded(*args: Any, _tool: Any = tool, _name: str = name, **kwargs: Any) -> Any:
            if not settings.workflow_function_actions_enabled:
                raise PermissionError(
                    f"InvokeFunctionTool {_name!r} is disabled (WORKFLOW_FUNCTION_ACTIONS_ENABLED=false)."
                )
            return _tool(*args, **kwargs)

        try:
            factory.register_tool(name, _guarded)
        except Exception:  # a non-registerable tool object is simply skipped
            logger.debug("could not register function tool %r for workflow jail", name, exc_info=True)


__all__ = [
    "boundary_action_warnings",
    "build_http_request_handler",
    "build_mcp_tool_handler",
    "enabled_action_classes",
    "register_function_tools",
]
