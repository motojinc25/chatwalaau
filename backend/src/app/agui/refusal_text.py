"""A refusal the model wrote as ordinary answer text (PRP-0255, UDR-0236 D6).

PRP-0237 (UDR-0220) handles the Responses ``refusal`` PART. A GPT model can also
answer with the same template as plain ``output_text``: it is then streamed,
saved and replayed to the model as if it had answered that way. On AG-UI, a run
whose LAST text message is nothing but that sentence is treated as refused.

The pattern is closed and English only (PRP-0255 Q2): it is the template GPT
models emit even in a Japanese chat. The WHOLE final message must match, so the
sentence quoted inside a longer answer never counts.
"""

from __future__ import annotations

import re

# The longest sentence the pattern accepts is far below this; the cap keeps a
# long message from being scanned at all.
TEXT_REFUSAL_MAX_CHARS = 120

TEXT_REFUSAL_PATTERN = re.compile(
    r"(?:I['’]m|I am) sorry,? but I (?:cannot|can['’]t|can not|am unable to) "
    r"(?:assist|help) with (?:that|this)(?: request)?[.!]?",
    re.IGNORECASE,
)


def text_refusal(final_message: str) -> str | None:
    """Return the stripped refusal sentence when it IS the whole final message, else None."""
    text = (final_message or "").strip()
    if not text or len(text) > TEXT_REFUSAL_MAX_CHARS:
        return None
    return text if TEXT_REFUSAL_PATTERN.fullmatch(text) else None


__all__ = ["TEXT_REFUSAL_MAX_CHARS", "TEXT_REFUSAL_PATTERN", "text_refusal"]
