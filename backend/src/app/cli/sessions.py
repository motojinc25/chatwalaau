"""CLI Sessions Subcommand (CTR-0082, PRP-0041).

Provides session management operations via REST API (CTR-0015).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from app.cli.client import _safe_print, client_from_args, output_json

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


def register_sessions_parser(
    subparsers: argparse._SubParsersAction,
    add_client_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the 'sessions' subcommand parser."""
    sessions_parser = subparsers.add_parser("sessions", help="Manage sessions")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_action")
    sessions_sub.required = True

    # list
    list_parser = sessions_sub.add_parser("list", help="List all sessions")
    add_client_options(list_parser)
    list_parser.add_argument("--search", default=None, help="Search sessions by content")
    list_parser.set_defaults(func=_run_sessions_list)

    # get
    get_parser = sessions_sub.add_parser("get", help="Get session detail")
    add_client_options(get_parser)
    get_parser.add_argument("id", help="Session/thread ID")
    get_parser.add_argument("--messages", action="store_true", help="Include messages in output")
    get_parser.set_defaults(func=_run_sessions_get)

    # delete
    delete_parser = sessions_sub.add_parser("delete", help="Delete a session")
    add_client_options(delete_parser)
    delete_parser.add_argument("id", help="Session/thread ID")
    delete_parser.set_defaults(func=_run_sessions_delete)

    # rename
    rename_parser = sessions_sub.add_parser("rename", help="Rename a session")
    add_client_options(rename_parser)
    rename_parser.add_argument("id", help="Session/thread ID")
    rename_parser.add_argument("title", help="New session title")
    rename_parser.set_defaults(func=_run_sessions_rename)

    # archive
    archive_parser = sessions_sub.add_parser("archive", help="Archive a session")
    add_client_options(archive_parser)
    archive_parser.add_argument("id", help="Session/thread ID")
    archive_parser.set_defaults(func=_run_sessions_archive)

    # pin
    pin_parser = sessions_sub.add_parser("pin", help="Pin or unpin a session")
    add_client_options(pin_parser)
    pin_parser.add_argument("id", help="Session/thread ID")
    pin_parser.add_argument("--unpin", action="store_true", help="Unpin instead of pin")
    pin_parser.set_defaults(func=_run_sessions_pin)

    # export
    export_parser = sessions_sub.add_parser("export", help="Export session to file")
    add_client_options(export_parser)
    export_parser.add_argument("id", help="Session/thread ID")
    export_parser.add_argument("-o", "--output", required=True, help="Output file path")
    export_parser.set_defaults(func=_run_sessions_export)


def _run_sessions_list(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        if args.search:
            response = client.get("/api/sessions/search", params={"q": args.search})
        else:
            response = client.get("/api/sessions")

        sessions = response.json()

        if args.json_output:
            output_json(sessions)
            return

        if not sessions:
            print("No sessions found.")
            return

        # Human-friendly table
        print(f"{'ID':<40} {'Title':<30} {'Updated':<20} {'Msgs':>5}")
        print("-" * 99)
        for s in sessions:
            tid = s.get("thread_id", "")[:38]
            title = s.get("title", "")[:28]
            updated = s.get("updated_at", "")[:19]
            count = s.get("message_count", 0)
            pin = "*" if s.get("pinned_at") else " "
            _safe_print(f"{pin}{tid:<39} {title:<30} {updated:<20} {count:>5}")

        if args.search:
            print(f'\n{len(sessions)} result(s) for "{args.search}"')
    finally:
        client.close()


def _run_sessions_get(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.get(f"/api/sessions/{args.id}")
        data = response.json()

        if args.json_output:
            if not args.messages:
                data.pop("messages", None)
            output_json(data)
            return

        print(f"Session: {data.get('thread_id', '')}")
        print(f"Title:   {data.get('title', '')}")
        print(f"Created: {data.get('created_at', '')}")
        print(f"Updated: {data.get('updated_at', '')}")
        print(f"Messages: {data.get('message_count', 0)}")
        print(f"Images:  {data.get('image_count', 0)}")
        pinned = data.get("pinned_at")
        if pinned:
            print(f"Pinned:  {pinned}")

        if args.messages:
            print(f"\n{'--- Messages ---':}")
            for i, msg in enumerate(data.get("messages", [])):
                role = msg.get("role", "unknown")
                contents = msg.get("contents", [])
                text = ""
                for c in contents:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text = c.get("text", "")
                        break
                preview = text[:120] + ("..." if len(text) > 120 else "")
                _safe_print(f"  [{i}] {role}: {preview}")
    finally:
        client.close()


def _run_sessions_delete(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.delete(f"/api/sessions/{args.id}")
        data = response.json()

        if args.json_output:
            output_json(data)
        else:
            print(f"Deleted session: {args.id}")
    finally:
        client.close()


def _run_sessions_rename(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.patch(f"/api/sessions/{args.id}/rename", json_data={"title": args.title})
        data = response.json()

        if args.json_output:
            output_json(data)
        else:
            print(f'Renamed session {args.id} to "{data.get("title", args.title)}"')
    finally:
        client.close()


def _run_sessions_archive(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.post(f"/api/sessions/{args.id}/archive")
        data = response.json()

        if args.json_output:
            output_json(data)
        else:
            print(f"Archived session: {args.id}")
    finally:
        client.close()


def _run_sessions_pin(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        pinned = not args.unpin
        response = client.patch(f"/api/sessions/{args.id}/pin", json_data={"pinned": pinned})
        data = response.json()

        if args.json_output:
            output_json(data)
        else:
            action = "Pinned" if pinned else "Unpinned"
            print(f"{action} session: {args.id}")
    finally:
        client.close()


def _run_sessions_export(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.get(f"/api/sessions/{args.id}")
        data = response.json()

        output_path = Path(args.output)
        # Atomic write: write to temp then rename
        tmp_path = output_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(output_path)
        except OSError as e:
            tmp_path.unlink(missing_ok=True)
            print(f"Error: Failed to write file: {e}", file=sys.stderr)
            sys.exit(1)

        if args.json_output:
            output_json({"exported": str(output_path), "thread_id": args.id})
        else:
            print(f"Exported session {args.id} to {output_path}")
    finally:
        client.close()
