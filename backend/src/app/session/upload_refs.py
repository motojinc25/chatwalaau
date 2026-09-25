"""Upload references inside a session record (CTR-0015, PRP-0174 / UDR-0156 D6).

A message points at its attachments -- uploaded, generated or paint-origin -- as
``/api/uploads/<thread_id>/<filename>``. A session that is created FROM another one
(import, fork) must own its files, otherwise deleting the source chat, which removes
``.uploads/<source_thread_id>/``, breaks every image in the new one.
"""

import json
import logging
from pathlib import Path
import re
import shutil
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# Paint-origin images carry a re-edit sidecar keyed by the image stem (CTR-0022 /
# app.paint.router). It travels with its image.
PAINT_SIDECAR_SUFFIX = ".paint.json"
# The image editor draft (CTR-0222, PRP-0187) also travels with its image, and so do
# the uploads it references (its reference images and layers), with its URLs rewritten.
IMAGE_EDIT_DRAFT_SUFFIX = ".imgedit.json"


def rewrite_upload_refs(data: Any, old_id: str, new_id: str) -> Any:
    """Recursively rewrite /api/uploads/<old_id>/ references to <new_id>."""
    old_seg = f"/api/uploads/{old_id}/"
    new_seg = f"/api/uploads/{new_id}/"

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return node.replace(old_seg, new_seg)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    return walk(data)


def referenced_upload_names(data: Any, thread_id: str) -> set[str]:
    """File names under ``/api/uploads/<thread_id>/`` referenced anywhere in ``data``."""
    pattern = re.compile(rf"/api/uploads/{re.escape(thread_id)}/([^/\"'\s)?#\\]+)")
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            names.update(pattern.findall(node))
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(data)
    return names


def copy_referenced_uploads(data: Any, source_id: str, target_id: str) -> int:
    """Copy the source uploads that ``data`` references into the target's directory.

    Returns how many files were copied. A referenced file that no longer exists is
    skipped and logged (its reference is left for the caller's rewrite to carry). A
    name that would escape the upload root is ignored.
    """
    upload_root = Path(settings.upload_dir).resolve()
    source_dir = (upload_root / source_id).resolve()
    target_dir = (upload_root / target_id).resolve()
    if not source_dir.is_relative_to(upload_root) or not target_dir.is_relative_to(upload_root):
        return 0

    copied = 0
    names = set(referenced_upload_names(data, source_id))
    # An image's editor draft names further uploads (references, layers) the messages
    # may not mention; they belong to the forked session too.
    for name in list(names):
        draft = (source_dir / f"{Path(name).stem}{IMAGE_EDIT_DRAFT_SUFFIX}").resolve()
        if draft.is_relative_to(source_dir) and draft.is_file():
            try:
                names |= referenced_upload_names(json.loads(draft.read_text(encoding="utf-8")), source_id)
            except (OSError, ValueError):
                logger.warning("Image edit draft %s of session %s is unreadable", draft.name, source_id)
    for name in sorted(names):
        candidates = [name, f"{Path(name).stem}{PAINT_SIDECAR_SUFFIX}", f"{Path(name).stem}{IMAGE_EDIT_DRAFT_SUFFIX}"]
        for candidate in candidates:
            src = (source_dir / candidate).resolve()
            if not src.is_relative_to(source_dir):
                continue
            if not src.is_file():
                if candidate == name:
                    logger.warning("Upload %s of session %s is missing; not copied", name, source_id)
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            if candidate.endswith(IMAGE_EDIT_DRAFT_SUFFIX):
                try:
                    draft = rewrite_upload_refs(json.loads(src.read_text(encoding="utf-8")), source_id, target_id)
                    (target_dir / candidate).write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
                    copied += 1
                except (OSError, ValueError):
                    logger.warning("Image edit draft %s of session %s not copied", candidate, source_id)
                continue
            shutil.copy2(src, target_dir / candidate)
            copied += 1
    return copied
