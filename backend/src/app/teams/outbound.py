"""Teams outbound image rendering (CTR-0140, PRP-0092, UDR-0070 D9).

The agent generates images via the image tools, which save them to the session
upload dir and reference them in the reply text as Markdown:
``![alt](/api/uploads/<thread_id>/<file>)``. That URL is a LOCAL ChatWalaʻau route
the Teams Cloud cannot fetch, so a Teams client renders only an empty frame.

These pure helpers extract those upload images from the reply text and load the
actual bytes as a base64 ``data:`` URI so the adapter can send the image DATA
itself as a Teams attachment (not a dead link), and strip the now-redundant image
Markdown from the text reply. SDK-independent, so unit-testable.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
from pathlib import Path
import re

from app.core.config import settings

logger = logging.getLogger(__name__)

# Markdown image whose target is a session upload: ![alt](/api/uploads/<thread>/<file>)
_UPLOAD_IMG_RE = re.compile(r"!\[[^\]]*\]\((/api/uploads/[^)\s]+)\)")

_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Skip inlining an image larger than this (raw bytes) to avoid an oversized Teams
# payload; the Markdown link is left in the text as a graceful fallback.
_MAX_INLINE_BYTES = 4 * 1024 * 1024  # 4 MiB


@dataclass(frozen=True)
class TeamsImage:
    """An image ready to attach to a Teams message as inline data."""

    filename: str
    content_type: str
    data_uri: str


def extract_upload_images(text: str, thread_id: str) -> list[TeamsImage]:
    """Return the inline images for every /api/uploads Markdown image in ``text``.

    Reads each referenced file from the upload dir and encodes it as a base64
    ``data:`` URI. A missing or oversized file is skipped (logged), so the reply
    still sends. ``thread_id`` is used only as a fallback when a reference omits
    its thread segment.
    """
    images: list[TeamsImage] = []
    for uri in _UPLOAD_IMG_RE.findall(text or ""):
        # uri == /api/uploads/<thread>/<file>
        parts = uri.split("/")
        if len(parts) < 5:
            continue
        ref_thread, filename = parts[3] or thread_id, parts[4]
        path = Path(settings.upload_dir) / ref_thread / filename
        try:
            raw = path.read_bytes()
        except OSError:
            logger.debug("Teams outbound image not found: %s", path)
            continue
        if len(raw) > _MAX_INLINE_BYTES:
            logger.info("Teams image %s too large to inline (%d bytes); leaving link", filename, len(raw))
            continue
        content_type = _CONTENT_TYPES.get(path.suffix.lower(), "image/png")
        data_uri = f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"
        images.append(TeamsImage(filename=filename, content_type=content_type, data_uri=data_uri))
    return images


def strip_upload_image_markdown(text: str) -> str:
    """Remove ![...](/api/uploads/...) markdown (the image is sent as an attachment)."""
    return _UPLOAD_IMG_RE.sub("", text or "").strip()


def rewrite_workspace_refs(text: str) -> str:
    """Rewrite workspace file references to plain text (CTR-0207, UDR-0150 D7).

    A ``workspace:`` / ``sandbox:`` link or image targets a local ChatWalaʻau route a
    Teams client cannot reach -- the same reason UDR-0070 D9 inlines generated
    images -- so it becomes ``label (workspace file: <path>)``. The file itself is
    NOT attached: sending workspace files into a Teams conversation is a data-egress
    decision outside PRP-0166. A reference whose path is rejected keeps only its
    label. Every other link (including ``/api/uploads`` images, handled above) is
    left untouched. The persisted session keeps the original text.
    """
    from app.workspace.refs import MARKDOWN_LINK_RE, is_workspace_ref_url, normalize_workspace_link_target

    def _rewrite(match: re.Match[str]) -> str:
        label, target = match.group(2), match.group(3)
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        path = normalize_workspace_link_target(target)
        if path is None:
            # A rejected scheme form keeps its label; anything else is an ordinary link.
            return label if is_workspace_ref_url(target) else match.group(0)
        return f"{label or path.rsplit('/', 1)[-1]} (workspace file: {path})"

    return MARKDOWN_LINK_RE.sub(_rewrite, text or "")
