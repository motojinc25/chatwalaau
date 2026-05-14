"""Shared upload validation policy (CTR-0022, CTR-0082, CTR-0078).

Defines the single source of truth for filename normalization, supported
media types, and size limits used by both the REST upload router and the CLI
upload preflight checks.
"""

from __future__ import annotations

from dataclasses import dataclass
import mimetypes
from pathlib import Path

IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
PDF_MEDIA_TYPE = "application/pdf"
ALLOWED_MEDIA_TYPES = frozenset({*IMAGE_MEDIA_TYPES, PDF_MEDIA_TYPE})
MAX_FILE_SIZE_IMAGE = 20 * 1024 * 1024
MAX_FILE_SIZE_PDF = 50 * 1024 * 1024


class UploadValidationError(ValueError):
    """Raised when upload metadata violates the shared validation policy."""


@dataclass(frozen=True)
class UploadValidationResult:
    """Normalized upload metadata after policy validation."""

    safe_filename: str
    content_type: str
    max_size_bytes: int


def allowed_media_types_message() -> str:
    """Return the allowed media types as a stable, comma-separated string."""
    return ", ".join(sorted(ALLOWED_MEDIA_TYPES))


def guess_upload_content_type(file_path: Path) -> str:
    """Infer a local file's content type for CLI preflight validation."""
    return mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"


def max_upload_size_bytes(content_type: str) -> int | None:
    """Return the maximum allowed size for a given upload media type."""
    if content_type == PDF_MEDIA_TYPE:
        return MAX_FILE_SIZE_PDF
    if content_type in IMAGE_MEDIA_TYPES:
        return MAX_FILE_SIZE_IMAGE
    return None


def validate_upload_metadata(
    filename: str | None,
    content_type: str,
    size_bytes: int | None = None,
) -> UploadValidationResult:
    """Validate and normalize upload metadata against the shared policy."""
    normalized_content_type = content_type or ""
    max_size = max_upload_size_bytes(normalized_content_type)
    if max_size is None:
        raise UploadValidationError(
            f"Unsupported file type: {normalized_content_type}. Allowed: {allowed_media_types_message()}"
        )

    if not filename:
        raise UploadValidationError("Filename is required")

    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith("."):
        raise UploadValidationError("Invalid filename")

    if size_bytes is not None and size_bytes > max_size:
        raise UploadValidationError(f"File too large. Maximum size: {max_size // (1024 * 1024)}MB")

    return UploadValidationResult(
        safe_filename=safe_name,
        content_type=normalized_content_type,
        max_size_bytes=max_size,
    )
