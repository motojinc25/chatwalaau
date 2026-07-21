"""Build a declarative ``kind: Prompt`` agent instance for a workflow node (CTR-0180).

A workflow's ``InvokeAzureAgent`` action references a declarative Prompt agent by
name; ChatWalaʻau -- not the YAML -- constructs that agent through the SAME provider
/ credential / tool path the AgentRegistry uses (CTR-0070 / CTR-0102), so the node
agent's model, options, and per-agent tool allow-list come from its OWN spec
(UDR-0101 D1/D4). This module owns that single-agent construction, reusing the
AgentRegistry building blocks (``_build_chat_client``, ``_build_tools_and_instructions``)
with the node's pinned spec.
"""

from __future__ import annotations

import logging

from agent_framework import Agent

from app import providers
from app.agent.compaction import resolve_compaction_strategy
from app.agent.declarative.loader import resolve_spec
from app.agent.declarative.spec import DeclarativeAgentError, DeclarativeAgentSpec
from app.agent.identity import load_identity
from app.agui.agent_registry import WEB_SEARCH_INSTRUCTION, _build_chat_client
from app.demo import is_demo_mode, resolve_demo_models

logger = logging.getLogger(__name__)


def _pick_model(spec: DeclarativeAgentSpec) -> str:
    """Choose the model for a node agent: the spec's preferred, else the default."""
    if is_demo_mode():
        models = list(resolve_demo_models())
        preferred = next((m for m in (spec.model_filter or []) if m in models), None)
        return preferred or (models[0] if models else "")
    configured = [m for m, _ in providers.resolve_models()]
    if not configured:
        raise DeclarativeAgentError(
            "No chat model is configured; author a 'chat' offering before running a workflow."
        )
    default = providers.resolve_default_model()
    if default not in configured:
        default = configured[0]
    if spec.model_filter:
        preferred = next((m for m in spec.model_filter if m in configured), None)
        if preferred:
            return preferred
    return default


def build_prompt_agent(agent_id: str, spec: DeclarativeAgentSpec | None = None) -> Agent:
    """Build an ``Agent`` for the declarative Prompt agent ``agent_id`` (UDR-0101 D4).

    The agent is constructed with the referenced spec's persona (Identity slot #1),
    model, options, structured-output default, and per-agent tool allow-list -- the
    same construction the AgentRegistry performs for the active agent, but pinned to
    THIS spec rather than the globally active one. Raises ``DeclarativeAgentError``
    when the id does not resolve or the spec carries a blocking warning.
    """
    if spec is None:
        spec = resolve_spec(agent_id)
    if spec.warnings:
        raise DeclarativeAgentError(
            f"Referenced agent {agent_id!r} has unresolved warnings: {spec.warnings}"
        )

    # Local import avoids the agent_factory <-> workflow import cycle.
    from app.agui.agent_factory import _build_tools_and_instructions

    model = _pick_model(spec)
    tools, context_providers, instructions, middleware = _build_tools_and_instructions(
        include_mcp=True,
        include_rag=True,
        apply_approval=True,
        spec=spec,
    )

    # _build_chat_client short-circuits to DemoChatClient in DEMO_MODE, so the
    # demo lane needs no special-casing here (UDR-0045 D7).
    client = _build_chat_client(model)
    model_options = None
    if not is_demo_mode():
        web_search = providers.web_search_tool(model)
        if web_search is not None:
            tools = [web_search, *tools]
            instructions = instructions + WEB_SEARCH_INSTRUCTION
        model_options = providers.build_model_options(model, spec.model_options_override)
        if spec.structured_output is not None:
            so = providers.build_structured_output(
                model,
                spec.structured_output.get("schema"),
                spec.structured_output.get("mode", "json_schema"),
            )
            model_options = providers.merge_generation_options(model_options, so)

    # Identity slot #1 (CTR-0104): the spec persona override, else the runtime
    # Global Agent Identity -- byte-for-byte the AgentRegistry._bake_instructions rule.
    baked = spec.instructions_override or load_identity()

    agent = Agent(
        name=f"ChatWalaau-Workflow-Node-{spec.id}",
        instructions=baked,
        client=client,
        tools=tools,
        context_providers=context_providers,
        default_options=model_options or None,
        compaction_strategy=resolve_compaction_strategy(),
        middleware=middleware or None,
    )
    logger.info("Workflow node agent built: id=%s model=%s", spec.id, model)
    return agent


__all__ = ["build_prompt_agent"]
