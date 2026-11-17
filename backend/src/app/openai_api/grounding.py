"""Grounded Answer Gate (CTR-0287, PRP-0256, UDR-0237 D6/D7).

For a key-bound agent whose spec sets ``metadata.chatwalaau.knowledge.grounding: strict``,
``/v1/responses`` searches the allowed collections BEFORE any model call:

- no hit left after ``max_distance`` -> the fixed answer, ``metadata.grounding = no_match``,
  and no model call (no tokens, no ledger record);
- hits -> numbered excerpts after the fixed instruction, ``metadata.grounding = grounded``;
  a final answer equal to the fixed sentence is reported as ``no_match`` too.

The query is the last user message plus, on a chained turn, the previous user message of
the thread (Q1). What this guarantees is "nothing found -> no answer" only; staying inside
the found excerpts remains an instruction (D7).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.declarative.knowledge import KnowledgeScope

logger = logging.getLogger(__name__)

#: Fixed texts (English, PRP-0256). Defined ONLY here.
NO_MATCH_ANSWER = (
    "I could not find an answer to this question in the documents I can search. "
    "Please rephrase your question or contact the person in charge."
)
GROUNDING_INSTRUCTION = (
    "Answer only from the excerpts below. Do not use general knowledge. If the excerpts do not "
    f'contain the answer, reply exactly: "{NO_MATCH_ANSWER}"'
)
GROUNDED = "grounded"
NO_MATCH = "no_match"
#: The synthetic call id under which server hits feed the sources-mode citations.
GROUNDING_CALL_ID = "chatwalaau_grounding"


@dataclass(frozen=True)
class Retrieval:
    query: str
    hits: tuple[dict[str, Any], ...]

    @property
    def found(self) -> bool:
        return bool(self.hits)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("input_text", "text", "output_text")
        ]
        return " ".join(p for p in parts if p)
    return ""


def last_user_text(request_input: Any) -> str:
    """The text of the last user message of a Responses ``input``."""
    if isinstance(request_input, str):
        return request_input
    for item in reversed(request_input or []):
        if isinstance(item, dict) and item.get("role", "user") == "user" and item.get("type", "message") == "message":
            text = _text_of(item.get("content"))
            if text:
                return text
    return ""


def previous_user_text(thread_id: str | None) -> str:
    """The last user message already stored in the thread, or ''."""
    if not thread_id:
        return ""
    try:
        from app.session.storage import read_session_json

        data = read_session_json(thread_id) or {}
    except Exception:
        logger.debug("grounding: could not read thread %s", thread_id, exc_info=True)
        return ""
    for message in reversed(data.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            text = " ".join(
                str(c.get("text") or "")
                for c in message.get("contents") or []
                if isinstance(c, dict) and c.get("type") == "text"
            ).strip()
            if text:
                return text
    return ""


def retrieval_query(request_input: Any, prior_thread_id: str | None) -> str:
    """Last user message, preceded by the previous one on a chained turn (Q1)."""
    current = last_user_text(request_input).strip()
    previous = previous_user_text(prior_thread_id)
    return f"{previous}\n{current}".strip() if previous else current


def retrieve(scope: KnowledgeScope, query: str) -> Retrieval:
    """Search every allowed collection; keep hits within max_distance, closest first, top_k."""
    from app.rag import tools as rag_tools

    if not query:
        return Retrieval(query, ())
    top_k = rag_tools.default_top_k()
    hits: list[dict[str, Any]] = []
    for collection in scope.rag_collections:
        result = rag_tools.search_collection(query, collection, top_k)
        if result.get("error"):
            logger.warning("grounding: search of %r failed: %s", collection, result["error"])
            continue
        for hit in result.get("results") or []:
            if scope.max_distance is not None and float(hit.get("distance", 0.0)) > scope.max_distance:
                continue
            hits.append({**hit, "collection": collection})
    hits.sort(key=lambda h: float(h.get("distance", 0.0)))
    return Retrieval(query, tuple(hits[:top_k]))


def excerpts_instruction(retrieval: Retrieval) -> str:
    """The fixed instruction followed by the numbered excerpts."""
    lines = [GROUNDING_INSTRUCTION, "", "Excerpts:"]
    for index, hit in enumerate(retrieval.hits, start=1):
        lines.append(f"[{index}] {hit.get('source') or 'unknown'}, page {hit.get('page', 0)}:")
        lines.append(str(hit.get("text") or "").strip())
        lines.append("")
    return "\n".join(lines).rstrip()


def citation_items(retrieval: Retrieval) -> list[dict[str, Any]]:
    """The server hits as rag_search items, so controls.rag_citations reads them (sources mode)."""
    if not retrieval.found:
        return []
    return [
        {"type": "function_call", "name": "rag_search", "arguments": "{}", "call_id": GROUNDING_CALL_ID},
        {
            "type": "function_call_output",
            "call_id": GROUNDING_CALL_ID,
            "output": json.dumps({"results": list(retrieval.hits)}),
        },
    ]


def is_no_match_answer(text: str) -> bool:
    return (text or "").strip() == NO_MATCH_ANSWER


def final_status(retrieval: Retrieval, answer: str) -> str:
    return NO_MATCH if not retrieval.found or is_no_match_answer(answer) else GROUNDED


__all__ = [
    "GROUNDED",
    "GROUNDING_INSTRUCTION",
    "NO_MATCH",
    "NO_MATCH_ANSWER",
    "Retrieval",
    "citation_items",
    "excerpts_instruction",
    "final_status",
    "is_no_match_answer",
    "last_user_text",
    "retrieval_query",
    "retrieve",
]
