"""Chat session export/import ZIP bundle codec (CTR-0015, PRP-0084 / UDR-0062).

A bundle is a single ZIP archive that makes one chat self-contained and
portable across ChatWalaʻau instances:

    manifest.json   { format: "chatwalaau.chat", format_version: 1,
                      app_version, exported_at, source_thread_id, title }
    session.json    the full session record (CTR-0014 shape)
    uploads/<file>  every file the session references under .uploads/<thread_id>/

Export (`build_export_bundle`) reads an existing session and archives it with
its whole upload directory. Import (`import_bundle`) treats the archive as
UNTRUSTED input (UDR-0062 D5): it enforces size / entry caps, rejects zip-slip,
allowlists the entry set, validates the manifest and session schema, and only
then -- validate-then-commit -- allocates a NEW thread id (UDR-0062 D3), rehouses
the uploads, rewrites in-message upload references to the new id, strips
per-session personalization (UDR-0062 D4), and persists an ordinary
`.sessions/` record. Structural / security violations (bad zip, zip-slip,
zip-bomb, unknown manifest format_version, malformed session schema, oversize
bundle) raise ``BundleValidationError`` (mapped to HTTP 400) and leave nothing
behind. Individual uploads are classified per-entry (CTR-0015 v1.17): allowed
media and the recognized paint scene sidecar (CTR-0161) are carried, while an
unsupported / oversized / malformed upload is SKIPPED (never written) and
reported in the returned ``warnings`` list so the chat still imports and the
operator is told what was dropped -- a bundle from a newer / differently-laid-out
instance no longer aborts the whole import.

Uses only the Python stdlib ``zipfile`` -- no new dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime
import io
import json
import logging
from pathlib import Path
import shutil
from typing import Any
import uuid
import zipfile

from app.core.config import settings
from app.core.version import get_app_version
from app.session.storage import (
    ensure_session_defaults,
    read_session_json,
    write_session_json,
)
from app.upload.validation import ALLOWED_MEDIA_TYPES, guess_upload_content_type, max_upload_size_bytes

logger = logging.getLogger(__name__)

# Bundle format identity (UDR-0062 D2). A new format_version is added for any
# breaking change; an unknown version is rejected, never reinterpreted.
BUNDLE_FORMAT = "chatwalaau.chat"
BUNDLE_FORMAT_VERSION = 1
SUPPORTED_FORMAT_VERSIONS = frozenset({1})

MANIFEST_NAME = "manifest.json"
SESSION_NAME = "session.json"
UPLOADS_PREFIX = "uploads/"

# Internal zip-bomb defenses applied during import, independent of the
# compressed-upload cap SESSION_IMPORT_MAX_BYTES (UDR-0062 D5).
MAX_BUNDLE_ENTRIES = 2_000
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024  # 256 MiB total expanded size

# Paint scene sidecar (CTR-0161): the editable Fabric.js scene JSON co-located
# with a painted image (`paint_<uuid>.paint.json`, paired 1:1 with
# `paint_<uuid>.png`). Export archives the whole upload dir, so a paint-origin
# chat carries this companion; import recognizes it as a first-class,
# non-media companion and carries it verbatim so the attachment stays
# re-editable after import. Kept in sync with app.paint.router.MAX_SCENE_SIZE_BYTES
# (a local constant avoids a session -> paint import edge).
PAINT_SIDECAR_SUFFIX = ".paint.json"
MAX_SIDECAR_BYTES = 25 * 1024 * 1024

# Per-session metadata fields removed on import so an imported chat lands as an
# ordinary, unfiled, de-personalized new session (UDR-0062 D4).
_STRIPPED_FIELDS = (
    "user_profile_snapshot",
    "pinned_at",
    "continuation_token",
    "service_session_id",
    "auto_title_pending",
    "auto_title_done",
    "memory_extracted_index",
)


class BundleValidationError(ValueError):
    """Raised when an import bundle violates the bundle policy (-> HTTP 400)."""


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def build_export_bundle(thread_id: str) -> tuple[bytes, str, str]:
    """Build a ZIP bundle for a session.

    Returns ``(zip_bytes, ascii_filename, utf8_filename)``: the latin-1-safe
    download name for the Content-Disposition ``filename=`` field and the full
    (possibly non-ASCII) display name for the RFC 5987 ``filename*`` field.
    Raises ``FileNotFoundError`` if the session does not exist.
    """
    data = read_session_json(thread_id)
    if data is None:
        raise FileNotFoundError(thread_id)
    data = ensure_session_defaults(data)

    title = data.get("title", "") or ""
    manifest = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "app_version": get_app_version(),
        "exported_at": datetime.now(UTC).isoformat(),
        "source_thread_id": thread_id,
        "title": title,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(SESSION_NAME, json.dumps(data, ensure_ascii=False, indent=2))

        upload_dir = Path(settings.upload_dir) / thread_id
        if upload_dir.is_dir():
            for file in sorted(upload_dir.iterdir()):
                if file.is_file():
                    archive.write(file, f"{UPLOADS_PREFIX}{file.name}")

    return buffer.getvalue(), _suggested_filename(thread_id, title), _utf8_filename(thread_id, title)


def _utf8_filename(thread_id: str, title: str) -> str:
    """Build a full (possibly non-ASCII) display name for RFC 5987 filename*."""
    # Strip path separators, quotes, and control chars; keep letters/digits/space.
    cleaned = "".join(" " if (ord(c) < 0x20 or c in '\\/:*?"<>|') else c for c in title).strip()
    cleaned = cleaned[:80].strip() or f"chat-{thread_id[:12]}"
    return f"chatwalaau-chat-{cleaned}.zip"


def _suggested_filename(thread_id: str, title: str) -> str:
    """Build an ASCII-safe download filename for the bundle.

    The Content-Disposition `filename=` field must be latin-1 encodable, so this
    keeps ONLY ASCII alphanumerics (CJK / accented title characters -- for which
    `str.isalnum()` is also True -- are dropped, falling back to the thread id).
    The router additionally emits an RFC 5987 `filename*` for the full title.
    """
    slug = "".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "-" for c in title).strip("-")
    slug = slug[:48].strip("-") or thread_id[:12]
    return f"chatwalaau-chat-{slug}.zip"


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def import_bundle(zip_bytes: bytes) -> dict[str, Any]:
    """Validate an import bundle and persist it as a NEW session.

    Returns the new session's summary metadata. Raises ``BundleValidationError``
    on any policy violation, leaving no partial session or stray upload dir.
    """
    if len(zip_bytes) > settings.session_import_max_bytes:
        raise BundleValidationError(
            f"Import bundle too large. Maximum size: {settings.session_import_max_bytes // (1024 * 1024)}MB"
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise BundleValidationError("Not a valid ZIP bundle") from exc

    with archive:
        infos = archive.infolist()
        _enforce_archive_caps(infos)
        upload_entries = _validate_entry_names(infos)

        manifest = _read_json_entry(archive, MANIFEST_NAME)
        _validate_manifest(manifest)
        session_data = _read_json_entry(archive, SESSION_NAME)
        _validate_session_schema(session_data)

        # Classify every upload BEFORE writing anything. Allowed media and the
        # recognized paint sidecar are carried; any other / oversized / malformed
        # upload is SKIPPED (never written) with an operator-facing warning so the
        # chat still imports (UDR-0062 D5 refinement: skip-with-warning for unknown
        # uploads instead of failing the whole bundle). Structural / security
        # violations (bad zip, zip-slip, zip-bomb, bad manifest / session schema,
        # oversize bundle) still hard-fail above.
        validated_uploads: list[tuple[str, bytes]] = []
        warnings: list[str] = []
        for name in upload_entries:
            payload = archive.read(name)
            basename = name[len(UPLOADS_PREFIX) :]
            carry, warning = _classify_upload_entry(basename, payload)
            if warning is not None:
                warnings.append(warning)
            if carry:
                validated_uploads.append((basename, payload))

    # ---- All validation passed; commit (UDR-0062 D3/D4). -----------------
    old_thread_id = str(manifest.get("source_thread_id") or "")
    new_thread_id = str(uuid.uuid4())

    upload_root = Path(settings.upload_dir) / new_thread_id
    try:
        if validated_uploads:
            upload_root.mkdir(parents=True, exist_ok=True)
            for basename, payload in validated_uploads:
                (upload_root / basename).write_bytes(payload)

        new_data = _build_imported_session(session_data, old_thread_id, new_thread_id)
        write_session_json(new_thread_id, new_data)
    except OSError as exc:
        # Roll back any uploads written for this id so nothing is left behind.
        shutil.rmtree(upload_root, ignore_errors=True)
        raise BundleValidationError("Failed to persist imported session") from exc

    logger.info(
        "Imported session %s -> %s (%d uploads, %d skipped)",
        old_thread_id or "?",
        new_thread_id,
        len(validated_uploads),
        len(warnings),
    )
    return {
        "thread_id": new_thread_id,
        "title": new_data.get("title", ""),
        "created_at": new_data.get("created_at", ""),
        "updated_at": new_data.get("updated_at", ""),
        "message_count": new_data.get("message_count", 0),
        "image_count": new_data.get("image_count", 0),
        "pinned_at": None,
        "folder_id": None,
        "source": new_data.get("source", "ag-ui"),
        "auto_title_pending": False,
        # Non-fatal notices about entries that were carried with a caveat or
        # skipped (CTR-0015 v1.17). Empty on a clean import.
        "warnings": warnings,
    }


def _enforce_archive_caps(infos: list[zipfile.ZipInfo]) -> None:
    """Reject archives that are too large or have too many entries (zip-bomb)."""
    if len(infos) > MAX_BUNDLE_ENTRIES:
        raise BundleValidationError("Import bundle has too many entries")
    total = sum(max(info.file_size, 0) for info in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise BundleValidationError("Import bundle expands to too large a size")


def _validate_entry_names(infos: list[zipfile.ZipInfo]) -> list[str]:
    """Allowlist entry names and reject zip-slip; return the uploads/ entries."""
    upload_entries: list[str] = []
    has_manifest = has_session = False
    for info in infos:
        name = info.filename
        if name.endswith("/"):
            continue  # directory marker
        if _is_unsafe_path(name):
            raise BundleValidationError(f"Unsafe path in bundle: {name}")
        if name == MANIFEST_NAME:
            has_manifest = True
        elif name == SESSION_NAME:
            has_session = True
        elif name.startswith(UPLOADS_PREFIX):
            rest = name[len(UPLOADS_PREFIX) :]
            # Single flat level only: uploads/<filename>, no nested directories.
            if not rest or "/" in rest or rest != Path(rest).name:
                raise BundleValidationError(f"Unexpected upload path: {name}")
            upload_entries.append(name)
        else:
            raise BundleValidationError(f"Unexpected entry in bundle: {name}")

    if not has_manifest:
        raise BundleValidationError(f"Bundle is missing {MANIFEST_NAME}")
    if not has_session:
        raise BundleValidationError(f"Bundle is missing {SESSION_NAME}")
    return upload_entries


def _is_unsafe_path(name: str) -> bool:
    """Detect absolute paths, drive letters, and parent-dir escapes (zip-slip)."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return True
    normalized = name.replace("\\", "/")
    if ":" in normalized:  # drive letter e.g. C:
        return True
    return any(part == ".." for part in normalized.split("/"))


def _read_json_entry(archive: zipfile.ZipFile, name: str) -> Any:
    """Read and parse a JSON entry, raising on malformed content."""
    try:
        return json.loads(archive.read(name).decode("utf-8"))
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleValidationError(f"Invalid {name} in bundle") from exc


def _validate_manifest(manifest: Any) -> None:
    """Verify the manifest format id and a supported format_version (UDR-0062 D2)."""
    if not isinstance(manifest, dict):
        raise BundleValidationError("Invalid manifest")
    if manifest.get("format") != BUNDLE_FORMAT:
        raise BundleValidationError("Unrecognized bundle format")
    if manifest.get("format_version") not in SUPPORTED_FORMAT_VERSIONS:
        raise BundleValidationError(f"Unsupported bundle format_version: {manifest.get('format_version')}")


def _validate_session_schema(data: Any) -> None:
    """Validate the minimal session JSON schema (UDR-0062 D5)."""
    if not isinstance(data, dict):
        raise BundleValidationError("Invalid session data")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise BundleValidationError("Session data has no messages list")
    for msg in messages:
        if not isinstance(msg, dict) or "role" not in msg:
            raise BundleValidationError("Malformed message in session data")
        contents = msg.get("contents", [])
        if contents is not None and not isinstance(contents, list):
            raise BundleValidationError("Malformed message contents in session data")


def _classify_upload_entry(basename: str, payload: bytes) -> tuple[bool, str | None]:
    """Decide whether to carry an upload entry into the imported session.

    Returns ``(carry, warning)``. Allowed image / PDF media (CTR-0022) and a
    recognized, well-formed paint scene sidecar (CTR-0161) are carried verbatim
    (``carry=True, warning=None``). Any other, oversized, or malformed entry is
    SKIPPED (``carry=False``) with a human-readable ``warning`` so the chat still
    imports and the operator is told what was dropped -- a bundle produced by a
    newer / differently-laid-out instance no longer fails the whole import
    (UDR-0062 D5 refinement). Skipping never writes the entry, so the fail-closed
    "no untrusted content persisted" posture is preserved.
    """
    if not basename or basename.startswith("."):
        return False, f"Skipped an attachment with an unsafe name: {basename!r}."

    # Paint scene sidecar (CTR-0161): validate as well-formed JSON + size cap and
    # carry it so a paint-origin attachment stays re-editable after import.
    if basename.endswith(PAINT_SIDECAR_SUFFIX):
        if len(payload) > MAX_SIDECAR_BYTES:
            return False, (
                f"Skipped an oversized paint scene '{basename}'; the image still "
                "imports but may no longer be re-editable."
            )
        try:
            json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, (
                f"Skipped a malformed paint scene '{basename}'; the image still "
                "imports but may no longer be re-editable."
            )
        return True, None

    content_type = guess_upload_content_type(Path(basename))
    if content_type not in ALLOWED_MEDIA_TYPES:
        return False, (
            f"Skipped an unsupported attachment '{basename}' ({content_type}); "
            "it may not display correctly in the imported chat."
        )
    max_size = max_upload_size_bytes(content_type)
    if max_size is not None and len(payload) > max_size:
        return False, (
            f"Skipped an oversized attachment '{basename}'; it may not display "
            "correctly in the imported chat."
        )
    return True, None


def _build_imported_session(session_data: dict[str, Any], old_thread_id: str, new_thread_id: str) -> dict[str, Any]:
    """Construct the new session record from imported data (UDR-0062 D3/D4)."""
    data = ensure_session_defaults(dict(session_data))

    # Rewrite every in-payload upload reference from the old thread id to the new
    # one. Both uploaded and generated images reference /api/uploads/<id>/<file>
    # (image_gen reuses the upload infra), so a single segment rewrite covers all.
    if old_thread_id:
        data = _rewrite_upload_refs(data, old_thread_id, new_thread_id)

    now = datetime.now(UTC).isoformat()
    data["thread_id"] = new_thread_id
    data["folder_id"] = None
    data["updated_at"] = now
    if not data.get("created_at"):
        data["created_at"] = now
    for field in _STRIPPED_FIELDS:
        data.pop(field, None)

    messages = data.get("messages", [])
    data["message_count"] = len(messages)
    data["image_count"] = _count_images(messages)
    # Mark provenance so the chat is recognizably an import (non-breaking extra).
    data["source"] = "import"
    return data


def _rewrite_upload_refs(data: dict[str, Any], old_id: str, new_id: str) -> dict[str, Any]:
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


def _count_images(messages: list[dict[str, Any]]) -> int:
    """Count image_url contents and generated images (mirrors the router)."""
    image_gen_tools = frozenset({"generate_image", "edit_image"})
    count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for c in msg.get("contents", []) or []:
            if isinstance(c, dict) and c.get("type") == "image_url":
                count += 1
        for tc in msg.get("tool_calls", []) or []:
            if tc.get("name") not in image_gen_tools:
                continue
            result = tc.get("result", "")
            if not isinstance(result, str):
                continue
            try:
                parsed = json.loads(result)
                count += len(parsed.get("images", []))
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
    return count
