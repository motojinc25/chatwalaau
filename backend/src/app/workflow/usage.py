"""Token usage of Declarative Workflow runs (PRP-0170, UDR-0152, CTR-0180 / CTR-0181).

A workflow node is a ChatWalaʻau-built Prompt agent that MAF's declarative executor runs
with a NON-streaming ``agent.run()`` and whose result it reduces to ``result.text``, so
the usage never reaches a WorkflowEvent. The only place the number exists is the node
agent itself -- which is where this module measures it (UDR-0152 D1).

Three parts, each with one job:

``NodeUsageRecorder``
    Attached ONLY by ``build_prompt_agent`` (the sole constructor of workflow node
    agents), so no other lane can be double-counted. Its chat middleware sees every
    model call of the node's tool loop -- verified against the production client layer
    order (``FunctionInvocationLayer`` outside ``ChatMiddlewareLayer``) in PRP-0170 V1.
    Its agent middleware brackets one ``agent.run()`` and, when a client has no chat
    middleware layer (``DemoChatClient``), falls back to the run's aggregated total with
    ``model_calls`` left absent (UDR-0152 D4). It never writes to the ledger.

``WorkflowUsageCollector``
    Run-scoped queue the recorder fills. Bound to the compiled workflow at compile time
    (D2) through a weak mapping, so it travels with the workflow -- including a paused
    human-in-the-loop run -- without a context variable.

``RunUsageAccount``
    Owned by the lane runner. ``drain`` attributes what is queued to the executor that
    just finished and appends ONE ledger record per node execution (D3); it also keeps
    the per-node rows the SPA ``usage`` event is built from (D7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
from typing import Any
import uuid
import weakref

from agent_framework import AgentMiddleware, ChatMiddleware, add_usage_details

from app import providers
from app.agui.token_usage import context_base_tokens, peak_node, sum_node_turns, turn_summary

logger = logging.getLogger(__name__)


@dataclass
class NodeUsageEntry:
    """What one ``agent.run()`` of one node agent consumed. Token counts only (UDR-0136 D3)."""

    agent: str
    model: str
    turn: dict[str, Any] | None = None
    model_calls: int = 0
    last: dict[str, Any] | None = None
    aggregated: bool = False

    def add_call(self, usage: dict[str, Any]) -> None:
        self.turn = dict(add_usage_details(self.turn, dict(usage)))  # type: ignore[arg-type]
        self.last = dict(usage)
        self.model_calls += 1

    @property
    def measured(self) -> bool:
        return bool(self.turn)


class WorkflowUsageCollector:
    """Run-scoped queue of finished node-agent runs, drained by the lane runner."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[NodeUsageEntry] = []

    def push(self, entry: NodeUsageEntry) -> None:
        if not entry.measured:
            return
        with self._lock:
            self._entries.append(entry)

    def drain(self) -> list[NodeUsageEntry]:
        with self._lock:
            entries, self._entries = self._entries, []
        return entries


# Collector <-> compiled workflow. Weak keys: a workflow that is dropped (a finished run,
# an evicted paused run) takes its collector with it.
_BOUND: weakref.WeakKeyDictionary[Any, WorkflowUsageCollector] = weakref.WeakKeyDictionary()


def bind_collector(workflow: Any, collector: WorkflowUsageCollector) -> None:
    try:
        _BOUND[workflow] = collector
    except TypeError:  # not weak-referenceable; nothing to measure against
        logger.debug("workflow object does not support weak references; usage not bound")


def collector_for(workflow: Any) -> WorkflowUsageCollector | None:
    try:
        return _BOUND.get(workflow)
    except TypeError:
        return None


class _NodeChatUsage(ChatMiddleware):
    """Per model call: fold the call's usage into the open run entry."""

    def __init__(self, recorder: NodeUsageRecorder) -> None:
        self._recorder = recorder

    async def process(self, context: Any, call_next: Any) -> None:
        await call_next()
        try:
            entry = self._recorder.current
            if entry is None or context.stream:
                return
            usage = getattr(context.result, "usage_details", None)
            if usage:
                entry.add_call(dict(usage))
        except Exception:
            logger.warning("workflow node usage capture failed", exc_info=True)


class _NodeAgentUsage(AgentMiddleware):
    """Per ``agent.run()``: open an entry, then hand it to the collector."""

    def __init__(self, recorder: NodeUsageRecorder) -> None:
        self._recorder = recorder

    async def process(self, context: Any, call_next: Any) -> None:
        entry = NodeUsageEntry(agent=self._recorder.agent, model=self._recorder.model)
        self._recorder.current = entry
        try:
            await call_next()
        finally:
            self._recorder.current = None
            try:
                if entry.model_calls == 0 and not context.stream:
                    usage = getattr(context.result, "usage_details", None)
                    if usage:
                        entry.turn = dict(usage)
                        entry.aggregated = True
                self._recorder.collector.push(entry)
            except Exception:
                logger.warning("workflow node usage hand-off failed", exc_info=True)


class NodeUsageRecorder:
    """The pair of middleware ``build_prompt_agent`` attaches to one node agent."""

    def __init__(self, collector: WorkflowUsageCollector, *, agent: str, model: str) -> None:
        self.collector = collector
        self.agent = agent
        self.model = model
        # Node agents of one workflow run sequentially within the action chain, so one
        # open entry per agent instance is sufficient.
        self.current: NodeUsageEntry | None = None

    def middleware(self) -> list[Any]:
        return [_NodeAgentUsage(self), _NodeChatUsage(self)]


def _node_summary(entry: NodeUsageEntry) -> tuple[dict[str, int] | None, int | None]:
    """(normalized node summary, context base) for one entry."""
    includes = providers.input_tokens_include_cache_read(entry.model)
    if entry.aggregated or entry.model_calls <= 0:
        summary = turn_summary(entry.turn, model_calls=1, includes_cache_read=includes)
        if summary is not None:
            summary.pop("model_calls", None)  # unknown -- never invented (UDR-0135 D7)
        return summary, None
    summary = turn_summary(entry.turn, model_calls=entry.model_calls, includes_cache_read=includes)
    return summary, context_base_tokens(entry.last, includes_cache_read=includes)


@dataclass
class RunUsageAccount:
    """The lane runner's view of one workflow run (spans HITL segments)."""

    lane: str
    run_target: str | None
    thread_id: str | None = None
    temporary: bool = False
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    nodes: list[dict[str, Any]] = field(default_factory=list)

    def drain(
        self,
        collector: WorkflowUsageCollector | None,
        *,
        node: str | None,
        label: str | None = None,
        outcome: str = "completed",
        sent_to_chat: bool | None = None,
    ) -> int:
        """Record everything queued, attributed to ``node``. Never raises (UDR-0136 D6).

        ``sent_to_chat`` marks whether this executor's output reached the chat message.
        That text is saved with the session and carried as history by the NEXT Prompt
        turn, so the SPA adds these nodes' output to its context estimate (UDR-0152 D8
        amendment). ``None`` (the job lane) leaves the row without the flag.
        """
        if collector is None:
            return 0
        try:
            entries = collector.drain()
            if not entries:
                return 0
            # Declarative actions run sequentially, so one drain holds one executor's work.
            # If it ever holds several agents, attribute by agent and omit the node (D3).
            mixed = len({e.agent for e in entries}) > 1
            from app.usage.ledger import append_workflow_node_usage

            for entry in entries:
                summary, base = _node_summary(entry)
                if summary is None:
                    continue
                attributed = None if mixed else node
                append_workflow_node_usage(
                    lane=self.lane,
                    turn=summary,
                    model=entry.model,
                    thread_id=self.thread_id,
                    temporary=self.temporary,
                    run_target=self.run_target,
                    node=attributed,
                    agent=entry.agent,
                    run_id=self.run_id,
                    outcome=outcome,
                )
                row: dict[str, Any] = {"agent": entry.agent, "model": entry.model, "turn": summary}
                if sent_to_chat is not None:
                    row["sent_to_chat"] = bool(sent_to_chat) and not mixed
                if attributed:
                    row["node"] = attributed
                    if label:
                        row["label"] = label
                if base is not None:
                    row["context_base_tokens"] = base
                    row["max_context_tokens"] = providers.get_max_context_tokens(entry.model)
                if entry.last and not entry.aggregated:
                    for key in ("input_token_count", "output_token_count"):
                        if isinstance(entry.last.get(key), int):
                            row[f"last_{key}"] = entry.last[key]
                self.nodes.append(row)
            return len(entries)
        except Exception:
            logger.warning("workflow usage drain failed", exc_info=True)
            return 0

    def usage_event_value(self) -> dict[str, Any] | None:
        """The CTR-0009-shaped ``usage`` value for this run, or None when nothing was measured."""
        if not self.nodes:
            return None
        value: dict[str, Any] = {}
        total = sum_node_turns([n["turn"] for n in self.nodes])
        if total is not None:
            value["turn"] = total
        peak = peak_node(self.nodes) or self.nodes[-1]
        value["model"] = peak.get("model")
        max_tokens = peak.get("max_context_tokens") or providers.get_max_context_tokens(peak.get("model"))
        value["max_context_tokens"] = max_tokens
        if "context_base_tokens" in peak:
            value["context_base_tokens"] = peak["context_base_tokens"]
        for key in ("input_token_count", "output_token_count"):
            if f"last_{key}" in peak:
                value[key] = peak[f"last_{key}"]
        value["workflow_nodes"] = [
            {k: v for k, v in n.items() if not k.startswith("last_") and k != "max_context_tokens"} for n in self.nodes
        ]
        return value


__all__ = [
    "NodeUsageEntry",
    "NodeUsageRecorder",
    "RunUsageAccount",
    "WorkflowUsageCollector",
    "bind_collector",
    "collector_for",
]
