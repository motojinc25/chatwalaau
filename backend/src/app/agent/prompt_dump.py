"""LLM prompt dump for debugging / observability (PROMPT_DUMP_*, CTR-0009 / CTR-0006).

When ``PROMPT_DUMP_ENABLED`` is set (default false), each AG-UI run writes the fully
assembled system prompt (Identity slot #1 + User Profile slot #2a + Agent Memory slot
#2b + capability guidance) together with the run's input messages to a timestamped
file under ``PROMPT_DUMP_DIR`` (default ``.prompts``), so an operator can inspect the
EXACT prompt state per run.

The prompt CONTENT goes ONLY to the file. Callers log METADATA (the written path,
sizes, injection flags) but never the full prompt body -- keeping logs readable while
the heavy content is inspectable on disk.

Best-effort and non-blocking: a write failure logs a WARNING and returns ``None``; it
never raises and never affects the chat. Default false means no files are written and
there is no behavior change.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from app.core.config import settings

if TYPE_CHECKING:
    from app.agent.tool_surface import ToolRow, ToolSurfaceReport

logger = logging.getLogger(__name__)

# Column at which a row's status starts, so the tool list stays scannable.
_NAME_COLUMN = 34


def _safe(value: str) -> str:
    """Make a value safe for a filename fragment."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:60] or "x"


def _message_text(message: dict[str, Any]) -> tuple[str, int]:
    """Flatten a message's content to text; return (text, image_count).

    Handles both the AG-UI request shape (``content``: str | list) and the stored
    MAF/session shape (``contents``: list of typed parts). Binary / image content is
    NOT written (only counted) so the dump stays a readable text file and never
    balloons with base64 data.
    """
    content = message.get("content")
    if content is None:
        content = message.get("contents")
    images = 0
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in ("text", "text_content") and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "image_url":
                    images += 1
        text = "\n".join(parts)
    else:
        text = ""
    images += len(message.get("images") or [])
    return text, images


def _render_row(row: ToolRow, *, indent: int = 0) -> list[str]:
    """Render one tool row (and its children) as aligned Markdown list lines."""
    pad = " " * indent
    label = f"{pad}- {row.name}"
    trailer = row.status
    detail = row.reason or row.note
    if row.reason and row.note:
        detail = f"{row.reason}; {row.note}"
    if detail:
        trailer = f"{row.status}  {detail}"
    lines = [f"{label.ljust(_NAME_COLUMN)}{trailer}"]
    for child in row.children:
        lines.extend(_render_row(child, indent=indent + 4))
    return lines


def _render_section(title: str, rows: list[ToolRow]) -> list[str]:
    lines = [f"### {title}", ""]
    if not rows:
        lines.extend(["- (none)", ""])
        return lines
    for row in rows:
        lines.extend(_render_row(row))
    lines.append("")
    return lines


def render_tool_surface(report: ToolSurfaceReport) -> list[str]:
    """Render the Tool Surface section of a dump (PRP-0119, CTR-0009 v18).

    Deliberately compact -- one line per tool, no descriptions and no JSON schemas
    (UDR-0102: the dump answers "is it there and why", not "what does it do"), so a
    large MCP deployment costs a few KB per turn rather than tens.
    """
    from app.agent.tool_surface import (
        STATUS_ACTIVE,
        STATUS_EXCLUDED,
        STATUS_MISMATCH,
        STATUS_WARNING,
    )

    counts = report.counts()
    lines = [
        "## Tool Surface",
        "",
        f"- agent: {report.agent_label or '(none)'}",
        f"- model: {report.model or '(unknown)'}",
        f"- digest: {report.digest}",
        (
            f"- summary: {counts[STATUS_ACTIVE]} active / {counts[STATUS_EXCLUDED]} excluded / "
            f"{counts[STATUS_WARNING]} warning / {counts[STATUS_MISMATCH]} mismatch"
        ),
    ]
    lines.extend(f"- note: {note}" for note in report.notes)
    lines.append("")
    lines.extend(_render_section("Built-in functions", report.functions))
    lines.extend(_render_section("MCP", report.mcp))
    lines.extend(_render_section("Skills", report.skills))
    lines.extend(_render_section("Provider-supplied", report.provider_supplied))
    return lines


def dump_prompt(
    *,
    thread_id: str,
    run_id: str,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
    include_system_prompt: bool = True,
    tool_surface: ToolSurfaceReport | None = None,
) -> Path | None:
    """Write the flowing prompt to a file, or return None.

    ``messages`` is the FULL flowing conversation for this run -- the persisted
    session history (which the backend history provider injects) plus the new
    incoming message(s) -- so the past conversation is visible from the 2nd turn on.

    ``include_system_prompt`` writes the assembled system prompt block on the first
    turn ("session start"); on later turns the system prompt is unchanged (a frozen
    snapshot), so only the flowing conversation is written and the section notes that.

    ``tool_surface`` (PRP-0119, CTR-0009 v18) writes the agent's tool surface. Unlike
    the system prompt it is written on EVERY turn: MCP tool state is re-read per run
    (UDR-0064 D3) and the whole surface is rebuildable mid-session from the MCP,
    Skills, and declarative-agent management surfaces, so a first-turn-only section
    would be stale exactly when it matters (UDR-0102 D6).

    Returns the written path when ``PROMPT_DUMP_ENABLED`` and the write succeeds;
    ``None`` when disabled or on any failure (logged as a WARNING, never raised).
    """
    if not settings.prompt_dump_enabled:
        return None
    try:
        directory = Path(settings.prompt_dump_dir)
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%S%f")
        path = directory / f"{stamp}_{_safe(thread_id)}_{_safe(run_id)}.md"

        lines: list[str] = [
            "# Prompt dump",
            "",
            f"- timestamp: {now.isoformat()}",
            f"- thread_id: {thread_id}",
            f"- run_id: {run_id}",
            f"- model: {model}",
        ]
        for key, val in (meta or {}).items():
            lines.append(f"- {key}: {val}")
        lines.append(f"- system_prompt_included: {include_system_prompt}")
        lines.append(f"- system_prompt_chars: {len(system_prompt or '')}")
        lines.append(f"- conversation_messages: {len(messages)}")
        if tool_surface is not None:
            _counts = tool_surface.counts()
            lines.append(f"- tool_surface_digest: {tool_surface.digest}")
            lines.append(f"- tool_surface_counts: {_counts}")

        if tool_surface is not None:
            lines.append("")
            lines.extend(render_tool_surface(tool_surface))

        lines.append("\n## System Prompt\n")
        if include_system_prompt:
            lines.append(system_prompt or "(none)")
        else:
            lines.append("(unchanged since session start -- see the first-turn dump for this thread)")

        lines.append("\n## Conversation (flowing prompt)\n")
        for i, message in enumerate(messages):
            role = message.get("role", "?")
            text, image_count = _message_text(message)
            header = f"### [{i}] {role}"
            if image_count:
                header += f"  (+{image_count} image(s))"
            lines.append(header)
            lines.append("")
            lines.append(text or "(no text)")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except (OSError, ValueError):
        # A write / permission failure OR a malformed PROMPT_DUMP_DIR (e.g. an
        # invalid path value) must never break the chat -- log and move on.
        logger.warning("Could not write prompt dump to %s", settings.prompt_dump_dir, exc_info=True)
        return None
