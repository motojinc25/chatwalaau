"""Assistant text sanitiser (CTR-0218, PRP-0178, UDR-0160 D1/D2).

A GPT-family model trained on ChatGPT writes its private citation markup instead of the
Markdown links our prompt asks for::

    U+E200 cite U+E202 turn0search0 U+E201
    open   kind sep    payload      close

The characters are Unicode private-use, so no font has a glyph for them and the reader
sees boxes around a meaningless ``citeturn0search0``. Nothing in the pipeline used to
remove it, so it reached the screen, the session file, the search index, the export, the
auto-title task, the memory-extraction task and the Teams reply.

This module is the ONE place that removes it (D1). It matches the ENVELOPE, not a list of
kinds (``cite`` / ``filecite`` / ``navlist`` / ...), so a kind introduced by a later model
cannot slip through, and it also drops stray private-use characters left without an
envelope.

Streaming (D2): a marker is regularly split across deltas, so text after an unmatched
opener is HELD until its closer arrives. The hold is bounded and flushed at the end of the
stream, so a lone private-use character in ordinary prose can never withhold or swallow an
answer.

Pure text in, pure text out: no I/O, no settings, no provider knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The envelope. U+E200 opens, U+E201 closes, U+E202 separates kind from payload; the
# neighbouring code points appear in the same family and are dropped when they arrive
# outside an envelope.
MARKER_OPEN = chr(0xE200)  # opens a marker
MARKER_CLOSE = chr(0xE201)  # closes it
MARKER_SEP = chr(0xE202)  # separates kind from payload
# U+E200..U+E206: the family the envelope belongs to.
PRIVATE_USE_STRAYS = "".join(chr(cp) for cp in range(0xE200, 0xE207))

# How much text may wait for a closer. A real marker is far shorter; past this bound the
# opener was not a marker at all, so the buffer is released unchanged (minus the opener).
MAX_HOLD_CHARS = 512


@dataclass
class TextSanitizer:
    """Stateful, streaming-safe remover of model-private markup.

    Usage::

        s = TextSanitizer()
        clean, removed = s.feed(delta)      # per streamed delta
        tail, removed_tail = s.flush()      # once, at end of stream
    """

    _held: str = ""
    removed: list[str] = field(default_factory=list)

    # ---- public API ---------------------------------------------------------------

    def feed(self, delta: str) -> tuple[str, list[str]]:
        """Return (text safe to emit, payloads removed in THIS call)."""
        if not delta:
            return "", []
        before = len(self.removed)
        out = self._consume(self._held + delta)
        return out, self.removed[before:]

    def flush(self) -> tuple[str, list[str]]:
        """Release whatever is still held: the stream ended mid-marker (or never had one).

        A run that is cut short (token limit, cancel) can end INSIDE a marker. Releasing
        the held text would then print exactly the symptom this module exists to remove
        ("citeturn0search0" without its boxes), so an orphan that still LOOKS like a
        marker -- an opener followed by an unbroken run of non-whitespace -- is dropped and
        reported. Anything else is ordinary text and is released, minus the strays.
        """
        before = len(self.removed)
        held, self._held = self._held, ""
        if not held:
            return "", []
        body = held.removeprefix(MARKER_OPEN)
        if held.startswith(MARKER_OPEN) and body and not any(ch.isspace() for ch in body):
            self.removed.append(_payload(held))
            return "", self.removed[before:]
        return _drop_strays(held), self.removed[before:]

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    # ---- internals ----------------------------------------------------------------

    def _consume(self, text: str) -> str:
        """Emit everything decided; hold only a possibly-incomplete marker."""
        out: list[str] = []
        while True:
            open_at = text.find(MARKER_OPEN)
            if open_at == -1:
                out.append(_drop_strays(text))
                self._held = ""
                return "".join(out)

            out.append(_drop_strays(text[:open_at]))
            rest = text[open_at:]
            close_at = rest.find(MARKER_CLOSE)
            if close_at == -1:
                if len(rest) > MAX_HOLD_CHARS:
                    # Not a marker after all -- release it rather than stall the answer.
                    out.append(_drop_strays(rest))
                    self._held = ""
                    return "".join(out)
                self._held = rest
                return "".join(out)

            marker = rest[: close_at + 1]
            self.removed.append(_payload(marker))
            text = rest[close_at + 1 :]


def _payload(marker: str) -> str:
    """The reportable part of a marker: its last separated field, else its kind."""
    body = marker.strip(MARKER_OPEN + MARKER_CLOSE)
    parts = [p for p in body.split(MARKER_SEP) if p]
    return parts[-1] if parts else body


def _drop_strays(text: str) -> str:
    """Remove private-use characters that arrived outside an envelope."""
    if not any(ch in text for ch in PRIVATE_USE_STRAYS):
        return text
    return "".join(ch for ch in text if ch not in PRIVATE_USE_STRAYS)


def sanitize_text(text: str) -> tuple[str, list[str]]:
    """One-shot sanitisation for a complete string (the Teams reply, accumulated text)."""
    s = TextSanitizer()
    clean, removed = s.feed(text)
    tail, removed_tail = s.flush()
    return clean + tail, [*removed, *removed_tail]


__all__ = [
    "MARKER_CLOSE",
    "MARKER_OPEN",
    "MARKER_SEP",
    "MAX_HOLD_CHARS",
    "TextSanitizer",
    "sanitize_text",
]
