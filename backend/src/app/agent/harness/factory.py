"""Harness Agent Factory (CTR-0193, PRP-0135, UDR-0119 D2/D4/D5/D7/D9).

Converts a valid :class:`HarnessAgentSpec` into ONE MAF ``create_harness_agent()``
call. This is the recorded construction-delegation seam: MAF owns ASSEMBLY, and
ChatWalaʻau owns every INPUT --

* ``client`` is ALWAYS built through the providers seam (CTR-0102) for the
  spec's single catalog offering (UDR-0119 D2); no other client path exists.
* ``tools`` resolve from the CTR-0178 identifier space (built-in function tools
  + whole MCP servers). ChatWalaʻau's coding tools are never mounted -- the
  harness-internal file / shell tools own that surface (UDR-0119 D7).
* Fixed phase-1 policies (UDR-0119 D4): history omitted (MAF-internal
  InMemoryHistoryProvider), default TodoProvider, default AgentModeProvider
  (initial mode from YAML), ``todos_remaining()`` loop clamped to the MAF cap,
  ``file_access_disable_readonly_tool_approval=True`` set EXPLICITLY.
* Workspace wiring (UDR-0119 D7): CODING_WORKSPACE_DIR-scoped file stores and a
  LocalShellTool; Skills through the SHARED provider (below).
* Skills ride ``skills_provider=create_skills_provider()`` -- the ONE construction
  path for a mounted SkillsProvider (CTR-0043, UDR-0130 D1). Handing MAF a bare
  directory string instead would make it build its own
  ``SkillsProvider.from_paths(...)``, which carries none of the four properties
  this project already fixed: the CODING_ENABLED-gated script runner (CTR-0043
  v3), the six-extension script filter (UDR-0086 D3), the Skills Management
  disabled set (UDR-0065 D2), and the per-agent allow-list (UDR-0100 D3).
  The factory returns ``None`` when SKILLS_DIR is unset, blank, or not a
  directory -- the condition UDR-0119 D7 states and the shape MAF's
  ``if skills_provider:`` opt-in expects, so the wiring stays conditional with no
  helper of its own. The skills APPROVAL middleware is deliberately NOT attached
  here -- UDR-0119 D6 keeps one approval coordinator, so skill tools stay
  ``always_require`` and every request reaches the FEAT-0028 card
  (UDR-0130 D5).
* The per-offering web-search capability gate (UDR-0119 D5) forces
  ``disable_web_search=True`` when the offering is withheld.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app import providers
from app.agent.harness.mapping import HARNESS_MAX_ITERATIONS
from app.agent.harness.spec import IDENTITY_SENTINEL, HarnessAgentError, HarnessAgentSpec
from app.core.config import settings
from app.skills.provider import create_skills_provider

logger = logging.getLogger(__name__)

# Domain default appended after the harness instructions when the YAML sets no
# ``instructions.agent`` (the operator-specified default; the pinned MAF has no
# DEFAULT_AGENT_INSTRUCTIONS constant of its own -- ``None`` there means "no
# agent instructions at all", which is NOT the requested behavior).
DEFAULT_AGENT_INSTRUCTIONS = (
    "You are a software engineering agent.\n"
    "Inspect the existing codebase before making changes.\n"
    "Implement requested changes completely and minimally.\n"
    "Follow the project's existing architecture and conventions.\n"
    "Run the relevant build, tests, and static checks after changes.\n"
    "Do not claim success unless verification passes."
)

# Subdirectories under CODING_WORKSPACE_DIR for the harness stores (UDR-0119 D7).
FILE_MEMORY_SUBDIR = "agent-file-memory"
FILE_ACCESS_SUBDIR = "agent-file-access"

# Application default output-token budget for compaction (PRP-0144, UDR-0125 D3,
# amending UDR-0119 D9). A backend constant, deliberately NOT an env var -- the
# UDR-0094 D5 precedent, where DEFAULT_CONTEXT_WINDOW replaced an env-configurable
# equivalent; the operator knob is the per-agent `compaction.maxOutputTokens`.
#
# It exists because MAF builds NO compaction strategy unless BOTH token budgets
# are present (_harness/_agent.py:119) and validates only the window
# (_agent.py:530-539). An unset maxOutputTokens therefore disabled compaction
# silently, while the GUI toggle, the CTR-0194 policy summary, and the YAML
# template published in PRP-0135 all reported it as ON. An unset budget now means
# "resolve to the application default", never "disable the feature".
#
# In the pinned MAF this value feeds _assemble_compaction and nothing else: it
# sets WHERE compaction fires, never how much the model may generate.
DEFAULT_HARNESS_MAX_OUTPUT_TOKENS = 32_768


def resolve_compaction_budget(spec: HarnessAgentSpec) -> tuple[int | None, int | None, str]:
    """Resolve (max_context_window_tokens, max_output_tokens, source) for ``spec``.

    Returns ``(None, None, "disabled")`` when the YAML disables compaction. The
    ``min(..., window // 8)`` clamp guarantees MAF's precondition
    ``0 < max_output < max_window`` for any catalog window -- including a small
    one -- without a second validation path.

    ``source`` reports where the numbers came from for the CTR-0192 policy
    summary (UDR-0125 D4): ``catalog`` (both resolved by the application),
    ``yaml`` (both declared), or ``mixed``.
    """
    if spec.compaction_disabled:
        return None, None, "disabled"
    window = spec.max_context_window_tokens or providers.get_max_context_tokens(spec.model_id)
    output = spec.max_output_tokens or min(DEFAULT_HARNESS_MAX_OUTPUT_TOKENS, max(window // 8, 1))
    declared = (spec.max_context_window_tokens is not None, spec.max_output_tokens is not None)
    source = {(True, True): "yaml", (False, False): "catalog"}.get(declared, "mixed")
    return window, output, source


def _workspace_dir() -> Path | None:
    raw = (settings.coding_workspace_dir or "").strip()
    if not raw:
        return None
    return Path(raw)


def _resolve_function_tools(names: list[str]) -> list[Any]:
    """Resolve built-in function-tool names to callables (UDR-0119 D2).

    Mirrors the availability gates of the assembly chokepoint
    (``app.agui.agent_factory._build_tools_and_instructions``) via the CTR-0178
    static registry: a recognized-but-gated-off name contributes no tool (the
    declarative-lane convention); coding tools were already rejected by the
    mapping (UDR-0119 D7).
    """
    from app.agent.declarative.tool_inventory import BUILTIN_FUNCTION_TOOLS

    available = {t.name for t in BUILTIN_FUNCTION_TOOLS if t.available()}
    out: list[Any] = []
    for name in names:
        if name not in available:
            logger.info("Harness tool function:%s is gated off; contributing no tool.", name)
            continue
        fn = _import_function_tool(name)
        if fn is not None:
            out.append(fn)
    return out


def _import_function_tool(name: str) -> Any | None:
    """Import one built-in function tool by callable name (lazy, gate-checked)."""
    try:
        if name.startswith("weather_"):
            from app.weather import tools as weather_tools

            return getattr(weather_tools, name, None)
        if name == "rag_search":
            from app.rag.tools import init_rag_search, rag_search

            init_rag_search(
                chroma_dir=settings.chroma_dir,
                collection_name=settings.rag_collection_name,
                top_k=settings.rag_top_k,
            )
            return rag_search
        if name in ("generate_image", "edit_image"):
            from app.image_gen import tools as image_tools

            return getattr(image_tools, name, None)
        if name == "manage_user_memory":
            from app.agent.user_memory import manage_user_memory

            return manage_user_memory
        if name == "manage_memory":
            from app.agent.agent_memory import manage_memory

            return manage_memory
        if name == "manage_cron":
            from app.cron.tool import manage_cron

            return manage_cron
        if name == "manage_pipeline":
            from app.pipeline.tool import manage_pipeline

            return manage_pipeline
        if name == "manage_webhook":
            from app.webhook.tool import manage_webhook

            return manage_webhook
        if name == "query_ontology":
            from app.ontology.tool import query_ontology

            return query_ontology
    except Exception:
        logger.warning("Harness tool function:%s failed to import; skipped.", name, exc_info=True)
        return None
    logger.info("Harness tool function:%s is not a known built-in; skipped.", name)
    return None


def _resolve_mcp_tools(servers: list[str]) -> list[Any]:
    """Resolve whole-server MCP selections to the SHARED MCPTool instances.

    Only whole servers are selectable for harness agents (the mapping rejects
    per-tool ids) so the shared instances are passed WITHOUT mutating their
    ``allowed_tools`` -- the exposure follows the MCP override store (CTR-0121)
    exactly as it does for the registry agents. A disabled or unknown server
    contributes no tool.
    """
    if not servers:
        return []
    try:
        from app.mcp.lifecycle import get_mcp_tools
        from app.mcp.overrides import get_override_store
    except Exception:  # MCP subsystem unavailable
        return []
    store = get_override_store()
    wanted = set(servers)
    out: list[Any] = []
    for tool in get_mcp_tools():
        server = getattr(tool, "name", "") or ""
        if server in wanted and not store.server_disabled(server):
            out.append(tool)
    return out


def resolve_tools(spec: HarnessAgentSpec) -> list[Any]:
    """Resolve the YAML allow-list to tool instances (CTR-0178 identifier space)."""
    fn_names: list[str] = []
    mcp_servers: list[str] = []
    for ident in spec.tool_allowlist:
        kind, _, rest = ident.partition(":")
        if kind == "function" and rest:
            fn_names.append(rest)
        elif kind == "mcp" and rest:
            mcp_servers.append(rest)
    return [*_resolve_function_tools(fn_names), *_resolve_mcp_tools(mcp_servers)]


def _resolve_agent_instructions(spec: HarnessAgentSpec) -> str:
    """Resolve ``instructions.agent``: None -> the domain default;
    ``"=Identity"`` -> the Global Agent Identity (UDR-0072 D6 convention)."""
    raw = spec.agent_instructions
    if raw is None:
        return DEFAULT_AGENT_INSTRUCTIONS
    if raw.strip() == IDENTITY_SENTINEL:
        from app.agent.identity import load_identity

        return load_identity()
    return raw


def web_search_withheld(model_id: str) -> bool:
    """True when the offering's hosted web search is withheld (UDR-0119 D5).

    ``providers.web_search_tool()`` returning None is the ONE gate for hosted
    web search (UDR-0112 D3); the harness factory consults the same gate so a
    deployment restriction PRP-0129 closed can never resurface through a
    harness run.
    """
    try:
        return providers.web_search_tool(model_id) is None
    except Exception:
        logger.warning("Web search gate probe failed for %s; withholding.", model_id, exc_info=True)
        return True


class HarnessRuntime:
    """One built harness runtime: the MAF Agent plus its owned shell executor.

    The shell is kept alongside the agent so the per-conversation cache
    (``app.agent.harness.runtime``) can close the persistent shell process on
    eviction (LocalShellTool is single-session by contract).
    """

    def __init__(self, agent: Any, shell_executor: Any | None) -> None:
        self.agent = agent
        self.shell_executor = shell_executor

    async def aclose(self) -> None:
        """Best-effort cleanup of the persistent shell process."""
        shell = self.shell_executor
        if shell is None:
            return
        try:
            aclose = getattr(shell, "aclose", None)
            if callable(aclose):
                await aclose()
            else:
                aexit = getattr(shell, "__aexit__", None)
                if callable(aexit):
                    await aexit(None, None, None)
        except Exception:
            logger.debug("Harness shell cleanup failed", exc_info=True)


def build_harness_agent(spec: HarnessAgentSpec) -> Any:
    """Build the runtime MAF Agent for one harness spec (CTR-0193).

    Thin wrapper over :func:`build_harness_runtime` for callers (and invariant
    tests) that only need the Agent.
    """
    return build_harness_runtime(spec).agent


def build_harness_runtime(spec: HarnessAgentSpec) -> HarnessRuntime:
    """Build the runtime for one harness spec (CTR-0193).

    Raises :class:`HarnessAgentError` for a spec with blocking warnings, an
    unbuildable client, or DEMO_MODE (UDR-0119 D7/D8). Never returns a
    partially wired agent.
    """
    from app.demo import is_demo_mode

    if is_demo_mode():
        raise HarnessAgentError("Harness agents are not runnable in demo mode.")
    if spec.warnings:
        raise HarnessAgentError(
            "This harness agent cannot run until its warnings are resolved: " + "; ".join(spec.warnings)
        )
    if not spec.model_id:
        raise HarnessAgentError("model.id is required to build a harness agent.")

    from agent_framework import (
        AgentModeProvider,
        FileSystemAgentFileStore,
        create_harness_agent,
        todos_remaining,
    )

    # The client is ALWAYS provider-built (CTR-0102) -- the UDR-0119 D2 invariant.
    client = providers.build_chat_client(spec.model_id)

    tools = resolve_tools(spec)

    # Workspace-scoped capabilities (UDR-0119 D7): omitted entirely when
    # CODING_WORKSPACE_DIR is unset (the harness degrades gracefully).
    workspace = _workspace_dir()
    file_memory_store = None
    file_access_store = None
    shell_executor = None
    if workspace is not None:
        if not spec.file_memory_disabled:
            file_memory_store = FileSystemAgentFileStore(workspace / FILE_MEMORY_SUBDIR)
        file_access_store = FileSystemAgentFileStore(workspace / FILE_ACCESS_SUBDIR)
        try:
            from app.agent.harness.shell import WorkspaceShellTool

            # A THREAD-based shell executor, not MAF's asyncio one: under uvicorn
            # the running loop is a SelectorEventLoop, which on Windows raises
            # NotImplementedError for subprocess transports -- and the shell
            # environment probe runs in before_run, so an asyncio shell killed
            # EVERY harness turn before the first token. See app.agent.harness.shell
            # (the app.cron.executor / CTR-0031 technique). Default
            # approval_mode="always_require" keeps shell commands on the FEAT-0028
            # approval card (UDR-0119 D6).
            shell_executor = WorkspaceShellTool(workdir=workspace)
        except Exception:
            logger.warning("Workspace shell unavailable; harness runs without shell.", exc_info=True)

    # Per-offering hosted web search gate (UDR-0119 D5): forced-safe, never an error.
    disable_web_search = spec.web_search_disabled
    if not disable_web_search and web_search_withheld(spec.model_id):
        logger.info(
            "Harness %s: web search withheld for offering %s (capability gate); disabled.",
            spec.id,
            spec.model_id,
        )
        disable_web_search = True

    # Token budgets from the Model Offering Catalog (UDR-0119 D9 as amended by
    # PRP-0144 / UDR-0125 D3): an unset maxOutputTokens resolves to the
    # application default, never to a disabled feature.
    # Built ONCE (UDR-0130 D1): the factory snapshots the Skills override store and
    # refreshes the live-build set (set_loaded_skills, CTR-0123 / UDR-0068 D4), so a
    # second call would repeat a side effect for a value the build already holds.
    skills_provider = create_skills_provider()

    max_window, max_output, _budget_source = resolve_compaction_budget(spec)
    # Canary, unreachable by construction above. A declared-enabled compaction
    # that resolved to no strategy MUST fail loudly at build time rather than
    # ship an agent that quietly does not compact (UDR-0125 D3).
    if not spec.compaction_disabled and (max_window is None or max_output is None):
        raise HarnessAgentError(
            f"Harness {spec.id}: compaction is enabled but its token budget did not resolve "
            f"(window={max_window}, max_output={max_output}); MAF would build no strategy."
        )

    # Initial mode (UDR-0119 D4): only a custom provider when the YAML chose one;
    # otherwise MAF's default provider (identical modes, MAF default initial).
    mode_provider = None
    if spec.mode_initial and not spec.mode_disabled:
        mode_provider = AgentModeProvider(default_mode=spec.mode_initial)

    agent = create_harness_agent(
        client,
        name=spec.name,
        description=spec.description or None,
        harness_instructions=spec.harness_instructions,
        agent_instructions=_resolve_agent_instructions(spec),
        tools=tools or None,
        max_context_window_tokens=max_window,
        max_output_tokens=max_output,
        # history_provider omitted => MAF-internal InMemoryHistoryProvider (D4).
        # PRP-0148 Section 4.1 needs to TRACE that provider's loads, and does it by
        # attaching an observer to MAF's own instance after construction
        # (`history_debug.attach_history_tracing`, called from the runtime cache)
        # rather than by passing one in: UDR-0119 D4's "parameter omitted" is pinned
        # by PRP-0135 / PRP-0144 invariants, and observability must not require
        # relaxing a shipped decision.
        disable_compaction=spec.compaction_disabled,
        disable_todo=spec.todo_disabled,
        disable_mode=spec.mode_disabled,
        mode_provider=mode_provider,
        disable_file_memory=spec.file_memory_disabled or file_memory_store is None,
        file_memory_store=file_memory_store,
        file_access_store=file_access_store,
        file_access_disable_write_tools=spec.file_access_disable_write_tools,
        # EXPLICIT True (the MAF default is False): readonly tools never prompt (D4/D6).
        file_access_disable_readonly_tool_approval=True,
        file_access_disable_write_tool_approval=spec.file_access_disable_write_tool_approval,
        # UDR-0130 D1: the SHARED provider, never skills_paths (see module doc).
        skills_provider=skills_provider,
        shell_executor=shell_executor,
        disable_web_search=disable_web_search,
        # ONE approval coordinator, always (UDR-0119 D6). MAF's harness
        # ToolApprovalMiddleware is a SECOND, session-state-backed coordinator: it
        # queues approval requests and RE-INJECTS collected approval responses into
        # the next call's messages. ChatWalaʻau's AG-UI approval loop (FEAT-0028)
        # does the same job on the same conversation, so wiring both makes the two
        # replay the same approvals -- and the Responses API rejects the result:
        #   400 "The following MCP approval requests have approval responses but
        #        weren't passed as input: call_..."
        # (reproduced via "approve for this session", which resolves instantly and
        # collides reliably). The tools keep approval_mode="always_require", so
        # every request still reaches the FEAT-0028 card; the harness loop
        # middleware's approval escape hatch returns the pending request to us,
        # which is exactly the host-driven flow D6 specifies.
        disable_tool_auto_approval=True,
        loop_should_continue=todos_remaining(),
        loop_max_iterations=min(spec.loop_max_iterations or HARNESS_MAX_ITERATIONS, HARNESS_MAX_ITERATIONS),
        # CLIENT-MANAGED CONVERSATION (UDR-0119 D4). The harness is built for it:
        # it sets require_per_service_call_history_persistence=True and ships an
        # InMemoryHistoryProvider with load_messages=True. But
        # OpenAIChatClient.STORES_BY_DEFAULT is True, and MAF resolves
        # "service_stores_history" as the explicit store option when set and the
        # client's STORES_BY_DEFAULT otherwise (_agents.py:1284-1294) -- so with
        # `store` left unset the SERVICE owned history:
        # the harness's own provider was skipped -- MAF says so on every run
        # ("HistoryProvider 'in_memory' has load_messages=True but the chat client
        # stores history server-side ... Set store=False to load from the
        # provider") -- and each turn's items became server-side references.
        #
        # That mismatch is what broke approval resume: a harness tool reaches the
        # provider as a SERVER-SIDE tool (the local shell), so its approval is a
        # stored `mcp_approval_request` item, and the operator's
        # `mcp_approval_response` referenced an id the service could no longer
        # resolve once the approval handshake interrupted the chain:
        #   400 The following MCP approval requests have approval responses but
        #       weren't passed as input: call_...
        # store=False removes the class of problem rather than one instance of it:
        # the conversation is carried in the request, the harness's history
        # provider loads as designed, and compaction finally sees the history it
        # is meant to compact.
        default_options={"store": False},
    )
    # The resolved compaction budget is logged so "is compaction actually
    # configured for this agent" is answerable from the log alone (UDR-0125 D3);
    # the two thresholds are ContextWindowCompactionStrategy's defaults applied
    # to the input budget (window - max_output).
    if max_window is not None and max_output is not None:
        _input_budget = max_window - max_output
        _compaction_desc = (
            f"on window={max_window} max_output={max_output} "
            f"evict_at={int(_input_budget * 0.5)} truncate_at={int(_input_budget * 0.8)}"
        )
    else:
        _compaction_desc = "off"
    logger.info(
        "Harness agent built: id=%s model=%s tools=%d web_search=%s workspace=%s skills=%s compaction=%s",
        spec.id,
        spec.model_id,
        len(tools),
        not disable_web_search,
        workspace is not None,
        skills_provider is not None,
        _compaction_desc,
    )
    return HarnessRuntime(agent, shell_executor)


def preflight(spec: HarnessAgentSpec) -> dict:
    """Dry-run resolution for validation (CTR-0193): no client, no agent, no run.

    Returns the resolved policy summary plus the tool identifiers that would
    contribute a tool at build time. Used by CTR-0194 / CTR-0195 validation.
    """
    from app.agent.harness.loader import policy_summary

    resolved: list[str] = []
    try:
        from app.agent.declarative.tool_inventory import BUILTIN_FUNCTION_TOOLS

        available_fns = {t.name for t in BUILTIN_FUNCTION_TOOLS if t.available()}
        for ident in spec.tool_allowlist:
            kind, _, rest = ident.partition(":")
            if (kind == "function" and rest in available_fns) or (kind == "mcp" and rest):
                resolved.append(ident)
    except Exception:
        logger.debug("Harness preflight tool resolution failed", exc_info=True)
    return {"policy": policy_summary(spec), "resolved_tools": resolved}


__all__ = [
    "DEFAULT_AGENT_INSTRUCTIONS",
    "DEFAULT_HARNESS_MAX_OUTPUT_TOKENS",
    "FILE_ACCESS_SUBDIR",
    "FILE_MEMORY_SUBDIR",
    "HarnessRuntime",
    "build_harness_agent",
    "build_harness_runtime",
    "preflight",
    "resolve_compaction_budget",
    "resolve_tools",
    "web_search_withheld",
]
