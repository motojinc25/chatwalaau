"""Structured capability guidance for Prompt Assembly slot #3 (PRP-0120, CTR-0104 v4).

Slot #1 is the Global Agent Identity, slot #2a/#2b are the User Profile / Agent Memory
Blocks (each delimited by a `<user-profile>` / `<agent-memory>` tag), and slot #3 is
the capability / tool guidance. Before PRP-0120 slot #3 was an undelimited plaintext
blob: `app.agui.agent_factory._build_tools_and_instructions` appended a prose paragraph
per tool category (~11 of them) with inconsistent formatting.

This module gives slot #3 the same discipline slot #2 has: each tool category
contributes a named `ToolGuidance` block and `render_capability_guidance` wraps each in
a uniform `<tool-guide name="...">` tag (UDR-0103 D1). The tag is deliberately NOT one
of the memory-block tags: `<user-profile>` / `<agent-memory>` delimit injected per-run
DATA, whereas `<tool-guide>` delimits static instruction text (UDR-0103 D3).

Guidance SUBSTANCE is preserved (UDR-0103 D4). The only normalization applied is
stripping a leading Markdown H2 heading line (`## ...`) from a block, since the tag now
supplies the label -- this generalizes UDR-0103's "normalized RAG heading" to every
block that carried one (cron / pipeline / webhook / ontology), so the layer is uniform
rather than "one heading dropped, four kept". See PRP-0120 "Implementation notes".
"""

from __future__ import annotations

from dataclasses import dataclass

_TAG = "tool-guide"


@dataclass(frozen=True)
class ToolGuidance:
    """One tool category's slot-#3 guidance: a stable ``name`` and its body ``text``."""

    name: str
    text: str


def _normalize(text: str) -> str:
    """Strip outer whitespace and a single leading Markdown H2 heading line.

    The `<tool-guide name="...">` tag supplies the label, so a leading `## Heading`
    (RAG / cron / pipeline / webhook / ontology carried one) is redundant and dropped
    for a uniform layer (UDR-0103 D4, extended). Blocks with no heading (weather,
    coding, image, MCP, the memory tools) are only whitespace-stripped.
    """
    lines = text.strip().splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def render_capability_guidance(blocks: list[ToolGuidance]) -> str:
    """Render slot #3 as `<tool-guide name="...">` blocks joined by blank lines.

    Blocks are emitted in the order given (UDR-0103 D4: fixed order). A block whose
    normalized text is empty is skipped, so a category that contributes an empty string
    adds nothing. An empty list renders the empty string, keeping the no-tool / DevUI /
    headless path byte-for-byte identical to "no capability guidance".
    """
    parts: list[str] = []
    for block in blocks:
        text = _normalize(block.text)
        if not text:
            continue
        parts.append(f'<{_TAG} name="{block.name}">\n{text}\n</{_TAG}>')
    return "\n\n".join(parts)


__all__ = ["ToolGuidance", "render_capability_guidance"]
