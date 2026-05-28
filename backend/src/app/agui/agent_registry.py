"""Multi-Model Agent Registry (CTR-0070, PRP-0035, PRP-0066).

Maintains one Agent instance per configured deployment name.
All agents share the same Tools, Skills, MCP tools, and context_providers.
Only the underlying ChatClient differs.

When DEMO_MODE=true (PRP-0066, UDR-0041), the underlying client is
replaced by DemoChatClient and the model list is taken from
DEMO_MODELS so the model selector UI surface is exercised without
any live Azure OpenAI deployment being configured.
"""

import logging
from typing import Any

from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient

from app.azure_credential import get_chat_client_credential_kwargs
from app.core.config import settings
from app.demo import is_demo_mode, resolve_demo_models

logger = logging.getLogger(__name__)


def _build_chat_client(model: str) -> Any:
    """Return a ChatClient for the model -- live or demo (PRP-0066, UDR-0041 D3).

    The MAF ``ChatClient`` Protocol already exists; no new seam is
    introduced. ``DemoChatClient`` is a fully MAF-compatible client so
    the Agent layer above is unaware of the swap.
    """
    if is_demo_mode():
        from app.demo.chat_client import DemoChatClient

        return DemoChatClient(model=model)
    return OpenAIChatClient(
        model=model,
        azure_endpoint=settings.azure_openai_endpoint or None,
        **get_chat_client_credential_kwargs(),
    )


class AgentRegistry:
    """Per-model Agent instance registry (CTR-0070).

    PRP-0067 / UDR-0042 D1: an optional ``compaction_strategy`` is
    passed through to every per-model Agent. The strategy is a stateless
    MAF object so sharing one instance across models is safe and matches
    the factory chokepoint pattern used for tools / context_providers.
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
            models = resolve_demo_models()
            self._default_model = models[0]
        else:
            models = settings.model_list
            self._default_model = settings.default_model

        self._configured_models = list(models)

        for model in models:
            client = _build_chat_client(model)

            # Per-model reasoning effort (CTR-0069):
            # Only models explicitly listed in REASONING_EFFORT get the parameter.
            # Models not listed receive None -> no reasoning option sent.
            # DEMO_MODE: skip reasoning options (DemoChatClient ignores them
            # and the model names don't match anything in REASONING_EFFORT).
            model_options: dict[str, Any] = {}
            if not is_demo_mode():
                effort = settings.get_reasoning_effort(model)
                if effort:
                    model_options["reasoning"] = {"effort": effort, "summary": "detailed"}

            agent = Agent(
                name=f"ChatWalaau-Agent-{model}",
                instructions=instructions,
                client=client,
                tools=tools,
                context_providers=context_providers,
                default_options=model_options or None,
                compaction_strategy=self._compaction_strategy,
            )
            self._agents[model] = agent
            logger.info(
                "Agent created for model: %s (demo=%s)",
                model,
                is_demo_mode(),
            )

        logger.info(
            "AgentRegistry initialized: %d model(s), default=%s, demo=%s",
            len(self._agents),
            self._default_model,
            is_demo_mode(),
        )

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
