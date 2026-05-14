"""CLI Templates Subcommand (CTR-0082, PRP-0041).

Provides template management operations via REST API (CTR-0047).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import TYPE_CHECKING

from app.cli.client import client_from_args, output_json

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


def register_templates_parser(
    subparsers: argparse._SubParsersAction,
    add_client_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the 'templates' subcommand parser."""
    templates_parser = subparsers.add_parser("templates", help="Manage prompt templates")
    templates_sub = templates_parser.add_subparsers(dest="templates_action")
    templates_sub.required = True

    # list
    list_parser = templates_sub.add_parser("list", help="List all templates")
    add_client_options(list_parser)
    list_parser.set_defaults(func=_run_templates_list)

    # get
    get_parser = templates_sub.add_parser("get", help="Get template detail")
    add_client_options(get_parser)
    get_parser.add_argument("id", help="Template ID")
    get_parser.set_defaults(func=_run_templates_get)

    # create
    create_parser = templates_sub.add_parser("create", help="Create a new template")
    add_client_options(create_parser)
    create_parser.add_argument("-n", "--name", required=True, help="Template name")
    create_parser.add_argument("-c", "--content", default=None, help="Template body content")
    create_parser.add_argument("-f", "--file", default=None, help="Read body content from file")
    create_parser.add_argument("-d", "--description", default="", help="Template description")
    create_parser.add_argument("--category", default="", help="Template category")
    create_parser.set_defaults(func=_run_templates_create)

    # update
    update_parser = templates_sub.add_parser("update", help="Update a template")
    add_client_options(update_parser)
    update_parser.add_argument("id", help="Template ID")
    update_parser.add_argument("-n", "--name", required=True, help="Template name")
    update_parser.add_argument("-c", "--content", default=None, help="Template body content")
    update_parser.add_argument("-f", "--file", default=None, help="Read body content from file")
    update_parser.add_argument("-d", "--description", default="", help="Template description")
    update_parser.add_argument("--category", default="", help="Template category")
    update_parser.set_defaults(func=_run_templates_update)

    # delete
    delete_parser = templates_sub.add_parser("delete", help="Delete a template")
    add_client_options(delete_parser)
    delete_parser.add_argument("id", help="Template ID")
    delete_parser.set_defaults(func=_run_templates_delete)


def _resolve_body(args: argparse.Namespace) -> str:
    """Resolve template body from --content or --file."""
    if args.content:
        return args.content
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print(f"Error: File '{args.file}' not found.", file=sys.stderr)
            sys.exit(1)
        return path.read_text(encoding="utf-8")
    print("Error: Either --content or --file is required.", file=sys.stderr)
    sys.exit(1)


def _run_templates_list(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.get("/api/templates")
        templates = response.json()

        if args.json_output:
            output_json(templates)
            return

        if not templates:
            print("No templates found.")
            return

        print(f"{'ID':<40} {'Name':<30} {'Updated':<20}")
        print("-" * 92)
        for t in templates:
            tid = t.get("id", "")[:38]
            name = t.get("name", "")[:28]
            updated = t.get("updated_at", "")[:19]
            print(f"{tid:<40} {name:<30} {updated:<20}")
    finally:
        client.close()


def _run_templates_get(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.get(f"/api/templates/{args.id}")
        data = response.json()

        if args.json_output:
            output_json(data)
            return

        print(f"ID:          {data.get('id', '')}")
        print(f"Name:        {data.get('name', '')}")
        print(f"Description: {data.get('description', '')}")
        print(f"Category:    {data.get('category', '')}")
        print(f"Created:     {data.get('created_at', '')}")
        print(f"Updated:     {data.get('updated_at', '')}")
        print(f"\n--- Body ---\n{data.get('body', '')}")
    finally:
        client.close()


def _run_templates_create(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        body = _resolve_body(args)
        payload = {
            "name": args.name,
            "body": body,
            "description": args.description,
            "category": args.category,
        }
        response = client.post("/api/templates", json_data=payload)
        data = response.json()

        if args.json_output:
            output_json(data)
        else:
            print(f"Created template: {data.get('id', '')} ({data.get('name', '')})")
    finally:
        client.close()


def _run_templates_update(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        body = _resolve_body(args)
        payload = {
            "name": args.name,
            "body": body,
            "description": args.description,
            "category": args.category,
        }
        response = client.put(f"/api/templates/{args.id}", json_data=payload)
        data = response.json()

        if args.json_output:
            output_json(data)
        else:
            print(f"Updated template: {args.id} ({data.get('name', '')})")
    finally:
        client.close()


def _run_templates_delete(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        client.delete(f"/api/templates/{args.id}")

        if args.json_output:
            output_json({"status": "deleted", "id": args.id})
        else:
            print(f"Deleted template: {args.id}")
    finally:
        client.close()
