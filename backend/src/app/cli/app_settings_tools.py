"""CLI ``settings`` subcommand: application settings store (CTR-0198, PRP-0136).

Two offline, server-free actions over ``app_settings.jsonc``:

- ``chatwalaau settings list [--json]``
    Read-only. Prints every owned setting with its live value, its group, its
    scope, and whether the value comes from the store or from the derived
    default. Never writes.

- ``chatwalaau settings migrate [--write]``
    One-step transcription of a pre-PRP-0136 configuration. PRP-0136 moved 52
    keys out of the env namespace, and because the store is the SOLE source
    (UDR-0120 D2) a value left behind in ``.env`` silently stops applying. This
    reads those residual variables ONCE and writes them into the store, which is
    the only place in the codebase that reads a migrated key's env value.
    DRY-RUN by default: prints what ``--write`` would store.

Migrate never deletes anything from ``.env`` -- ``chatwalaau env sync`` already
owns that job and keeps a timestamped backup (UDR-0039 D5), so this command has
no business rewriting a file it does not own.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def register_settings_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``settings`` subcommand and its ``list`` / ``migrate`` actions."""
    settings_parser = subparsers.add_parser(
        "settings",
        help="Inspect / migrate application settings (CTR-0198)",
        description=(
            "Application settings live in app_settings.jsonc and are edited from the "
            "in-app App Settings screen. `settings list` shows the current values; "
            "`settings migrate` copies keys that PRP-0136 moved out of .env into the "
            "store so they apply again."
        ),
    )
    settings_sub = settings_parser.add_subparsers(dest="settings_command")

    list_parser = settings_sub.add_parser(
        "list",
        help="Print every application setting with its value, group, and scope",
    )
    list_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Machine-readable JSON output",
    )
    list_parser.set_defaults(func=_run_settings_list)

    migrate_parser = settings_sub.add_parser(
        "migrate",
        help="Copy migrated keys still set in .env into app_settings.jsonc",
    )
    migrate_parser.add_argument(
        "--write",
        action="store_true",
        help="Apply the migration (default: dry-run, print what would change)",
    )
    migrate_parser.set_defaults(func=_run_settings_migrate)

    settings_parser.set_defaults(func=_run_settings_help, _parser=settings_parser)


def _run_settings_help(args: argparse.Namespace) -> None:
    args._parser.print_help()


def _run_settings_list(args: argparse.Namespace) -> None:
    from app.app_settings import descriptor_registry, load_store
    from app.app_settings.store import SettingsStoreError, store_path

    try:
        doc = load_store()
    except SettingsStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    rows = []
    for desc in descriptor_registry():
        key = desc["key"]
        stored = key in doc.values
        rows.append(
            {
                "key": key,
                "env_name": desc["env_name"],
                "group": desc["group"],
                "scope": desc["scope"],
                "value": doc.values.get(key, desc["default"]),
                "source": "store" if stored else "default",
            }
        )

    if args.json_output:
        print(
            json.dumps(
                {
                    "path": str(store_path()) if store_path() else None,
                    "present": doc.present,
                    "settings": rows,
                    "unknown": doc.unknown,
                    "warnings": doc.warnings,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    path = store_path()
    print(f"Application settings: {path or '(APP_SETTINGS_FILE unset)'}")
    if not doc.present:
        print("  (no file yet -- every key is using its built-in default)")
    print()

    width = max(len(r["env_name"]) for r in rows)
    current_group = None
    for row in rows:
        if row["group"] != current_group:
            current_group = row["group"]
            print(f"[{current_group}]")
        marker = " " if row["source"] == "store" else "."
        print(f"  {marker} {row['env_name']:<{width}}  {row['value']!r:<24} ({row['scope']})")

    print()
    print("  (a leading '.' means the built-in default; no mark means it is set in the store)")

    if doc.unknown:
        print()
        print("Unrecognized keys (preserved, not applied):")
        for key in sorted(doc.unknown):
            print(f"  ? {key}")

    for warning in doc.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _run_settings_migrate(args: argparse.Namespace) -> None:
    from app.app_settings import load_store, write_store
    from app.app_settings.store import (
        SettingsStoreError,
        coerce_value,
        residual_env_values,
        store_path,
    )

    path = store_path()
    if path is None:
        print("error: APP_SETTINGS_FILE is unset; nothing to migrate into.", file=sys.stderr)
        raise SystemExit(1)

    residual = residual_env_values()
    if not residual:
        print("Nothing to migrate: no relocated variables are set in .env or the environment.")
        return

    try:
        doc = load_store()
    except SettingsStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    planned: dict[str, object] = {}
    skipped: list[str] = []
    failed: list[str] = []

    for key, raw in residual.items():
        if key in doc.values:
            # The store already has an explicit value. It wins: a migration must
            # never overwrite a choice the operator made in the GUI with a stale
            # line they simply forgot to delete from .env.
            skipped.append(key)
            continue
        value, warning = coerce_value(key, raw)
        if warning:
            failed.append(warning)
            continue
        planned[key] = value

    if not planned and not failed:
        print(f"Nothing to migrate: all {len(skipped)} relocated variable(s) are already set in the store.")
        return

    merged = dict(doc.values)
    merged.update(planned)

    print(f"Store: {path}")
    print()
    if planned:
        print(
            f"Would copy {len(planned)} value(s) from .env:"
            if not args.write
            else f"Copied {len(planned)} value(s) from .env:"
        )
        for key in sorted(planned):
            print(f"  + {key.upper()} = {planned[key]!r}")
    if skipped:
        print()
        print(f"Left alone ({len(skipped)} already set in the store):")
        for key in sorted(skipped):
            print(f"  = {key.upper()}")
    if failed:
        print()
        print("Not migrated (value is no longer valid):", file=sys.stderr)
        for message in failed:
            print(f"  ! {message}", file=sys.stderr)

    if not args.write:
        print()
        print("Dry run. Re-run with --write to apply.")
        return

    try:
        write_store(merged, doc.unknown, path)
    except (SettingsStoreError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    print()
    print(f"Wrote {path}")
    print("Restart the server (or reload from App Settings) for the values to take effect.")
    print("The old lines can stay in .env harmlessly; `chatwalaau env sync` will retire them.")


__all__ = ["register_settings_parser"]
