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

import logging
from typing import Any

from agent_framework import Agent

from app import providers
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
        self._agents: dict[str, Agent] = {}
        self._compaction_strategy = compaction_strategy

        if is_demo_mode():
            self._build_demo_agents(tools, context_providers, instructions)
        else:
            self._build_live_agents(tools, context_providers, instructions)

        logger.info(
            "AgentRegistry initialized: %d model(s), default=%s, demo=%s",
            len(self._agents),
            self._default_model,
            is_demo_mode(),
        )

    def _build_demo_agents(
        self,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
    ) -> None:
        """Build demo agents (DemoChatClient), preserving pre-PRP-0069 behavior.

        UDR-0045 D7: demo agents keep the OpenAI hosted web search tool + the
        web search instruction exactly as before, and carry no per-provider
        reasoning options.
        """
        models = resolve_demo_models()
        self._configured_models = list(models)
        self._default_model = models[0]

        web_search = providers.openai_web_search_tool()
        demo_tools = [web_search, *tools]
        demo_instructions = instructions + WEB_SEARCH_INSTRUCTION

        for model in models:
            client = _build_chat_client(model)
            agent = Agent(
                name=f"ChatWalaau-Agent-{model}",
                instructions=demo_instructions,
                client=client,
                tools=demo_tools,
                context_providers=context_providers,
                default_options=None,
                compaction_strategy=self._compaction_strategy,
            )
            self._agents[model] = agent
            logger.info("Agent created for model: %s (demo=True)", model)

    def _build_live_agents(
        self,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
    ) -> None:
        """Build live agents via provider dispatch (PRP-0069, UDR-0045).

        Per model: the owning provider builds the ChatClient, the per-model
        ``default_options`` (OpenAI ``reasoning.effort`` vs Anthropic
        ``thinking`` + ``max_tokens``), and -- for OpenAI-family models only in
        v0.66.0 -- the hosted web search tool. The web search instruction is
        appended only when a web search tool is supplied.
        """
        resolved = providers.resolve_models()
        self._configured_models = [model for model, _ in resolved]
        self._default_model = self._configured_models[0] if self._configured_models else ""

        for model, provider_name in resolved:
            client = _build_chat_client(model)
            web_search = providers.web_search_tool(model)
            model_tools = [web_search, *tools] if web_search is not None else list(tools)
            model_instructions = instructions + (WEB_SEARCH_INSTRUCTION if web_search is not None else "")
            model_options = providers.build_model_options(model)

            agent = Agent(
                name=f"ChatWalaau-Agent-{model}",
                instructions=model_instructions,
                client=client,
                tools=model_tools,
                context_providers=context_providers,
                default_options=model_options or None,
                compaction_strategy=self._compaction_strategy,
            )
            self._agents[model] = agent
            logger.info("Agent created for model: %s (provider=%s)", model, provider_name)

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

    @property
    def available_models(self) -> list[str]:
        """Return ordered list of configured model names (demo or live)."""
        return list(self._configured_models)

    @property
    def default_model(self) -> str:
        """Return the default model name."""
        return self._default_model
