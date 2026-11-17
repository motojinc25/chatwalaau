"""CLI ``keys`` subcommand (CTR-0033, CTR-0282, PRP-0253, UDR-0234).

Manages the API Key Store directly on disk (``API_KEYS_FILE``, default ``api_keys.jsonc``),
with the same atomic write and audit log as the API (actor ``cli``). A running server picks
the change up by the file's mtime. This is how the first key is made on a server without
web sign-in.

    chatwalaau keys list
    chatwalaau keys create --name NAME --scope responses|api|admin [--expires-days N]
                           [--agent AGENT_ID] [--storage saved|temporary]
    chatwalaau keys set ID [--agent AGENT_ID | --no-agent] [--storage saved|temporary]
    chatwalaau keys reissue ID
    chatwalaau keys disable ID | enable ID
    chatwalaau keys delete ID
"""

from __future__ import annotations

from datetime import timedelta
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

ACTOR = "cli"


def register_keys_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "keys",
        help="Manage API keys in API_KEYS_FILE (PRP-0253)",
        description=(
            "Create, list, reissue, enable, disable and delete managed API keys. Works on the key file "
            "named by API_KEYS_FILE; a running server sees the change without a restart. A secret is "
            "printed once, when it is issued."
        ),
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="Machine-readable JSON output")
    sub = parser.add_subparsers(dest="keys_command", required=True)

    sub.add_parser("list", help="List keys (never secrets)")

    create = sub.add_parser("create", help="Issue a new key and print its secret once")
    create.add_argument("--name", required=True)
    create.add_argument("--scope", required=True, choices=("responses", "api", "admin"))
    create.add_argument("--expires-days", type=int, default=None, help="Days until it expires (default: never)")
    _add_binding_args(create)

    # PRP-0256 (UDR-0237 D1/D8): bind a key to a custom agent and choose its storage.
    setter = sub.add_parser("set", help="Bind a key to a custom agent and/or set its conversation storage")
    setter.add_argument("key_id")
    _add_binding_args(setter)
    setter.add_argument("--no-agent", action="store_true", help="Remove the agent binding")

    for name, help_text in (
        ("reissue", "Issue a new secret for a key; the old one stops working"),
        ("disable", "Keep a key but refuse it"),
        ("enable", "Accept a disabled key again"),
        ("delete", "Delete a key"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("key_id")

    parser.set_defaults(func=run_keys)


def _add_binding_args(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--agent", default=None, help="Custom declarative agent id this key runs (/v1/responses)")
    cmd.add_argument(
        "--storage",
        default=None,
        choices=("saved", "temporary"),
        help="temporary: every /v1/responses request of this key runs temporary",
    )


def _check_agent(agent: str | None) -> None:
    if not agent:
        return
    from app.agent.declarative.loader import resolve_spec
    from app.agent.declarative.spec import CORE_AGENT_ID, DeclarativeAgentError

    try:
        if agent == CORE_AGENT_ID:
            raise DeclarativeAgentError("the Built-in agent cannot be bound")
        resolve_spec(agent)
    except DeclarativeAgentError as exc:
        print(f"Error: {agent!r} is not a usable custom agent ({exc}).", file=sys.stderr)
        sys.exit(1)


def _store():
    from app.api_keys import store as key_store
    from app.core.config import settings

    if not settings.api_keys_file:
        print("API_KEYS_FILE is empty, so managed API keys are off.", file=sys.stderr)
        sys.exit(2)
    return key_store.store_for(settings.api_keys_file)


def _emit(args: argparse.Namespace, payload: dict, text: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json_output else text)


def run_keys(args: argparse.Namespace) -> None:
    from app.api_keys import store as key_store

    store = _store()
    command = args.keys_command
    try:
        if command == "list":
            records = store.list()
            rows = [r.public() for r in records]
            lines = [f"{'ID':10} {'NAME':24} {'SCOPE':10} {'STATE':9} EXPIRES"]
            for r in records:
                state = "disabled" if not r.enabled else ("expired" if r.expired() else "active")
                lines.append(f"{r.id:10} {r.name[:24]:24} {r.scope:10} {state:9} {r.expires_at or 'never'}")
            _emit(args, {"keys": rows}, "\n".join(lines) if records else "No API keys.")
        elif command == "create":
            expires = None
            if args.expires_days is not None:
                expires = (key_store.now_utc() + timedelta(days=args.expires_days)).isoformat()
            agent = getattr(args, "agent", None)
            _check_agent(agent)
            record, secret = store.create(
                name=args.name,
                scope=args.scope,
                expires_at=expires,
                actor=ACTOR,
                agent=agent,
                storage=getattr(args, "storage", None),
            )
            _emit(
                args,
                {"key": record.public(), "secret": secret},
                f"Created {record.name} ({record.scope}, id {record.id}).\n\n  {secret}\n\n"
                "Copy it now. It will not be shown again.",
            )
        elif command == "reissue":
            record, secret = store.reissue(args.key_id, actor=ACTOR)
            _emit(
                args,
                {"key": record.public(), "secret": secret},
                f"Reissued {record.name}. The old secret no longer works.\n\n  {secret}\n\n"
                "Copy it now. It will not be shown again.",
            )
        elif command == "set":
            changes: dict = {}
            if args.no_agent:
                changes["agent"] = None
            elif args.agent is not None:
                _check_agent(args.agent)
                changes["agent"] = args.agent
            if args.storage is not None:
                changes["storage"] = args.storage
            record = store.update(args.key_id, actor=ACTOR, **changes)
            _emit(
                args,
                {"key": record.public()},
                f"{record.name}: agent {record.agent or '(none)'}, storage {record.storage}.",
            )
        elif command in ("disable", "enable"):
            record = store.update(args.key_id, actor=ACTOR, enabled=command == "enable")
            _emit(
                args, {"key": record.public()}, f"{record.name} is now {'enabled' if record.enabled else 'disabled'}."
            )
        elif command == "delete":
            store.delete(args.key_id, actor=ACTOR)
            _emit(args, {"deleted": args.key_id}, f"Deleted {args.key_id}.")
    except key_store.KeyStoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
