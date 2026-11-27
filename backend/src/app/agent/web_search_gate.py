"""Hosted web search per run: withholding it, and the fallback after a refusal (PRP-0260, UDR-0241).

A GPT model on Azure ends some turns with its refusal template right after its own hosted
``web_search`` fetched certain pages (measured 2026-11-16: the Declaration of Independence, the
Gettysburg Address). The same turn without the tool is answered. This module holds the three
things the AG-UI endpoint and the provider chokepoints share for that case:

- the per-run flag that withholds the hosted tool (UDR-0241 D3, D5). Only the AG-UI endpoint
  sets it, and only the providers' ``_prepare_options`` read it;
- the ONE read-only tool list that decides whether a refused attempt may run again (D3);
- the hosted-search summary used for the log line, without query text or full URLs (D1).
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any
from urllib.parse import urlparse

_WITHHELD: ContextVar[bool] = ContextVar("chatwalaau_web_search_withheld", default=False)

# UDR-0241 D3 (PRP-0260 Q2): a refused attempt runs again only when every LOCAL tool it
# called is one of these. They read and never change anything, so repeating them has no
# side effect. MCP tools are not listed: their effects are unknown here. The same list
# decides the restart after a cut stream (PRP-0268, UDR-0249 D3), which added the two
# skill tools; run_skill_script runs code and stays off.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "rag_search",
        "ontology_query",
        "file_read",
        "file_glob",
        "file_grep",
        "load_skill",
        "read_skill_resource",
    }
)
READ_ONLY_TOOL_PREFIXES: tuple[str, ...] = ("weather_",)

# The extra instruction of the fallback attempt (UDR-0241 D3).
FALLBACK_INSTRUCTION = (
    "Web search is not available for this answer. Answer from the conversation, the attached "
    "files and your general knowledge. Do not try to search or open web pages. Where a current "
    "or primary source would be needed, say so briefly."
)

HOSTED_WEB_SEARCH_TOOL = "web_search"


def web_search_withheld() -> bool:
    """True while the current run must not offer the hosted web search tool."""
    return _WITHHELD.get()


def withhold_for_run(withheld: bool = True) -> Token[bool]:
    """Set the flag for the current run; give the token back to :func:`release`."""
    return _WITHHELD.set(withheld)


def release(token: Token[bool] | None) -> None:
    """Reset the flag. Never raises: a generator closed from another context cannot reset."""
    if token is None:
        return
    try:
        _WITHHELD.reset(token)
    except ValueError:
        _WITHHELD.set(False)


def is_read_only_tool(name: str | None) -> bool:
    """Whether ``name`` is on the read-only list (UDR-0241 D3)."""
    if not name:
        return False
    return name in READ_ONLY_TOOLS or name.startswith(READ_ONLY_TOOL_PREFIXES)


def search_action(arguments: Any) -> dict[str, Any]:
    """The action dict of a hosted search call (``{"type": "search", "queries": [...]}`` etc.)."""
    if isinstance(arguments, dict):
        action = arguments.get("action") if isinstance(arguments.get("action"), dict) else arguments
        return action if isinstance(action, dict) else {}
    return {}


def search_log_fields(arguments: Any) -> tuple[str, int, str]:
    """``(action type, number of queries, host)`` for the log line -- never the query text (D1)."""
    action = search_action(arguments)
    kind = str(action.get("type") or "unknown")
    queries = action.get("queries")
    count = len(queries) if isinstance(queries, list) else (1 if action.get("query") else 0)
    url = action.get("url")
    host = ""
    if isinstance(url, str) and url:
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            host = ""
    return kind, count, host or "-"


__all__ = [
    "FALLBACK_INSTRUCTION",
    "HOSTED_WEB_SEARCH_TOOL",
    "READ_ONLY_TOOLS",
    "READ_ONLY_TOOL_PREFIXES",
    "is_read_only_tool",
    "release",
    "search_action",
    "search_log_fields",
    "web_search_withheld",
    "withhold_for_run",
]
