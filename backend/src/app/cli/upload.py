"""CLI Upload Subcommand (CTR-0082, PRP-0041).

Provides file upload to sessions via REST API (CTR-0022).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import TYPE_CHECKING

from app.cli.client import client_from_args, output_json
from app.upload.validation import (
    UploadValidationError,
    guess_upload_content_type,
    validate_upload_metadata,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


def register_upload_parser(
    subparsers: argparse._SubParsersAction,
    add_client_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the 'upload' subcommand parser."""
    upload_parser = subparsers.add_parser("upload", help="Upload file to session")
    add_client_options(upload_parser)
    upload_parser.add_argument("file", help="File path to upload")
    upload_parser.add_argument("-s", "--session", required=True, help="Target session/thread ID")
    upload_parser.set_defaults(func=_run_upload)


def _run_upload(args: argparse.Namespace) -> None:
    file_path = Path(args.file)
    if not file_path.is_file():
        _exit_upload_error(f"File '{args.file}' not found.")

    content_type = guess_upload_content_type(file_path)
    try:
        validated = validate_upload_metadata(
            file_path.name,
            content_type,
            file_path.stat().st_size,
        )
    except UploadValidationError as exc:
        _exit_upload_error(str(exc))

    client = client_from_args(args)
    try:
        with file_path.open("rb") as f:
            response = client.post(
                f"/api/upload/{args.session}",
                files={"file": (validated.safe_filename, f, validated.content_type)},
            )
        data = response.json()

        if args.json_output:
            output_json(data)
        else:
            print(f"Uploaded {data.get('filename', file_path.name)} to session {args.session}")
            print(f"URI: {data.get('uri', '')}")
    finally:
        client.close()


def _exit_upload_error(message: str) -> None:
    """Print a CLI upload validation error and exit non-zero."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)
