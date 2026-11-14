"""Operator controls of POST /v1/responses (CTR-0057 v5, PRP-0252, UDR-0233).

Three runtime App Settings (group ``openai_api``), read on every request:

- ``responses_max_output_tokens`` -- a CAP on ``max_output_tokens`` (D2).
- ``responses_tool_output`` -- ``full`` | ``sources`` | ``none``: what tool output the
  RESPONSE carries (D3). It never touches session storage, the CTR-0266 fold or the
  usage ledger (D4).
- ``responses_input_roles`` -- ``passthrough`` | ``user_only``: which ``input`` items are
  accepted (D5). ``user_only`` rejects; it never drops or rewrites an item.

Every default reproduces the behavior before PRP-0252 (D7). The settings singleton is
read through the module at call time, so a settings swap or an App Settings apply is
seen by the next request.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from app.core import config as _config

TOOL_OUTPUT_MODES = ("full", "sources", "none")
INPUT_ROLE_POLICIES = ("passthrough", "user_only")
#: The one tool whose results become file_citation annotations (D3).
RAG_TOOL_NAME = "rag_search"


def _setting(name: str, default: Any) -> Any:
    return getattr(_config.settings, name, default)


def tool_output_mode() -> str:
    """The configured tool output mode; anything unknown reads as ``full`` (D7)."""
    mode = str(_setting("responses_tool_output", "full") or "full")
    return mode if mode in TOOL_OUTPUT_MODES else "full"


def effective_max_output_tokens(requested: int | None) -> int | None:
    """``min(requested, cap)``, ``cap`` when the request omits it, ``requested`` when cap is 0 (D2)."""
    try:
        cap = int(_setting("responses_max_output_tokens", 0) or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return requested
    if requested is None:
        return cap
    return min(requested, cap)


def check_input_roles(input_data: Any) -> None:
    """Reject a non-user ``input`` item with 400 when the policy is ``user_only`` (D5).

    A string input is a user message and always passes. In an array, every item must be a
    dict with ``role == "user"`` whose ``type`` is absent or ``"message"``. A role-less
    item (for example a ``function_call_output``) is refused: the converter would
    otherwise default it to ``user``.
    """
    if str(_setting("responses_input_roles", "passthrough") or "passthrough") != "user_only":
        return
    if isinstance(input_data, str):
        return
    for index, item in enumerate(input_data or []):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400, detail=f"input[{index}] is not an object; only user messages are accepted."
            )
        item_type = item.get("type")
        role = item.get("role")
        if item_type not in (None, "message"):
            raise HTTPException(
                status_code=400,
                detail=f"input[{index}] has type {item_type!r}; only user messages are accepted (responses_input_roles=user_only).",
            )
        if role != "user":
            shown = repr(role) if role is not None else "no role"
            raise HTTPException(
                status_code=400,
                detail=f"input[{index}] has {shown}; only role 'user' is accepted (responses_input_roles=user_only).",
            )


def _result_payloads(result: Any) -> list[dict[str, Any]]:
    """The JSON object(s) inside one tool result, whatever shape MAF handed over."""
    if isinstance(result, dict):
        return [result]
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return []
        return [parsed] if isinstance(parsed, dict) else []
    if isinstance(result, list):
        payloads: list[dict[str, Any]] = []
        for part in result:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if isinstance(text, str):
                payloads.extend(_result_payloads(text))
        return payloads
    return []


def rag_citations(items: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """``file_citation`` annotations for the rag_search results among ``items`` (D3, D8).

    ``items`` are the turn's ``function_call`` / ``function_call_output`` output items.
    One annotation per ``(filename, page)`` in first-seen order, carrying ``page`` and
    ``distance`` beside the standard ``filename`` and ``index`` (the answer length). An
    annotation means the chunk was RETRIEVED, not that the answer used it.
    """
    rag_calls = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call" and item.get("name") == RAG_TOOL_NAME
    }
    seen: set[tuple[str, Any]] = set()
    annotations: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") != "function_call_output" or item.get("call_id") not in rag_calls:
            continue
        for payload in _result_payloads(item.get("output")):
            for hit in payload.get("results") or []:
                if not isinstance(hit, dict):
                    continue
                filename = str(hit.get("source") or "")
                if not filename:
                    continue
                key = (filename, hit.get("page"))
                if key in seen:
                    continue
                seen.add(key)
                annotations.append(
                    {
                        "type": "file_citation",
                        "index": len(text),
                        "filename": filename,
                        "page": hit.get("page"),
                        "distance": hit.get("distance"),
                    }
                )
    return annotations


def is_tool_item(item: dict[str, Any]) -> bool:
    return item.get("type") in ("function_call", "function_call_output")


def shape_output(items: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Apply the tool output mode to a finished output list (D3).

    ``full`` returns ``items`` unchanged. ``sources`` and ``none`` drop every tool item;
    ``sources`` adds the rag_search annotations to each ``output_text`` part.
    """
    if mode == "full":
        return items
    tool_items = [item for item in items if is_tool_item(item)]
    shaped: list[dict[str, Any]] = []
    for item in items:
        if is_tool_item(item):
            continue
        if mode == "sources" and item.get("type") == "message":
            item = dict(item)
            parts = []
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    part = {**part, "annotations": rag_citations(tool_items, part.get("text", ""))}
                parts.append(part)
            item["content"] = parts
        shaped.append(item)
    return shaped
