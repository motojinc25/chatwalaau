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


# PRP-0260 (UDR-0241 D2): the same sentence at the END of the final message, after a
# preamble the model wrote first ("... I'll also check the links.I'm sorry, but I cannot
# assist with that request."). The text before it must end a sentence or a line.
_TAIL_PATTERN = re.compile(r"(?:" + TEXT_REFUSAL_PATTERN.pattern + r")\s*\Z", re.IGNORECASE)
_PREAMBLE_ENDINGS = (".", "!", "?", ":", "\n", "。", "！", "？", "：")


def split_text_refusal(final_message: str) -> tuple[str, str] | None:
    """``(preamble, sentence)`` when the final message is, or ends with, the refusal sentence.

    The whole-message case returns an empty preamble (UDR-0236 D6). The tail case
    (UDR-0241 D2) needs all of:
    - nothing but whitespace after the sentence;
    - the preamble, without trailing spaces, ends a sentence or a line;
    - the sentence is not inside an open code fence (an odd number of fences before it).

    A sentence quoted in the middle of an answer, or followed by a closing quote mark,
    never matches. The returned preamble is the original text up to the sentence.
    """
    text = final_message or ""
    whole = text_refusal(text)
    if whole is not None:
        return "", whole
    match = _TAIL_PATTERN.search(text)
    if match is None:
        return None
    sentence = match.group(0).strip()
    if len(sentence) > TEXT_REFUSAL_MAX_CHARS:
        return None
    preamble = text[: match.start()]
    before = preamble.rstrip(" \t")
    if not before.strip():
        return None
    if not before.endswith(_PREAMBLE_ENDINGS):
        return None
    if preamble.count("```") % 2 == 1:
        return None
    return preamble, sentence


__all__ = ["TEXT_REFUSAL_MAX_CHARS", "TEXT_REFUSAL_PATTERN", "split_text_refusal", "text_refusal"]
