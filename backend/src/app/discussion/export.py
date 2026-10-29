"""Markdown export of an AI discussion run (CTR-0272, PRP-0235, UDR-0218 D1).

Built by code from the stored run and session -- no model call -- in a fixed section order. The
AI's conclusion (and its report) and the person's decision are separate sections (UDR-0215 D10);
an undecided run says so. The full discussion log is appended only on request.
"""

from __future__ import annotations

import re
from typing import Any

from app.discussion import schema, selection

_VERDICT_LABELS = {"adopt": "Adopted", "hold": "On hold", "rediscuss": "Re-discuss"}
_STATUS_LABELS = {
    schema.CREATED: "Created",
    schema.RUNNING: "Running",
    schema.WAITING_HUMAN: "Waiting for you",
    schema.PAUSED: "Paused",
    schema.SUMMARIZING: "Summarizing",
    schema.COMPLETED: "Completed",
    schema.CANCELLED: "Cancelled",
    schema.FAILED: "Failed",
}


def file_name(run: dict[str, Any]) -> str:
    """``discussion-<YYYYMMDD>-<slug>-<run6>.md`` -- deterministic, so a re-save replaces its file."""
    day = re.sub(r"[^0-9]", "", str(run.get("created_at") or ""))[:8] or "00000000"
    topic = str((run.get("brief") or {}).get("topic") or "")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40].strip("-") or "discussion"
    short = str(run.get("id") or "").removeprefix("dr_")[:6] or "run"
    return f"discussion-{day}-{slug}-{short}.md"


def _text_of(message: dict[str, Any]) -> str:
    return "".join(c.get("text", "") for c in message.get("contents") or [] if isinstance(c, dict)).strip()


def _run_messages(data: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    out = []
    for m in data.get("messages") or []:
        meta = m.get(schema.MESSAGE_KEY) if isinstance(m, dict) else None
        if isinstance(meta, dict) and meta.get("run_id") == run_id:
            out.append(m)
    return out


def _bullets(items: Any) -> list[str]:
    values = [str(x).strip() for x in items or [] if str(x).strip()]
    return [f"- {x}" for x in values] or ["- (none)"]


def _shift_headings(text: str) -> str:
    """The report's own headings one level down, so they sit under this document's section."""
    return re.sub(r"(?m)^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", text)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def run_markdown(data: dict[str, Any], run: dict[str, Any], *, include_log: bool = False) -> tuple[str, str]:
    """``(file name, Markdown)`` of one run."""
    brief = run.get("brief") or {}
    names = {p["id"]: p["display_name"] for p in run.get("participants") or []}
    names[schema.MODERATOR_ID] = (run.get("moderator") or {}).get("display_name") or "Moderator"
    names[schema.HUMAN_ID] = "You"
    messages = _run_messages(data, run["id"])
    mode = selection.mode_of(run)
    lines: list[str] = [f"# AI discussion: {brief.get('topic') or '(no topic)'}", ""]
    meta = [
        f"- Date: {str(run.get('created_at') or '')[:10]}",
        f"- Chat: {data.get('title') or ''}",
        f"- Run: {run.get('id')}",
        f"- Status: {_STATUS_LABELS.get(str(run.get('status')), str(run.get('status')))}",
    ]
    if run.get("termination_reason"):
        meta.append(f"- Ended by: {run['termination_reason']}")
    meta.append(f"- Rounds: {int(run.get('round') or 0)} / {int(run.get('rounds') or 0)}")
    meta.append(
        "- Who speaks in a round: "
        + ("chosen by the decision model" if mode == selection.MODE_DECISION else "everyone")
    )
    lines += [*meta, ""]

    lines += ["## Brief", "", f"- Topic: {brief.get('topic') or ''}"]
    if brief.get("goal"):
        lines.append(f"- Wanted outcome: {brief['goal']}")
    if brief.get("constraints"):
        lines.append(f"- Constraints: {brief['constraints']}")
    lines.append("")

    if run.get("parent_run_id"):
        lines += ["## Continuation", "", f"- Continues: {run['parent_run_id']}"]
        if run.get("focus"):
            lines.append(f"- Focus: {run['focus']}")
        carried = run.get("carried") or {}
        if carried:
            lines.append("- Carried conclusion:")
            lines += [f"  - Common: {x}" for x in carried.get("common") or []]
            lines += [f"  - Conflict: {x}" for x in carried.get("conflicts") or []]
            lines += [f"  - Open: {x}" for x in carried.get("missing") or []]
        lines.append("")

    lines += ["## Participants", "", "| Name | Role | Model | Reasoning | Tools |", "| --- | --- | --- | --- | --- |"]
    for p in run.get("participants") or []:
        lines.append(
            f"| {_cell(p.get('display_name'))} | {_cell(p.get('role'))} | {_cell(p.get('model') or '')} | "
            f"{_cell(p.get('effort') or '-')} | {_cell(', '.join(p.get('tools') or []) or '-')} |"
        )
    lines.append("")

    final = run.get("final")
    lines += ["## AI conclusion (recommendation)", ""]
    if isinstance(final, dict):
        lines += [f"**Recommendation:** {final.get('recommendation') or '(no option has enough support)'}", ""]
        for title, key in (
            ("Agreements", "agreements"),
            ("Dissent", "dissent"),
            ("Open issues", "open_issues"),
            ("Next actions", "next_actions"),
        ):
            lines += [f"**{title}**", "", *_bullets(final.get(key)), ""]
    else:
        lines += ["No summary yet.", ""]

    report_id = schema.message_id(run["id"], schema.SUMMARY, schema.REPORT_ID)
    report = next((m for m in messages if m.get("message_id") == report_id), None)
    if report is not None and _text_of(report):
        lines += ["## Report", "", _shift_headings(_text_of(report)), ""]

    decision = run.get("human_decision")
    lines += ["## Person's decision", ""]
    if isinstance(decision, dict) and decision.get("verdict"):
        lines.append(f"- Verdict: {_VERDICT_LABELS.get(decision['verdict'], decision['verdict'])}")
        if decision.get("note"):
            lines.append(f"- Note: {decision['note']}")
        lines.append(f"- Decided by: {decision.get('decided_by') or ''}")
        lines.append(f"- Decided at: {decision.get('decided_at') or ''}")
    else:
        lines.append("Not decided yet.")
    lines.append("")

    lines += [
        "## Participation",
        "",
        "| Participant | Rounds spoken | Messages | Tool uses |",
        "| --- | --- | --- | --- |",
    ]
    for row in selection.participation(data, run):
        lines.append(
            f"| {_cell(row['display_name'])} | {row['rounds_spoken']} / {row['rounds_held']} | "
            f"{row['messages']} | {row['tool_uses']} |"
        )
    lines.append("")

    selections = run.get("selections") or {}
    if mode == selection.MODE_DECISION and selections:
        lines += ["## Speaker selection", ""]
        for key in sorted(selections, key=lambda k: int(k) if str(k).isdigit() else 0):
            record = selections[key]
            if isinstance(record, dict):
                lines.append(f"- {selection.summary_line(run, record)}")
        lines.append("")

    budget = run.get("budget") or {}
    lines += [
        "## Usage",
        "",
        f"- Tokens used: {int(budget.get('used_tokens') or 0):,} of {int(budget.get('token_budget') or 0):,}",
        "",
    ]

    if include_log:
        lines += ["## Appendix: discussion log", ""]
        for m in messages:
            meta_ = m.get(schema.MESSAGE_KEY) or {}
            who = meta_.get("display_name") or names.get(str(meta_.get("speaker_id")), "")
            lines += [f"### {who} -- {meta_.get('phase')}", "", _shift_headings(_text_of(m)), ""]

    return file_name(run), "\n".join(lines).rstrip() + "\n"


__all__ = ["file_name", "run_markdown"]
