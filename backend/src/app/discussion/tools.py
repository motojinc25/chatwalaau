"""AI Discussion research tools (CTR-0275, PRP-0234, UDR-0217 D1-D3).

A participant seat may use read-only research tools from ONE code allowlist: hosted web search,
the document search (RAG) and the ontology query tool. Nothing else can be mounted -- no MCP,
coding, memory, automation, image, decision or Computer Use tool, and no context provider (D1).
The moderator never gets a tool.

Each turn builds its own ``TurnTools``: the function tools are wrapped so that a turn makes at
most ``calls_per_turn`` calls and every result is cut to ``result_max_chars`` (D2). What the
turn used is recorded (name and query only, never the result) for the stored message (D3); the
results themselves live only inside the turn.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

WEB_SEARCH = "web_search"
RAG_SEARCH = "rag_search"
ONTOLOGY_QUERY = "ontology_query"
#: The allowlist (D1). An invariant pins it; add nothing that writes or has side effects.
TOOL_IDS: tuple[str, ...] = (WEB_SEARCH, RAG_SEARCH, ONTOLOGY_QUERY)
TOOL_LABELS: dict[str, str] = {WEB_SEARCH: "Web search", RAG_SEARCH: "Documents", ONTOLOGY_QUERY: "Ontology"}

LIMIT_REACHED = "Tool limit reached for this turn; answer with what you have."
TRUNCATED = "\n[truncated]"
#: The query recorded on the message is at most this long (D3).
QUERY_CHARS = 200


class ToolError(ValueError):
    """A requested tool id that is unknown or not available for the seat's model."""


def _scripted() -> bool:
    from app.discussion.speakers import use_scripted_replies

    return use_scripted_replies()


def _function_available(tool_id: str) -> bool:
    if _scripted():
        return True  # scripted turns never call a tool; the browser tests need the choice
    from app.agent.declarative.tool_inventory import BUILTIN_FUNCTION_TOOLS

    return any(t.name == tool_id and t.available() for t in BUILTIN_FUNCTION_TOOLS)


def web_search_available(model: str) -> bool:
    """The ONE gate for hosted web search (UDR-0112 D3), applied to the seat's model."""
    if _scripted():
        return True
    from app.agent.harness.factory import web_search_withheld

    return not web_search_withheld(model)


def web_search_models(models: list[str]) -> list[str]:
    return [m for m in models if web_search_available(m)]


def catalog() -> list[dict[str, Any]]:
    """``[{id, label, available}]`` for the setup dialog; web search depends on the seat's model."""
    out: list[dict[str, Any]] = []
    for tool_id in TOOL_IDS:
        available = True if tool_id == WEB_SEARCH else _function_available(tool_id)
        out.append({"id": tool_id, "label": TOOL_LABELS[tool_id], "available": available})
    return out


def check_tools(requested: Any, model: str) -> list[str]:
    """The seat's tool ids, validated and de-duplicated in allowlist order (D1)."""
    if requested is None:
        return []
    if not isinstance(requested, list):
        raise ToolError("tools must be a list")
    wanted = {str(t) for t in requested}
    unknown = sorted(wanted - set(TOOL_IDS))
    if unknown:
        raise ToolError(f"unknown tool {unknown[0]!r}")
    for tool_id in wanted:
        ok = web_search_available(model) if tool_id == WEB_SEARCH else _function_available(tool_id)
        if not ok:
            raise ToolError(f"tool {tool_id!r} is not available for model {model!r}")
    return [t for t in TOOL_IDS if t in wanted]


def _import(tool_id: str) -> Any:
    if tool_id == RAG_SEARCH:
        from app.core.config import settings
        from app.rag.tools import init_rag_search, rag_search

        init_rag_search(
            chroma_dir=settings.chroma_dir, collection_name=settings.rag_collection_name, top_k=settings.rag_top_k
        )
        return rag_search
    from app.ontology.tool import ontology_query

    return ontology_query


def _query_of(tool_id: str, kwargs: dict[str, Any]) -> str:
    key = "query" if tool_id == RAG_SEARCH else "question"
    value = kwargs.get(key) or kwargs.get("action") or ""
    return str(value)[:QUERY_CHARS]


class TurnTools:
    """The tools of ONE participant turn, with its call counter and its record (D2 / D3)."""

    def __init__(self, tool_ids: list[str], *, model: str, calls_per_turn: int, result_max_chars: int) -> None:
        self.tool_ids = [t for t in tool_ids if t in TOOL_IDS]
        self.model = model
        self.calls_per_turn = max(int(calls_per_turn), 0)
        self.result_max_chars = max(int(result_max_chars), 1)
        self.calls = 0
        self.used: list[dict[str, str]] = []

    @property
    def enabled(self) -> bool:
        return bool(self.tool_ids) and self.calls_per_turn > 0

    def allowance_tokens(self) -> int:
        """The pre-call estimate's share for tool results (D2): calls x result length / 3."""
        return self.calls_per_turn * self.result_max_chars // 3 if self.enabled else 0

    def _wrap(self, tool_id: str, fn: Any) -> Any:
        from agent_framework import tool

        @functools.wraps(fn)
        async def bounded(*args: Any, **kwargs: Any) -> str:
            if self.calls >= self.calls_per_turn:
                return LIMIT_REACHED
            self.calls += 1
            self.used.append({"name": tool_id, "query": _query_of(tool_id, kwargs)})
            value = fn(*args, **kwargs)
            if inspect.isawaitable(value):
                value = await value
            text = value if isinstance(value, str) else str(value)
            if len(text) > self.result_max_chars:
                text = text[: self.result_max_chars] + TRUNCATED
            return text

        return tool(bounded, approval_mode="never_require")

    def build(self) -> list[Any]:
        """The tool objects for ``Agent(tools=...)``; an unavailable tool contributes nothing."""
        if not self.enabled:
            return []
        out: list[Any] = []
        for tool_id in self.tool_ids:
            try:
                if tool_id == WEB_SEARCH:
                    hosted = _hosted_web_search(self.model, self.calls_per_turn)
                    if hosted is not None:
                        out.append(hosted)
                    continue
                out.append(self._wrap(tool_id, _import(tool_id)))
            except Exception:
                logger.warning("discussion tool %s could not be mounted; skipped", tool_id, exc_info=True)
        return out

    def note_hosted(self, name: str, call_id: str) -> None:
        """Record one hosted web search call seen in the stream (its query is not visible)."""
        if not any(u.get("call_id") == call_id for u in self.used if call_id):
            self.used.append({"name": WEB_SEARCH, "query": "", **({"call_id": call_id} if call_id else {})})

    def record(self) -> list[dict[str, str]]:
        return [{"name": u["name"], "query": u.get("query", "")} for u in self.used]


def _hosted_web_search(model: str, max_uses: int) -> Any | None:
    """The provider's hosted web search, capped where the provider takes a per-request limit."""
    from app import providers

    hosted = providers.web_search_tool(model)
    if hosted is None:
        return None
    if isinstance(hosted, dict) and str(hosted.get("type", "")).startswith("web_search_2"):
        # Anthropic's server tool takes max_uses; OpenAI's web search has no per-request limit,
        # so the turn's output cap, the phase time limit and the post-call charge bound it.
        hosted = {**hosted, "max_uses": max_uses}
    return hosted


__all__ = [
    "LIMIT_REACHED",
    "TOOL_IDS",
    "TOOL_LABELS",
    "ToolError",
    "TurnTools",
    "catalog",
    "check_tools",
    "web_search_available",
    "web_search_models",
]
