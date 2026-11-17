"""Key-Bound API Agent (CTR-0286, PRP-0256, UDR-0237 D1/D2).

An API key may be bound to a custom declarative agent (``KeyRecord.agent``). A request with
that key runs an agent built from THAT spec -- its tool allow-list, knowledge scope,
persona and preferred model -- by the same construction as the workflow Prompt node
(``workflow.builder.build_prompt_agent``). It never reaches the registry's active-spec
agents or the unknown-model fallback (D1).

The built agent is cached per (agent id, YAML digest, model, registry generation), so an
edit of the YAML, a catalog / MCP / App Settings rebuild or a model change builds a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Any

from agent_framework import Agent

from app import providers
from app.agent.compaction import resolve_compaction_strategy
from app.agent.declarative.knowledge import KnowledgeScope, allows_web_search
from app.agent.declarative.loader import resolve_spec, source_digest
from app.agent.declarative.spec import CORE_AGENT_ID, DeclarativeAgentError, DeclarativeAgentSpec
from app.agent.identity import load_identity
from app.agent.model_capabilities import subset_for_model
from app.agui.agent_registry import _build_chat_client
from app.demo import is_demo_mode

logger = logging.getLogger(__name__)

#: Fixed texts (English, PRP-0256).
AGENT_UNAVAILABLE = "The agent assigned to this API key is not available."
MODEL_NOT_ALLOWED = 'This API key is bound to an agent; send model "chatwalaau".'
#: The model names a bound key may send.
GENERIC_MODEL = "chatwalaau"


class BoundAgentError(Exception):
    """A refusal in the OpenAI error shape."""

    def __init__(self, status: int, code: str, message: str, error_type: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.error_type = error_type

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.error_type, "code": self.code, "param": None}}


@dataclass(frozen=True)
class BoundAgent:
    agent_id: str
    model: str
    agent: Agent
    #: The slot-#3 tool guidance, supplied per run like the registry's remainder.
    capabilities: str
    knowledge: KnowledgeScope | None


def resolve_spec_for_key(agent_id: str) -> DeclarativeAgentSpec:
    """The bound spec, or 503 ``agent_unavailable``. Never falls back (D1)."""
    if not agent_id or agent_id == CORE_AGENT_ID:
        raise BoundAgentError(503, "agent_unavailable", AGENT_UNAVAILABLE, "server_error")
    try:
        spec = resolve_spec(agent_id)
    except DeclarativeAgentError:
        logger.warning("API key bound to agent %r, which does not resolve", agent_id)
        raise BoundAgentError(503, "agent_unavailable", AGENT_UNAVAILABLE, "server_error") from None
    if spec.warnings:
        logger.warning("API key bound to agent %r, which has blocking warnings: %s", agent_id, spec.warnings)
        raise BoundAgentError(503, "agent_unavailable", AGENT_UNAVAILABLE, "server_error")
    return spec


def check_model(requested: str | None, agent_id: str) -> None:
    """A bound key sends ``chatwalaau`` or the agent id; anything else is 400 (D1)."""
    if (requested or GENERIC_MODEL) not in (GENERIC_MODEL, agent_id):
        raise BoundAgentError(400, "model_not_allowed", MODEL_NOT_ALLOWED)


_cache: dict[tuple[str, str, str, int], BoundAgent] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def get_bound_agent(spec: DeclarativeAgentSpec, generation: int) -> BoundAgent:
    """The built agent for ``spec`` (cached; rebuilt when the YAML, model or registry changes)."""
    from app.workflow.builder import pick_model

    model = pick_model(spec)
    key = (spec.id, source_digest(spec.id) or "", model, generation)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        built = _build(spec, model)
        # Only the newest build per agent id is kept.
        for stale in [k for k in _cache if k[0] == spec.id]:
            del _cache[stale]
        _cache[key] = built
        return built


def _build(spec: DeclarativeAgentSpec, model: str) -> BoundAgent:
    # Local import avoids the agent_factory <-> openai_api import cycle.
    from app.agent.capability_guidance import render_capability_guidance
    from app.agui.agent_factory import _build_tools_and_instructions

    tools, context_providers, guidance, middleware = _build_tools_and_instructions(
        include_mcp=True,
        include_rag=True,
        spec=spec,
        include_computer_use=False,
    )
    client = _build_chat_client(model)
    default_options: dict[str, Any] = {}
    if is_demo_mode():
        capabilities = render_capability_guidance(guidance)
    else:
        surface = subset_for_model(model, tools, context_providers, guidance, allow_web_search=allows_web_search(spec))
        tools = surface.tools
        context_providers = surface.context_providers
        capabilities = surface.instructions()
        default_options = dict(providers.build_model_options(model, spec.model_options_override) or {})
        if spec.structured_output is not None:
            so = providers.build_structured_output(
                model, spec.structured_output.get("schema"), spec.structured_output.get("mode", "json_schema")
            )
            default_options = dict(providers.merge_generation_options(default_options, so) or {})
        # Client-managed conversation, as the registry does (PRP-0142).
        if providers.stores_responses_server_side(model):
            default_options.setdefault("store", False)

    agent = Agent(
        name=f"ChatWalaau-API-Agent-{spec.id}",
        instructions=spec.instructions_override or load_identity(),
        client=client,
        tools=tools,
        context_providers=context_providers,
        default_options=default_options or None,
        compaction_strategy=resolve_compaction_strategy(),
        middleware=middleware or None,
    )
    logger.info("Key-bound API agent built: id=%s model=%s tools=%d", spec.id, model, len(tools))
    return BoundAgent(spec.id, model, agent, capabilities, spec.knowledge)


__all__ = [
    "AGENT_UNAVAILABLE",
    "GENERIC_MODEL",
    "MODEL_NOT_ALLOWED",
    "BoundAgent",
    "BoundAgentError",
    "check_model",
    "clear_cache",
    "get_bound_agent",
    "resolve_spec_for_key",
]
