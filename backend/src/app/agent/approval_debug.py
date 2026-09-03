"""Fact-level tracing for the tool-approval resume path (CTR-0009 / CTR-0100).

Three releases in a row have fixed a provider 400 on this path from a hypothesis
that a reproduction confirmed and production then refuted. This module exists so
the NEXT report carries the facts instead: what the accumulator observed in the
iteration, what MAF holds on the session, what the replay contained, and -- the
ground truth -- what actually went on the wire to the provider.

Everything here is IDs and shapes only. No arguments, no tool output, no prompt
text. Volume is a few short lines per model call, so it stays at INFO under the
dedicated ``app.agent.approval_trace`` logger and can be silenced by name.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.agent.approval_trace")

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

# call_id -> function_result Content most recently SEEN ON THE WIRE (PRP-0141
# multi-iteration follow-up). MAF resumes a deferred call inside the iteration
# that follows the one that replayed it and produces its result there -- that
# result reaches the provider request (captured here) but is NEVER streamed back
# as an observable update, so ``IterationContentAccumulator`` cannot see it and a
# LATER outer-loop iteration re-sends the deferred function_call with no matching
# output (400 "No tool output found for function call ..."). Capturing the wire
# result lets the endpoint heal that gap before the next re-run. Bounded; keyed by
# a globally-unique provider call id, so cross-request reuse cannot collide.
_wire_results: dict[str, Any] = {}
_WIRE_RESULTS_MAX = 1000


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
        if t == "function_call" and item.get("call_id"):
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


def record_wire_function_results(messages: Any) -> None:
    """Remember every ``function_result`` seen on the wire, keyed by call id.

    Called from the request chokepoint so a deferred call's result -- which MAF
    injects into the request but never streams back -- is available to heal a
    later iteration. Best-effort: never raises.
    """
    try:
        for message in messages or []:
            for content in getattr(message, "contents", []) or []:
                if getattr(content, "type", None) == "function_result":
                    call_id = getattr(content, "call_id", None)
                    if call_id:
                        _wire_results[call_id] = content
        overflow = len(_wire_results) - _WIRE_RESULTS_MAX
        if overflow > 0:
            for stale in list(_wire_results)[:overflow]:
                _wire_results.pop(stale, None)
    except Exception:  # capture must never break the request
        logger.exception("[wire] failed to record function results")


def approval_response_call_id(content: Any) -> str | None:
    """The TOOL CALL id an approval response answers for.

    Read from the wrapped ``function_call``, never from the response's own ``id``:
    that one is the APPROVAL REQUEST id, a different namespace, and comparing the
    two discards valid approvals (the same rule ``build_iteration_messages``
    documents at its pairing invariant). The response id is the last resort only
    because MAF's own binding keys on it and the two coincide for the local
    function-tool path.
    """
    wrapped = getattr(content, "function_call", None)
    return getattr(wrapped, "call_id", None) or getattr(content, "id", None)


def settle_consumed_approvals(messages: Any) -> tuple[list[Any], list[str], list[str]]:
    """Settle every already-consumed ``function_approval_response`` in ``messages``.

    UDR-0132 D1. An approval response is a SINGLE-USE TICKET, not a durable fact.
    MAF binds it to the pending approval request and POPS that entry
    (``_tools._bind_approval_response_to_pending_request``, ``consume=True``); a
    later replay of the same response matches nothing, is logged as "Ignored an
    approval response ... because no pending approval request exists" and is
    REMOVED from the request. The ``function_call`` our replay put beside it is
    not removed, so the provider receives a call it will never see an output for
    and rejects the whole turn (400 "No tool output found for function call ...").

    ``messages`` here is the content the approval loop is CARRYING FORWARD -- it
    has been through at least one ``agent.run``, so every approval response in it
    is spent by position, not by heuristic. The round currently being submitted is
    appended afterwards and is untouched by construction.

    Per response, in order of preference:

    1. the call already has a ``function_result`` in ``messages`` -> drop the
       response only;
    2. its result was captured on an earlier request (``_wire_results``) -> drop
       the response and emit that result in a ``tool`` message in its place;
    3. no result is obtainable -> drop the response AND its ``function_call``.

    Rule 3 is load-bearing: a carried-forward call with no obtainable output must
    not reach the request. The cost is a re-issued tool call (the model asks
    again, the operator approves again, a non-idempotent tool may run twice) and
    it is logged at WARNING for exactly that reason. The alternative is a
    guaranteed 400 that kills the turn.

    Returns ``(messages, settled_call_ids, dropped_call_ids)``. Best-effort: on
    any unexpected shape the input is returned unchanged, which degrades to
    today's behaviour rather than to a corrupted request.
    """
    try:
        message_list = list(messages)
        have_result: set[str] = set()
        consumed: list[tuple[Any, str | None]] = []
        for message in message_list:
            for content in getattr(message, "contents", []) or []:
                ctype = getattr(content, "type", None)
                if ctype == "function_result" and getattr(content, "call_id", None):
                    have_result.add(content.call_id)
                elif ctype == "function_approval_response":
                    consumed.append((content, approval_response_call_id(content)))
        if not consumed:
            return message_list, [], []

        # Decide per call id BEFORE rebuilding, because rule 3 has to strip a
        # function_call that sits in an EARLIER message than its response.
        settled_ids: list[str] = []
        dropped_ids: list[str] = []
        replacement: dict[str, Any] = {}
        for _content, call_id in consumed:
            if call_id is None or call_id in settled_ids or call_id in dropped_ids:
                continue
            if call_id in have_result:
                settled_ids.append(call_id)
                continue
            captured = _wire_results.get(call_id)
            if captured is not None:
                replacement[call_id] = captured
                settled_ids.append(call_id)
                continue
            dropped_ids.append(call_id)

        from agent_framework import Message

        dropped = set(dropped_ids)
        out: list[Any] = []
        for message in message_list:
            contents = getattr(message, "contents", None) or []
            kept: list[Any] = []
            emit_after: list[Any] = []
            for content in contents:
                ctype = getattr(content, "type", None)
                if ctype == "function_approval_response":
                    call_id = approval_response_call_id(content)
                    if call_id is None:
                        kept.append(content)
                        continue
                    if call_id in replacement:
                        emit_after.append(replacement[call_id])
                    # settled (rule 1/2) or dropped (rule 3): the response goes.
                    continue
                if ctype == "function_call" and getattr(content, "call_id", None) in dropped:
                    continue
                kept.append(content)
            if len(kept) != len(contents):
                if kept:
                    out.append(
                        Message(
                            role=getattr(message, "role", "user"),
                            contents=kept,
                            author_name=getattr(message, "author_name", None),
                            message_id=getattr(message, "message_id", None),
                            # `additional_properties` is load-bearing and carried:
                            # MAF's `_split_service_call_messages` reads `_attribution`
                            # from it to tell replayed history from new input.
                            # `raw_representation` is NOT carried -- it is excluded from
                            # serialization by `Message.DEFAULT_EXCLUDE`, never reaches a
                            # provider, and reading it here would break UDR-0131 D4.
                            additional_properties=getattr(message, "additional_properties", None),
                        )
                    )
                # A message emptied by this settlement is dropped, exactly as MAF
                # drops one it emptied itself (`messages[:] = filtered_messages`).
            else:
                out.append(message)
            if emit_after:
                # Role "tool" and a separate message: the same shape
                # heal_dangling_tool_calls already proved on both provider lanes.
                out.append(Message(role="tool", contents=emit_after))

        if settled_ids:
            logger.info(
                "[settle] consumed approval response(s) settled into their results (UDR-0132 D1): %s",
                sorted(settled_ids),
            )
        if dropped_ids:
            logger.warning(
                "[settle] consumed approval response(s) with NO obtainable result: removed the "
                "response AND its function_call so the request stays valid (UDR-0132 D1 rule 3). "
                "The model will re-issue these call(s) and the operator will be asked to approve "
                "again: %s",
                sorted(dropped_ids),
            )
        return out, settled_ids, dropped_ids
    except Exception:  # settlement must never break the run
        logger.exception("[settle] failed to settle consumed approval responses")
        return list(messages), [], []


def heal_dangling_tool_calls(
    messages: Any,
    *,
    live_approval_call_ids: Any = (),
) -> tuple[list[Any], list[str]]:
    """Append captured results for any ``function_call`` in ``messages`` that has
    neither a matching ``function_result`` nor a LIVE ``function_approval_response``.

    Returns ``(possibly_extended_messages, healed_call_ids)``. This closes the
    multi-iteration deferred-call gap: a call MAF resumed in an earlier iteration
    (its result captured on that iteration's wire) but which our replay left bare.

    UDR-0132 D2: ``live_approval_call_ids`` is the set of calls answered by the
    round being submitted -- the only approvals whose ticket is still unspent and
    therefore the only ones that predict an output. It is passed IN and is never
    derived by scanning ``messages``. That scan is what let this heal skip the one
    call that needed it: a spent approval response from an earlier round satisfied
    the "is answered" test while MAF was discarding it, and the bare call reached
    the provider. Defaulting to "nothing is live" errs toward healing, which is
    the safe direction -- a call that already has its result is skipped anyway by
    the ``have_result`` test.
    """
    try:
        message_list = list(messages)
        called: list[str] = []
        have_result: set[str] = set()
        have_approval: set[str] = {cid for cid in (live_approval_call_ids or ()) if cid}
        for message in message_list:
            for content in getattr(message, "contents", []) or []:
                ctype = getattr(content, "type", None)
                if ctype == "function_call" and getattr(content, "call_id", None):
                    called.append(content.call_id)
                elif ctype == "function_result" and getattr(content, "call_id", None):
                    have_result.add(content.call_id)
        seen: set[str] = set()
        healed_contents: list[Any] = []
        healed_ids: list[str] = []
        for call_id in called:
            if call_id in have_result or call_id in have_approval or call_id in seen:
                continue
            seen.add(call_id)
            result = _wire_results.get(call_id)
            if result is not None:
                healed_contents.append(result)
                healed_ids.append(call_id)
        if not healed_contents:
            return message_list, []
        from agent_framework import Message

        return [*message_list, Message(role="tool", contents=healed_contents)], healed_ids
    except Exception:  # healing must never break the run
        logger.exception("[wire] failed to heal dangling tool calls")
        return list(messages), []


def log_wire_request(*, messages: Any, run_options: dict[str, Any], tag: str = "") -> None:
    """Log the request as MAF assembled it: the MAF-side messages AND the wire input."""
    record_wire_function_results(messages)
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


def describe_session_state(session: Any) -> str:
    """MAF's tool-approval state on the session, ids only."""
    state = getattr(session, "state", None)
    if not isinstance(state, dict):
        return "session.state=<none>"
    raw = state.get("tool_approval")
    if raw is None:
        return "tool_approval=<absent>"
    if not isinstance(raw, dict):
        to_dict = getattr(raw, "to_dict", None)
        raw = to_dict(exclude={"type"}) if callable(to_dict) else {"<type>": type(raw).__name__}
    parts: list[str] = []
    for key, value in raw.items():
        if key == "already_approved_approval_request_groups" and isinstance(value, list):
            groups = []
            for g in value:
                if not isinstance(g, dict):
                    continue
                vis = g.get("approval_request_ids")
                hidden = [
                    (r.get("function_call") or {}).get("call_id") or r.get("id")
                    for r in (g.get("approval_requests") or [])
                    if isinstance(r, dict)
                ]
                groups.append(f"visible={vis} hidden={hidden}")
            parts.append(f"deferred_groups=[{'; '.join(groups)}]")
        elif isinstance(value, list):
            parts.append(f"{key}=<{len(value)} items>")
        else:
            parts.append(f"{key}={_short(value)}")
    return "tool_approval{" + ", ".join(parts) + "}"


__all__ = [
    "approval_response_call_id",
    "describe_content",
    "describe_messages",
    "describe_session_state",
    "describe_wire_input",
    "heal_dangling_tool_calls",
    "log_wire_request",
    "logger",
    "record_wire_function_results",
    "settle_consumed_approvals",
    "wire_pairing_report",
]
