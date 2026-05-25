"""CLI ``env`` subcommand: .env reconciliation (CTR-0097, PRP-0064).

Two offline, server-free actions that reconcile an operator's ``.env``
against the package-embedded ``.env.template`` (the layout SSOT, UDR-0039
D1):

- ``chatwalaau env diff [--json]``
    Read-only. Reports ADDED keys (present in the template, missing from
    your ``.env`` -- new knobs / features to discover) and REMOVED keys
    (active in your ``.env``, no longer in the template -- safe to drop).
    Never writes. Always exits 0.

- ``chatwalaau env sync [--write]``
    Re-renders ``.env`` to the template's canonical layout (keys, comments,
    order) while preserving the operator's values verbatim and quarantining
    unmanaged keys (UDR-0039 D2/D3/D4). DRY-RUN by default: prints the
    unified diff of what ``--write`` would change. With ``--write`` it
    first creates a timestamped, non-clobbering backup
    (``.env.<UTC>.bak``) and then writes the result (UDR-0039 D5).

All rendering logic lives in ``app.core.env_template`` (pure functions);
this module only handles argparse wiring and file I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def register_env_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``env`` subcommand and its ``diff`` / ``sync`` actions."""
    env_parser = subparsers.add_parser(
        "env",
        help="Inspect / reconcile .env against the bundled template (CTR-0097)",
        description=(
            "Reconcile your .env against the package-embedded template that "
            "ships with this release. `env diff` reports new and removed "
            "keys; `env sync` re-renders .env to the template's layout while "
            "preserving your values and keeping a backup. See "
            "assets/docs/guides/env-upgrade.md."
        ),
    )
    env_sub = env_parser.add_subparsers(dest="env_command")

    diff_parser = env_sub.add_parser(
        "diff",
        help="Report keys added / removed relative to the bundled template",
    )
    diff_parser.add_argument(
        "--output",
        "-o",
        default=".env",
        help="Path to the .env file to inspect (default: .env)",
    )
    diff_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Machine-readable JSON output",
    )
    diff_parser.set_defaults(func=_run_env_diff)

    sync_parser = env_sub.add_parser(
        "sync",
        help="Re-render .env to the template layout (dry-run unless --write)",
    )
    sync_parser.add_argument(
        "--output",
        "-o",
        default=".env",
        help="Path to the .env file to reconcile (default: .env)",
    )
    sync_parser.add_argument(
        "--write",
        action="store_true",
        help="Apply changes (default is a dry-run that only prints the diff)",
    )
    sync_parser.add_argument(
        "--backup-dir",
        default=None,
        help="Directory for the timestamped backup (default: alongside .env)",
    )
    sync_parser.set_defaults(func=_run_env_sync)

    # `chatwalaau env` with no action prints the env help instead of erroring.
    env_parser.set_defaults(func=lambda _args: env_parser.print_help())


def _load_template_text() -> str:
    from app.core.env_template import read_template_text

    try:
        return read_template_text()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_env_diff(args: argparse.Namespace) -> None:
    """Execute ``env diff`` (read-only report)."""
    from app.core.env_template import compute_drift

    template_text = _load_template_text()
    env_path = Path(args.output)
    if not env_path.exists():
        if args.json_output:
            print(json.dumps({"error": f"{env_path} not found", "exists": False}))
        else:
            print(f"{env_path} does not exist. Run `chatwalaau init` to create it.")
        return

    env_text = env_path.read_text(encoding="utf-8")
    drift = compute_drift(template_text, env_text)

    if args.json_output:
        print(
            json.dumps(
                {
                    "env_file": str(env_path),
                    "added": drift.added,
                    "removed": drift.removed,
                    "template_key_count": drift.template_key_count,
                    "env_key_count": drift.env_key_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not drift.has_drift:
        print(f"{env_path} is in sync with the bundled template ({drift.template_key_count} keys).")
        return

    if drift.added:
        print(f"ADDED ({len(drift.added)}) -- in the template, not in your {env_path.name}:")
        for key in drift.added:
            print(f"  + {key}")
        print("  These are new settings available in this release. Review them with")
        print("  `chatwalaau env sync` (dry-run) and apply with `--write`.")
        print()
    if drift.removed:
        print(f"REMOVED ({len(drift.removed)}) -- active in your {env_path.name}, not in the template:")
        for key in drift.removed:
            print(f"  - {key}")
        print("  No longer read by this version. `chatwalaau env sync --write` moves")
        print("  them to an 'Unmanaged keys' section (never deletes them).")


def _timestamped_backup_path(env_path: Path, backup_dir: str | None) -> Path:
    """Return a non-clobbering timestamped backup path for env_path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target_dir = Path(backup_dir) if backup_dir else env_path.parent
    candidate = target_dir / f"{env_path.name}.{stamp}.bak"
    # Guarantee no clobber even on sub-second repeated runs.
    counter = 1
    while candidate.exists():
        candidate = target_dir / f"{env_path.name}.{stamp}.{counter}.bak"
        counter += 1
    return candidate


def _run_env_sync(args: argparse.Namespace) -> None:
    """Execute ``env sync`` (dry-run by default; ``--write`` applies)."""
    from app.core.env_template import render, unified_diff

    template_text = _load_template_text()
    env_path = Path(args.output)
    if not env_path.exists():
        print(f"ERROR: {env_path} does not exist. Run `chatwalaau init` first.", file=sys.stderr)
        sys.exit(1)

    current_text = env_path.read_text(encoding="utf-8")
    rendered_text, unmanaged = render(template_text, current_text)
    diff = unified_diff(current_text, rendered_text, path=str(env_path))

    if not diff:
        print(f"{env_path} already matches the bundled template layout. Nothing to do.")
        return

    if not args.write:
        # Dry-run: show exactly what --write would change.
        print(f"--- DRY RUN: `chatwalaau env sync --write` would update {env_path} ---")
        print()
        print(diff, end="" if diff.endswith("\n") else "\n")
        if unmanaged:
            print()
            print(f"Note: {len(unmanaged)} unmanaged key(s) would be preserved in an")
            print(f"'Unmanaged keys' section: {', '.join(unmanaged)}")
        print()
        print("Re-run with --write to apply. A timestamped backup is created first.")
        return

    # Apply.
    backup_path = _timestamped_backup_path(env_path, args.backup_dir)
    if args.backup_dir:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(current_text, encoding="utf-8")
    env_path.write_text(rendered_text, encoding="utf-8")

    print(f"Backed up {env_path} -> {backup_path}")
    print(f"Updated {env_path} to match the bundled template layout.")
    if unmanaged:
        print(f"Preserved {len(unmanaged)} unmanaged key(s) in an 'Unmanaged keys' section:")
        for key in unmanaged:
            print(f"  {key}")


__all__ = ["register_env_parser"]
