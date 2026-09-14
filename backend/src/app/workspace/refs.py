"""Workspace File Reference (CTR-0207, PRP-0166, UDR-0150 D1/D2/D7).

A file the agent made is referenced in assistant Markdown as
``workspace:<workspace-relative POSIX path>``. ``sandbox:<path>`` -- the link form of
OpenAI-hosted code sandboxes, carried in by skills written for that host -- is
accepted as an INPUT alias and normalized to the same path (D2); nothing in this
product emits it.

This is the Python mirror of ``frontend/src/lib/workspace-ref.ts``. Both run
against ONE case table (``tests/fixtures/workspace_refs.json``) in
``tests/invariants/test_prp0166_workspace_file_links.py``, so the grammar cannot
drift between the web renderer and the channels that cannot reach a local route
(the Teams adapter, UDR-0150 D7).

Normalization is presentation, not security: the CTR-0031 jail reached through
CTR-0136 ``/raw`` remains the only authority on what is inside the workspace.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

REF_SCHEMES = ("workspace:", "sandbox:")

# A `%` not followed by two hex digits. decodeURIComponent throws on it, so the
# mirror rejects it too (Python's unquote would silently keep it).
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_LEADING_SEPARATORS = re.compile(r"^(?:\.?/)+")
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")

# A Markdown link or image whose target may be wrapped in <...> and may carry a title.
MARKDOWN_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


def _scheme_of(url: str) -> str | None:
    lower = url.lower()
    return next((s for s in REF_SCHEMES if lower.startswith(s)), None)


def is_workspace_ref_url(url: str | None) -> bool:
    """True when ``url`` uses a workspace-reference scheme, valid path or not."""
    return bool(url) and _scheme_of(url or "") is not None


def normalize_workspace_ref(url: str | None) -> str | None:
    """Return the workspace-relative POSIX path of a reference, or ``None``.

    Tolerated: a leading ``/``, ``//`` or ``./`` (stripped), ``\\`` (converted to
    ``/``), percent-escapes (decoded once). Rejected: an empty path, a ``..``
    segment, a drive letter, a NUL, and a malformed escape. Any other URL is not a
    reference.
    """
    if not url:
        return None
    scheme = _scheme_of(url)
    if scheme is None:
        return None
    raw = url[len(scheme) :]
    if _BAD_ESCAPE.search(raw):
        return None
    try:
        raw = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if "\0" in raw:
        return None
    raw = _LEADING_SEPARATORS.sub("", raw.replace("\\", "/"))
    if _DRIVE_LETTER.match(raw):
        return None
    segments = [s for s in raw.split("/") if s not in ("", ".")]
    if not segments or ".." in segments:
        return None
    return "/".join(segments)


__all__ = ["MARKDOWN_LINK_RE", "REF_SCHEMES", "is_workspace_ref_url", "normalize_workspace_ref"]
