"""CLI Models Subcommand (CTR-0082, PRP-0041).

Provides model information via REST API (CTR-0069, CTR-0070).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.cli.client import client_from_args, output_json

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


def register_models_parser(
    subparsers: argparse._SubParsersAction,
    add_client_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the 'models' subcommand parser."""
    models_parser = subparsers.add_parser("models", help="List available models")
    models_sub = models_parser.add_subparsers(dest="models_action")
    models_sub.required = True

    # list
    list_parser = models_sub.add_parser("list", help="List available models")
    add_client_options(list_parser)
    list_parser.set_defaults(func=_run_models_list)


def _run_models_list(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    try:
        response = client.get("/api/model")
        data = response.json()

        if args.json_output:
            output_json(data)
            return

        models = data.get("models", [])
        default_model = data.get("default_model", "")
        context_map = data.get("max_context_tokens_map", {})

        print("Models:")
        for model in models:
            is_default = " (default)" if model == default_model else ""
            ctx = context_map.get(model)
            ctx_str = f"  context: {ctx:,} tokens" if ctx else ""
            print(f"  {model}{is_default}{ctx_str}")
    finally:
        client.close()
