"""Multi-Model + Multi-Provider Agent Registry (CTR-0070, PRP-0035, PRP-0066, PRP-0069).

Maintains one Agent instance per configured model. The model list spans
providers (Azure OpenAI + Anthropic) via ``app.providers.resolve_models()``
(PRP-0069, UDR-0045). All agents share the same base Tools, Skills, and
context_providers; the underlying ChatClient, the per-model generation /
reasoning ``default_options``, and the provider-supplied web search tool differ
per model (CTR-0102).

When DEMO_MODE=true (PRP-0066, UDR-0041), the underlying client is replaced by
DemoChatClient and the model list is taken from DEMO_MODELS. Demo behavior is
preserved byte-for-byte (UDR-0045 D7): provider dispatch is bypassed and demo
agents keep the OpenAI hosted web search tool + instruction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_framework import Agent

from app import providers
from app.agent.identity import build_capability_block, load_identity
from app.demo import is_demo_mode, resolve_demo_models

logger = logging.getLogger(__name__)

# Web search instruction fragment, appended per-model only when the model's
# provider supplies a web search tool (PRP-0069, UDR-0045 D5). Defined here --
# not in app.agui.agent_factory, which imports this module -- to avoid a
# circular import while letting both the registry and build_devui_agent reuse
# it. The leading space lets it append cleanly to the base instructions.
WEB_SEARCH_INSTRUCTION = (
    " You can search the web for up-to-date information. "
    "When you use web search results, include source links in your response "
    "as Markdown links: [source title](URL). "
    "Place citation links inline near the relevant text."
)


def _build_chat_client(model: str) -> Any:
    """Return a ChatClient for the model -- demo, or via provider dispatch.

    DEMO_MODE short-circuits to DemoChatClient before provider dispatch
    (UDR-0045 D7). Otherwise the owning provider (azure-openai / anthropic)
    constructs the MAF ChatClient (CTR-0102). The MAF ``ChatClient`` Protocol
    is the seam; no new abstraction is introduced (UDR-0045 D1).
    """
    if is_demo_mode():
        from app.demo.chat_client import DemoChatClient

        return DemoChatClient(model=model)
    return providers.build_chat_client(model)


class AgentRegistry:
    """Per-model Agent instance registry (CTR-0070).

    PRP-0067 / UDR-0042 D1: an optional ``compaction_strategy`` is passed
    through to every per-model Agent. The strategy is a stateless MAF object so
    sharing one instance across models is safe and matches the factory
    chokepoint pattern used for tools / context_providers.

    PRP-0069 / UDR-0045: the live model list and per-model client / options /
    web search tool are provider-aware via ``app.providers``.
    """

    def __init__(
        self,
        *,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
        compaction_strategy: Any | None = None,
    ) -> None:
        self._compaction_strategy = compaction_strategy
        # Serialises runtime rebuilds (PRP-0086, UDR-0064 D5). asyncio.Lock can be
        # constructed without a running event loop on Python 3.12 (it binds lazily),
        # which matters because the registry is created at import time.
        self._rebuild_lock = asyncio.Lock()
        self._install(self._build(tools, context_providers, instructions))

        logger.info(
            "AgentRegistry initialized: %d model(s), default=%s, demo=%s",
            len(self._agents),
            self._default_model,
            is_demo_mode(),
        )

    # ---- build / install (PRP-0086, UDR-0064 D2/D5) ----

    def _build(
        self,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
    ) -> tuple[dict[str, Agent], dict[str, str], list[str], str]:
        """Build a FRESH per-model agent map + capability map (pure; no self mutation).

        Returns ``(agents, capability_instructions, configured_models,
        default_model)`` so ``__init__`` and ``rebuild()`` can adopt the result
        with a single atomic swap (UDR-0064 D5).
        """
        if is_demo_mode():
            return self._build_demo_agents(tools, context_providers, instructions)
        return self._build_live_agents(tools, context_providers, instructions)

    def _install(
        self,
        built: tuple[dict[str, Agent], dict[str, str], list[str], str],
    ) -> None:
        """Adopt a freshly-built result atomically (UDR-0064 D5).

        Each assignment merely rebinds a reference, so a concurrent ``get()``
        observes either the complete old map or the complete new map -- never a
        partially-populated one.
        """
        agents, caps, models, default = built
        # Per-model capability instructions (slot #3..) feed the per-run remainder
        # (Memory Block + capabilities) when USER_PROFILE_ENABLED (CTR-0105).
        self._agents = agents
        self._capability_instructions = caps
        self._configured_models = models
        self._default_model = default

    async def rebuild(
        self,
        *,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
    ) -> None:
        """Rebuild all per-model agents with a new tool set and swap atomically.

        PRP-0086 / UDR-0064 D2/D5: builds a brand-new per-model map off to the
        side, then installs it under a single lock. ``get()`` always returns a
        fully-built agent; if the build raises, the prior agents stay installed (no
        partial swap). The compaction strategy is reused from construction.
        """
        async with self._rebuild_lock:
            built = self._build(tools, context_providers, instructions)
            self._install(built)
            logger.info(
                "AgentRegistry rebuilt: %d model(s), default=%s, demo=%s",
                len(built[0]),
                built[3],
                is_demo_mode(),
            )

    def _build_demo_agents(
        self,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
    ) -> tuple[dict[str, Agent], dict[str, str], list[str], str]:
        """Build demo agents (DemoChatClient), preserving pre-PRP-0069 behavior.

        UDR-0045 D7: demo agents keep the OpenAI hosted web search tool + the
        web search instruction exactly as before, and carry no per-provider
        reasoning options.
        """
        models = resolve_demo_models()
        configured = list(models)
        default = models[0]
        agents: dict[str, Agent] = {}
        caps: dict[str, str] = {}

        web_search = providers.openai_web_search_tool()
        demo_tools = [web_search, *tools]
        demo_caps = instructions + WEB_SEARCH_INSTRUCTION

        for model in models:
            caps[model] = demo_caps
            client = _build_chat_client(model)
            agent = Agent(
                name=f"ChatWalaau-Agent-{model}",
                instructions=self._bake_instructions(demo_caps),
                client=client,
                tools=demo_tools,
                context_providers=context_providers,
                default_options=None,
                compaction_strategy=self._compaction_strategy,
            )
            agents[model] = agent
            logger.info("Agent created for model: %s (demo=True)", model)
        return agents, caps, configured, default

    def _build_live_agents(
        self,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
    ) -> tuple[dict[str, Agent], dict[str, str], list[str], str]:
        """Build live agents via provider dispatch (PRP-0069, UDR-0045).

        Per model: the owning provider builds the ChatClient, the per-model
        ``default_options`` (OpenAI ``reasoning.effort`` vs Anthropic
        ``thinking`` + ``max_tokens``), and -- for OpenAI-family models only in
        v0.66.0 -- the hosted web search tool. The web search instruction is
        appended only when a web search tool is supplied.
        """
        resolved = providers.resolve_models()
        configured = [model for model, _ in resolved]
        default = configured[0] if configured else ""
        agents: dict[str, Agent] = {}
        caps: dict[str, str] = {}

        for model, provider_name in resolved:
            client = _build_chat_client(model)
            web_search = providers.web_search_tool(model)
            model_tools = [web_search, *tools] if web_search is not None else list(tools)
            model_caps = instructions + (WEB_SEARCH_INSTRUCTION if web_search is not None else "")
            caps[model] = model_caps
            model_options = providers.build_model_options(model)

            agent = Agent(
                name=f"ChatWalaau-Agent-{model}",
                instructions=self._bake_instructions(model_caps),
                client=client,
                tools=model_tools,
                context_providers=context_providers,
                default_options=model_options or None,
                compaction_strategy=self._compaction_strategy,
            )
            agents[model] = agent
            logger.info("Agent created for model: %s (provider=%s)", model, provider_name)
        return agents, caps, configured, default

    def get(self, model: str | None = None) -> Agent:
        """Get Agent for specified model. Falls back to default if None or unknown."""
        name = model or self._default_model
        agent = self._agents.get(name)
        if agent is None:
            logger.warning("Unknown model '%s', falling back to default '%s'", name, self._default_model)
            agent = self._agents.get(self._default_model)
        if agent is None:
            msg = f"No agent available. Configured models: {list(self._agents.keys())}"
            raise ValueError(msg)
        return agent

    def _bake_instructions(self, capability_instructions: str) -> str:
        """Return the agent-level (baked) instructions for one model (CTR-0105, CTR-0106).

        Always bake ONLY the Identity block (slot #1). The per-run remainder --
        the Memory Block (slot #2, when USER_PROFILE_ENABLED) plus the capability
        guidance, OR nothing for a Temporary Chat run -- is supplied via
        ``run_instructions()`` and concatenated by MAF AFTER the baked Identity,
        yielding Identity -> Memory -> capabilities (UDR-0051 D4). Both registry
        consumers (AG-UI endpoint CTR-0009, OpenAI API CTR-0057) always call
        ``run_instructions()``, so the assembled prompt is byte-for-byte the same
        as the prior bake-time assembly; a Temporary Chat run can then reduce the
        effective prompt to the Identity block alone (UDR-0052 D6) by supplying an
        empty remainder, which the prior conditional full-bake made impossible.
        """
        return load_identity()

    def run_instructions(
        self,
        model: str | None = None,
        *,
        user_profile_block: str | None = None,
        temporary: bool = False,
    ) -> str | None:
        """Return the per-run instruction remainder, or None (CTR-0105/CTR-0106).

        For a Temporary Chat run (``temporary=True``) returns None: the agent is
        baked Identity-only, so the effective system prompt is the Identity block
        alone -- no Memory Block and no capability guidance text (UDR-0052 D6).
        Tools stay registered (the memory tool no-ops via the temporary
        contextvar).

        Otherwise returns the Memory Block (when ``user_profile_block`` is given)
        followed by the model's capability guidance; merged into
        ``agent.run(options=...)`` it is appended by MAF AFTER the baked Identity,
        yielding Identity -> Memory -> capabilities. Returns None only if there is
        no remainder text at all.
        """
        if temporary:
            return None
        name = model or self._default_model
        caps = self._capability_instructions.get(name)
        if caps is None:
            caps = self._capability_instructions.get(self._default_model, "")
        return build_capability_block(caps, user_profile_block) or None

    @property
    def available_models(self) -> list[str]:
        """Return ordered list of configured model names (demo or live)."""
        return list(self._configured_models)

    @property
    def default_model(self) -> str:
        """Return the default model name."""
        return self._default_model
