"""Multi-Model Agent Registry (CTR-0070, PRP-0035).

Maintains one Agent instance per configured deployment name.
All agents share the same Tools, Skills, MCP tools, and context_providers.
Only the underlying OpenAIChatClient differs.
"""

import logging
from typing import Any

from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient
from azure.identity import AzureCliCredential

from app.core.config import settings

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Per-model Agent instance registry (CTR-0070)."""

    def __init__(
        self,
        *,
        tools: list[Any],
        context_providers: list[Any],
        instructions: str,
    ) -> None:
        self._agents: dict[str, Agent] = {}
        self._default_model = settings.default_model

        credential = AzureCliCredential()

        for model in settings.model_list:
            client = OpenAIChatClient(
                model=model,
                credential=credential,
                azure_endpoint=settings.azure_openai_endpoint or None,
            )

            # Per-model reasoning effort (CTR-0069):
            # Only models explicitly listed in REASONING_EFFORT get the parameter.
            # Models not listed receive None -> no reasoning option sent.
            model_options: dict[str, Any] = {}
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
            )
            self._agents[model] = agent
            logger.info(
                "Agent created for model: %s (reasoning=%s)",
                model,
                effort or "disabled",
            )

        logger.info(
            "AgentRegistry initialized: %d model(s), default=%s",
            len(self._agents),
            self._default_model,
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
        """Return ordered list of configured model names."""
        return settings.model_list

    @property
    def default_model(self) -> str:
        """Return the default model name."""
        return self._default_model
