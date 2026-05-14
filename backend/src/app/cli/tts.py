"""CLI TTS Subcommand (CTR-0082, PRP-0041).

Provides text-to-speech generation via REST API (CTR-0039).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import TYPE_CHECKING

from app.cli.client import client_from_args, output_json

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


def register_tts_parser(
    subparsers: argparse._SubParsersAction,
    add_client_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the 'tts' subcommand parser."""
    tts_parser = subparsers.add_parser("tts", help="Text-to-speech synthesis")
    add_client_options(tts_parser)
    tts_parser.add_argument("text", nargs="?", default=None, help="Text to synthesize")
    tts_parser.add_argument("-f", "--file", default=None, help="Read input text from file")
    tts_parser.add_argument("-o", "--output", default=None, help="Output audio file path (default: stdout)")
    tts_parser.set_defaults(func=_run_tts)


def _run_tts(args: argparse.Namespace) -> None:
    text = _resolve_text(args)
    client = client_from_args(args)
    try:
        response = client.post("/api/tts", json_data={"text": text})
        audio_data = response.content

        if args.output:
            output_path = Path(args.output)
            # Atomic write
            tmp_path = output_path.with_suffix(".tmp")
            try:
                tmp_path.write_bytes(audio_data)
                tmp_path.replace(output_path)
            except OSError as e:
                tmp_path.unlink(missing_ok=True)
                print(f"Error: Failed to write file: {e}", file=sys.stderr)
                sys.exit(1)

            if args.json_output:
                output_json({"output": str(output_path), "size_bytes": len(audio_data)})
            else:
                size_kb = len(audio_data) / 1024
                print(f"Audio saved to {output_path} ({size_kb:.1f} KB)")
        else:
            # Write raw audio to stdout
            if args.json_output:
                print("Error: --output is required with --json (cannot mix binary and JSON).", file=sys.stderr)
                sys.exit(1)
            sys.stdout.buffer.write(audio_data)
    finally:
        client.close()


def _resolve_text(args: argparse.Namespace) -> str:
    """Resolve text from positional argument or --file."""
    if args.text:
        return args.text
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print(f"Error: File '{args.file}' not found.", file=sys.stderr)
            sys.exit(1)
        return path.read_text(encoding="utf-8")
    print('Error: Text required. Use: chatwalaau tts "text" or chatwalaau tts -f file.txt', file=sys.stderr)
    sys.exit(1)
