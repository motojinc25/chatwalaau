"""CLI ``hash-password`` subcommand (CTR-0033 v5, CTR-0093, PRP-0057).

Generates an ``AUTH_PASSWORD_HASH`` value for ``backend/.env`` using
``hashlib.scrypt`` (stdlib, no extra dependency). Three input modes:

- Default (interactive): prompts via ``getpass`` twice (confirmation).
- ``--no-confirm``: prompts once (single typing). Still uses ``getpass``
  so the password never appears in the shell history.
- ``--stdin``: reads one line from stdin without echo handling. Suited
  for headless / CI pipelines that already have the password in a
  secret manager. The trailing newline is stripped.

Output is the canonical hash string on a single line; with the default
human-readable preface, or just the bare value when ``--quiet`` is
set. See ``assets/docs/guides/web-auth.md`` for the full operator
runbook (Section 3).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def register_hash_password_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``hash-password`` subcommand parser."""
    parser = subparsers.add_parser(
        "hash-password",
        help="Generate AUTH_PASSWORD_HASH for backend/.env (CTR-0093, PRP-0057)",
        description=(
            "Generate a scrypt password hash compatible with the "
            "AUTH_PASSWORD_HASH key in backend/.env. By default, prompts "
            "interactively for the password (twice, for confirmation). "
            "Use --stdin to pipe the password from a script or secret "
            "manager. See assets/docs/guides/web-auth.md for the full "
            "operator runbook."
        ),
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help=(
            "Read the password from stdin (single line, trailing newline "
            "stripped). Skips the confirmation prompt. Recommended for "
            "scripted / CI flows."
        ),
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help=(
            "Prompt only once instead of twice. Use only when you are sure the password is correct (no typo recovery)."
        ),
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Print only the hash value, with no human-readable preface.",
    )
    parser.set_defaults(func=_run_hash_password)


def _read_password(args: argparse.Namespace) -> str:
    """Read the password from stdin or via getpass per ``args``."""
    if args.stdin:
        line = sys.stdin.readline()
        if not line:
            print("ERROR: stdin closed without sending a password.", file=sys.stderr)
            sys.exit(2)
        # Strip only the trailing newline, not internal whitespace.
        return line.rstrip("\n").rstrip("\r")

    import getpass

    try:
        password = getpass.getpass("Password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)

    if not password:
        print("ERROR: password must not be empty.", file=sys.stderr)
        sys.exit(2)

    if not args.no_confirm:
        try:
            confirm = getpass.getpass("Confirm:  ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            sys.exit(130)
        if password != confirm:
            print("ERROR: passwords do not match.", file=sys.stderr)
            sys.exit(2)

    return password


def _run_hash_password(args: argparse.Namespace) -> None:
    """Execute the ``hash-password`` subcommand."""
    # Imported lazily so ``chatwalaau --help`` does not pay the cost of
    # loading the hashing module.
    from app.auth.password import generate_hash

    password = _read_password(args)
    hash_value = generate_hash(password)

    if args.quiet:
        print(hash_value)
        return

    print()
    print("Generated AUTH_PASSWORD_HASH (paste into backend/.env):")
    print()
    print(f"  AUTH_PASSWORD_HASH={hash_value}")
    print()
    print("Next steps:")
    print("  - Set AUTH_USERNAME in backend/.env (single allowed username)")
    print("  - Restart the backend to pick up the new hash")
    print("  - Full runbook: assets/docs/guides/web-auth.md")


__all__ = ["register_hash_password_parser"]
