"""Fact-level tracing of the provider request seam (CTR-0102, UDR-0126, UDR-0123 D5).

Prints what actually goes on the wire to the provider -- the MAF-side messages, the
Responses ``input`` items, and a pairing verdict computed with the same checks the
provider rejects on -- so a provider 400 arrives pre-explained instead of being
deduced afterwards.

Everything here is IDs and shapes only. No arguments, no tool output, no prompt
text. Volume is a few short lines per model call, at INFO under the dedicated
``app.agent.wire_trace`` logger, which can be silenced by name.

PRP-0179 (UDR-0161 D9): this was the wire half of ``app.agent.approval_debug``
(logger ``app.agent.approval_trace``). The approval-loop half -- settlement,
dangling-call healing, wire-result capture, session approval state -- was deleted
with the approval loop; the diagnostics stay because they describe the wire, not
the feature. The ``mcp_approval_*`` item names below are the provider's wire
grammar and are kept for the same reason.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.agent.wire_trace")

_MAX_ITEMS = 80

# The three output item types the OpenAI Responses assembler can emit for ONE tool
# result, selected by a marker MAF attaches when a hosted `shell_call` is mapped onto
# a local function name (agent_framework_openai/_chat_client.py:1910-1932).
#
# PRP-0147 / UDR-0126 D1: this module previously modelled only `function_call_output`,
# so every `run_shell` call was reported as CALL-WITHOUT-OUTPUT -- 17 fabricated
# defects per round, printed ahead of the 50 real ones. A check that models a subset
# reports the absence of its own coverage as a defect in the subject.
_CALL_KEYED_OUTPUT_TYPES = ("function_call_output", "shell_call_output")

# `local_shell_call_output` carries the ORIGINATING item's `id` and no `call_id`
# (_chat_client.py:1927-1932), so it cannot be paired against the `call_id` its
# `function_call` was serialized with. UDR-0126 D1 requires saying so rather than
# inferring a key: an item counted here is reported as a labelled NON-DEFECT and
# never suppresses a real finding.
_UNPAIRABLE_OUTPUT_TYPES = ("local_shell_call_output",)


def _short(value: Any, width: int = 40) -> str:
    text = str(value) if value is not None else "-"
    return text if len(text) <= width else text[: width - 3] + "..."


def describe_content(content: Any) -> str:
    """``type[call_id ...]`` for one MAF Content; approval contents show both ids."""
    ctype = getattr(content, "type", None) or type(content).__name__
    call_id = getattr(content, "call_id", None)
    cid = getattr(content, "id", None)
    wrapped = getattr(content, "function_call", None)
    wrapped_call = getattr(wrapped, "call_id", None) if wrapped is not None else None
    if ctype in ("function_approval_request", "function_approval_response"):
        # id is the approval id; the pairing id lives on the wrapped call.
        return f"{ctype}[id={_short(cid)} call={_short(wrapped_call)}]"
    if ctype == "function_call":
        return f"{ctype}[{_short(call_id or cid)} {getattr(content, 'name', '?')}]"
    if call_id or cid:
        return f"{ctype}[{_short(call_id or cid)}]"
    return str(ctype)


def _truncation_note(total: int) -> list[str]:
    """A visible marker when a dump was cut at ``_MAX_ITEMS`` (PRP-0147, UDR-0126 D2).

    Volume discipline is correct -- a 934-item dump per model call is unreadable --
    but silent truncation is not: a reader given 80 of 934 items with no marker
    cannot tell which of the two numbers a verdict was derived from. The pairing
    report always scans the FULL list; this line is what says so.
    """
    hidden = total - _MAX_ITEMS
    if hidden <= 0:
        return []
    return [f"... +{hidden} more not shown (pairing verdict covers all {total})"]


def describe_messages(messages: Any) -> list[str]:
    """One ``role{...}`` entry per MAF Message, contents summarised by type/id."""
    message_list = list(messages or [])
    out: list[str] = []
    for message in message_list[:_MAX_ITEMS]:
        role = getattr(message, "role", "?")
        contents = getattr(message, "contents", None) or []
        out.append(f"{role}{{{', '.join(describe_content(c) for c in contents)}}}")
    return [*out, *_truncation_note(len(message_list))]


def describe_wire_item(item: Any) -> str:
    """Summarise one OpenAI Responses ``input`` item as the provider will validate it."""
    if not isinstance(item, dict):
        return _short(type(item).__name__)
    itype = item.get("type") or ("message" if "role" in item else "?")
    if itype == "function_call":
        return (
            f"function_call[call_id={_short(item.get('call_id'))} id={_short(item.get('id'))} {item.get('name', '?')}]"
        )
    if itype == "function_call_output":
        return f"function_call_output[call_id={_short(item.get('call_id'))}]"
    if itype == "mcp_approval_response":
        return (
            f"mcp_approval_response[approval_request_id={_short(item.get('approval_request_id'))} "
            f"approve={item.get('approve')}]"
        )
    if itype == "mcp_approval_request":
        return f"mcp_approval_request[id={_short(item.get('id'))} {item.get('name', '?')}]"
    if itype == "message" or "role" in item:
        content = item.get("content")
        n = len(content) if isinstance(content, list) else 1
        return f"message[{item.get('role', '?')} parts={n}]"
    if itype == "reasoning":
        return f"reasoning[id={_short(item.get('id'))}]"
    if itype in _CALL_KEYED_OUTPUT_TYPES or itype in _UNPAIRABLE_OUTPUT_TYPES:
        # PRP-0147 / UDR-0126 D1. Without this branch these fell to the generic tail
        # below, which reads `id`; a shell output carries `call_id`, so the dump read
        # `shell_call_output[id=-]` and erased the linkage from the one line a reader
        # would use to audit the pairing check itself.
        return f"{itype}[call_id={_short(item.get('call_id') or item.get('id'))}]"
    return f"{itype}[id={_short(item.get('id'))}]"


def describe_wire_input(items: Any) -> list[str]:
    if isinstance(items, str):
        return [f"text[{len(items)} chars]"]
    item_list = list(items or [])
    described = [describe_wire_item(i) for i in item_list[:_MAX_ITEMS]]
    return [*described, *_truncation_note(len(item_list))]


def describe_wire_input_full(items: Any) -> list[str]:
    """Every item, no ``_MAX_ITEMS`` cap (PRP-0148 Section 4.4).

    Used only when the post-repair verdict says the request is going to be rejected.
    That is the one moment the 80-item cap costs more than it saves, and it is rare
    by construction: a request that is about to fail anyway.
    """
    if isinstance(items, str):
        return [f"text[{len(items)} chars]"]
    return [describe_wire_item(i) for i in list(items or [])]


def wire_pairing_report(items: Any) -> str:
    """The exact checks the Responses API rejects on, computed locally before the POST.

    Always computed over the FULL item list, and -- per UDR-0126 D5 -- over the input
    as MAF assembled it, BEFORE any repair this codebase applies at the request seam.
    A repair that erased its own evidence would make an unfixed upstream producer
    invisible to the next investigation.

    The verdict leads with counts (PRP-0147, UDR-0126 D1): fifty duplicate ids is
    roughly two kilobytes of log line, and the shape has to be readable in the first
    characters. The ids follow, because they are the evidence.
    """
    if not isinstance(items, list):
        return "n/a"
    calls: list[str] = []
    outputs: list[str] = []
    approvals: list[str] = []
    unpairable = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        # `shell_call` is a call too (PRP-0182, UDR-0164 D5): MAF 1.19.0 replays a local
        # harness shell call as the provider's `shell_call` item. Counting only
        # function_call reported "OK" while its output had been dropped.
        if t in ("function_call", "shell_call") and item.get("call_id"):
            calls.append(item["call_id"])
        elif t in _CALL_KEYED_OUTPUT_TYPES and item.get("call_id"):
            outputs.append(item["call_id"])
        elif t in _UNPAIRABLE_OUTPUT_TYPES:
            unpairable += 1
        elif t == "mcp_approval_response":
            approvals.append(str(item.get("approval_request_id")))
    orphans = [cid for cid in outputs if cid not in calls]
    bare = [cid for cid in calls if cid not in outputs]
    duplicates = sorted({c for c in calls if calls.count(c) > 1})
    problems: list[str] = []
    problems.extend(f"OUTPUT-WITHOUT-CALL {cid}" for cid in orphans)
    problems.extend(f"CALL-WITHOUT-OUTPUT {cid}" for cid in bare)
    problems.extend(f"DUPLICATE-CALL {cid}" for cid in duplicates)
    problems.extend(f"MCP-APPROVAL-RESPONSE {aid}" for aid in approvals)
    # An unpairable shell output is a LIMIT OF THE CHECK, not a defect in the payload,
    # so it is counted and named but never makes the verdict non-OK and never
    # suppresses a real finding (UDR-0126 D1).
    note = f"{unpairable} unpairable-shell-output" if unpairable else ""
    if not problems:
        return f"OK | {note}" if note else "OK"
    counts = ", ".join(
        part
        for part in (
            f"{len(duplicates)} duplicate-call",
            f"{len(bare)} call-without-output",
            f"{len(orphans)} orphan-output",
            f"{len(approvals)} mcp-approval-response",
            note,
        )
        if part
    )
    return f"{counts} | " + "; ".join(problems)


def log_wire_request(*, messages: Any, run_options: dict[str, Any], tag: str = "") -> None:
    """Log the request as MAF assembled it: the MAF-side messages AND the wire input."""
    try:
        wire = run_options.get("input")
        include = run_options.get("include")
        has_enc_reasoning = isinstance(include, list) and "reasoning.encrypted_content" in include
        logger.info(
            "[wire%s] store=%s prev_resp_id=%s conversation=%s conversation_id=%s enc_reasoning=%s "
            "model=%s maf_messages=%d wire_items=%d\n"
            "  maf : %s\n"
            "  wire: %s\n"
            "  pairing: %s",
            f" {tag}" if tag else "",
            run_options.get("store"),
            _short(run_options.get("previous_response_id")),
            _short(run_options.get("conversation")),
            _short(run_options.get("conversation_id")),
            has_enc_reasoning,
            run_options.get("model"),
            len(list(messages or [])),
            len(wire) if isinstance(wire, list) else -1,
            " | ".join(describe_messages(messages)),
            " | ".join(describe_wire_input(wire)),
            wire_pairing_report(wire),
        )
    except Exception:  # tracing must never break the request
        logger.exception("[wire] failed to describe request")


__all__ = [
    "describe_content",
    "describe_messages",
    "describe_wire_input",
    "describe_wire_input_full",
    "describe_wire_item",
    "log_wire_request",
    "logger",
    "wire_pairing_report",
]
