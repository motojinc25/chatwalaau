"""Outbound Teams reply chunking (CTR-0140, PRP-0092, UDR-0070 D6).

Pure helper that splits a reply into ordered chunks no longer than
``TEAMS_MAX_REPLY_CHARS`` (default 25000), honoring Teams' per-message size limit.
Kept SDK-independent so it is unit-testable; the actual ``ctx.send`` /
proactive-send call lives in the adapter (lazy SDK import).

Splitting prefers paragraph and line boundaries, then a hard character cut, so a
chunk break lands on whitespace where possible and never mid-grapheme-run beyond
the cap. A code fence that would be split across chunks is re-opened on the next
chunk so Markdown rendering in Teams stays well-formed.
"""

from __future__ import annotations

from app.core.config import settings


def _effective_limit(limit: int | None) -> int:
    cap = limit if limit is not None else settings.teams_max_reply_chars
    return max(1, int(cap))


def chunk_reply(text: str, *, limit: int | None = None) -> list[str]:
    """Split ``text`` into ordered chunks <= the Teams size limit (UDR-0070 D6).

    Empty / whitespace-only input yields an empty list (nothing to send). A single
    chunk is returned unchanged when it already fits.
    """
    cap = _effective_limit(limit)
    body = text or ""
    if not body.strip():
        return []
    if len(body) <= cap:
        return [body]

    chunks: list[str] = []
    remaining = body
    while remaining:
        if len(remaining) <= cap:
            chunks.append(remaining)
            break
        window = remaining[:cap]
        # Prefer a paragraph break, then a newline, then a space, within the window.
        cut = window.rfind("\n\n")
        if cut < cap // 2:
            cut = window.rfind("\n")
        if cut < cap // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = cap  # no good boundary; hard cut at the cap
        head, remaining = remaining[:cut].rstrip(), remaining[cut:].lstrip()
        if head:
            chunks.append(head)
    return _balance_code_fences(chunks)


def _balance_code_fences(chunks: list[str]) -> list[str]:
    """Re-open an unclosed ``` fence on the next chunk so Markdown stays valid."""
    out: list[str] = []
    carry_fence: str | None = None
    for chunk in chunks:
        piece = (carry_fence + "\n" if carry_fence else "") + chunk
        # Count fence lines to decide if this piece leaves a fence open.
        fence_lines = [ln for ln in piece.splitlines() if ln.lstrip().startswith("```")]
        if len(fence_lines) % 2 == 1:
            # Odd number of fences -> currently inside a code block. Close this
            # piece and re-open with the same opening fence on the next chunk.
            opener = fence_lines[-1].strip()
            piece = piece + "\n```"
            carry_fence = opener if opener != "```" else "```"
        else:
            carry_fence = None
        out.append(piece)
    return out
