"""Teams Adaptive Card tool approval bridge (CTR-0141, PRP-0092, UDR-0070 D8).

Makes Microsoft Teams a THIRD, interactive, non-AG-UI consumer of the Tool
Approval Workflow (CTR-0099 / UDR-0043). A tool-approval request raised during a
Teams-thread turn is rendered as an Adaptive Card with three actions -- Allow
Once / Allow Session / Deny -- and the card's ``Action.Submit`` is mapped back to
the shared approval store.

This module is SDK-independent and unit-testable:

- ``build_approval_card(...)`` returns a plain Adaptive Card dict (the SDK / Bot
  Framework serializes it as an attachment).
- ``parse_submit(value)`` reads the ``Action.Submit`` payload into a typed
  ``ApprovalSubmit``.
- ``apply_decision(...)`` maps a decision onto the CTR-0099 ``approval_store``:
  Allow Once -> approve this record; Allow Session -> approve + cache the
  (thread_id, tool) session grant; Deny -> reject.

Teams MUST NOT auto-approve (UDR-0070 D8): the adapter always renders the card and
waits for a decision (or a bounded timeout that resolves to Deny).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.agent.approval import approval_store

Decision = Literal["allow_once", "allow_session", "deny"]

# Marker that identifies a tool-approval Action.Submit among other Teams submits.
SUBMIT_KIND = "teams_tool_approval"


def build_approval_card(
    *,
    record_id: str,
    thread_id: str,
    tool_name: str,
    arguments_preview: dict[str, Any],
) -> dict[str, Any]:
    """Build an Adaptive Card for a pending tool-approval request (UDR-0070 D8).

    The three Action.Submit buttons carry a hidden ``data`` payload (kind, the
    approval record id, the thread id, the tool name, and the decision) so the
    adapter can resolve the matching store record on submit without any client
    state. Returns a plain dict; the adapter wraps it as an Adaptive Card
    attachment for ``ctx.send``.
    """
    args_text = _format_args(arguments_preview)
    base_data = {
        "kind": SUBMIT_KIND,
        "record_id": record_id,
        "thread_id": thread_id,
        "tool": tool_name,
    }
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Tool approval required",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": f"The agent wants to run **{tool_name}**.",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": args_text,
                "wrap": True,
                "isSubtle": True,
                "fontType": "Monospace",
                "spacing": "Small",
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Allow Once",
                "style": "positive",
                "data": {**base_data, "decision": "allow_once"},
            },
            {
                "type": "Action.Submit",
                "title": "Allow Session",
                "data": {**base_data, "decision": "allow_session"},
            },
            {
                "type": "Action.Submit",
                "title": "Deny",
                "style": "destructive",
                "data": {**base_data, "decision": "deny"},
            },
        ],
    }


def _format_args(arguments_preview: dict[str, Any]) -> str:
    if not arguments_preview:
        return "(no arguments)"
    lines = [f"{key}: {value}" for key, value in arguments_preview.items()]
    return "\n".join(lines)


@dataclass(frozen=True)
class ApprovalSubmit:
    """A parsed tool-approval Action.Submit payload."""

    record_id: str
    thread_id: str
    tool: str
    decision: Decision


def parse_submit(value: dict[str, Any] | None) -> ApprovalSubmit | None:
    """Parse an Action.Submit ``value`` into an ApprovalSubmit, or None.

    Returns None when the payload is not a tool-approval submit (so the adapter can
    ignore other card submits) or is missing required fields / has an unknown
    decision.
    """
    if not isinstance(value, dict) or value.get("kind") != SUBMIT_KIND:
        return None
    decision = value.get("decision")
    if decision not in ("allow_once", "allow_session", "deny"):
        return None
    record_id = str(value.get("record_id") or "")
    thread_id = str(value.get("thread_id") or "")
    tool = str(value.get("tool") or "")
    if not record_id or not thread_id:
        return None
    return ApprovalSubmit(record_id=record_id, thread_id=thread_id, tool=tool, decision=decision)  # type: ignore[arg-type]


async def apply_decision(submit: ApprovalSubmit) -> tuple[str, Any]:
    """Map a parsed submit onto the CTR-0099 approval store (UDR-0070 D8).

    - ``allow_once``    -> resolve the record approved.
    - ``allow_session`` -> resolve the record approved AND cache the
      (thread_id, tool) session grant so the same tool in this Teams thread
      proceeds without re-prompting (reuses the existing session cache).
    - ``deny``          -> resolve the record rejected.

    Returns the ``(status, record)`` tuple from ``approval_store.resolve`` so the
    caller can distinguish ok / already-resolved / missing.
    """
    approved = submit.decision in ("allow_once", "allow_session")
    if submit.decision == "allow_session":
        await approval_store.cache_decision(thread_id=submit.thread_id, tool_name=submit.tool, approved=True)
    return await approval_store.resolve(submit.record_id, approved=approved, source="user")
