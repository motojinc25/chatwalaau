"""Plain-text shell output (PRP-0181, UDR-0163 D7).

A shell command's output reaches two readers: the tool card on screen and the MODEL, as
the tool result. PowerShell 7 styles table headers (``Format-Table``, e.g.
``Get-ChildItem``) with ANSI SGR sequences even when its output is redirected; the ESC
byte is invisible in the card, so the reader saw ``[32;1mMode [0m`` and the model paid
tokens to read around it.

Two layers, used by both shell tools -- the harness ``run_shell``
(``app.agent.harness.shell``, CTR-0193) and the Prompt-lane ``bash_execute``
(``app.coding.tools``, CTR-0031):

- ``plain_text_env()`` -- the child's environment with ``NO_COLOR=1`` added (read by
  PowerShell 7.4+ and most CLIs). It only REDUCES the work.
- ``strip_ansi()`` -- removes CSI and OSC escape sequences. This is the GUARANTEE, and it
  runs before any truncation so the size limit counts real text.
"""

from __future__ import annotations

import os
import re

# CSI: ESC [ <parameter bytes 0x30-0x3F>* <intermediate bytes 0x20-0x2F>* <final byte 0x40-0x7E>
# OSC: ESC ] ... terminated by BEL or ST (ESC \)
# Other two-byte escapes: ESC followed by one byte in 0x40-0x5F (e.g. ESC M), except
# '[' and ']' which open the two forms above.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\^_]"
)


def strip_ansi(text: str) -> str:
    """Return *text* without ANSI escape sequences. Text without ESC is returned as is."""
    if not text or "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


def plain_text_env() -> dict[str, str]:
    """The inherited environment plus ``NO_COLOR=1`` for a shell child process."""
    return {**os.environ, "NO_COLOR": "1"}


__all__ = ["plain_text_env", "strip_ansi"]
