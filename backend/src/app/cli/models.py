"""CLI Models Subcommand (CTR-0082, PRP-0041 / PRP-0111).

``models list`` reports models from a running server (read-only, CTR-0069 /
CTR-0070). ``models add`` / ``edit`` / ``remove`` author the local Model
Offering Catalog (``model_offerings.jsonc``, CTR-0174) OFFLINE -- no server is
required, mirroring how ``chatwalaau init`` writes ``.env`` (PRP-0111 / UDR-0090
D5). Authoring reuses the ``app.models_catalog`` serializer + validator, so the
same invariants the server enforces at load are enforced here before any write.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from app.cli.client import client_from_args, output_json

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

# Suggested API-key env-var NAME per provider (never a secret value; UDR-0090 D4).
_DEFAULT_API_KEY_ENV = {
    "azure-openai": "AZURE_OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "foundry": "",  # Entra ID lane; no api key
}
_PROVIDERS = ("azure-openai", "anthropic", "openai", "foundry")
_OPERATIONS = ("chat", "embeddings", "image")
_HOSTINGS = ("direct", "foundry")
_FAMILIES = ("openai-reasoning", "anthropic-adaptive", "bare")


def register_models_parser(
    subparsers: argparse._SubParsersAction,
    add_client_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the 'models' subcommand parser (list + offline authoring)."""
    import argparse as _argparse

    models_parser = subparsers.add_parser("models", help="List or configure models")
    models_sub = models_parser.add_subparsers(dest="models_action")
    models_sub.required = True

    # Subcommand "list": read-only, queries a running server.
    list_parser = models_sub.add_parser("list", help="List available models (from a running server)")
    add_client_options(list_parser)
    list_parser.set_defaults(func=_run_models_list)

    # Subcommand "add": offline authoring; writes model_offerings.jsonc.
    add_parser = models_sub.add_parser("add", help="Add a model offering to model_offerings.jsonc (offline)")
    _add_offering_options(add_parser, _argparse)
    add_parser.set_defaults(func=_run_models_add)

    # Subcommand "edit": offline authoring.
    edit_parser = models_sub.add_parser("edit", help="Edit a model offering in model_offerings.jsonc (offline)")
    edit_parser.add_argument("id", help="The offering id to edit")
    _add_offering_options(edit_parser, _argparse, include_id=False)
    edit_parser.set_defaults(func=_run_models_edit)

    # Subcommand "remove": offline authoring.
    remove_parser = models_sub.add_parser("remove", help="Remove a model offering from model_offerings.jsonc (offline)")
    remove_parser.add_argument("id", help="The offering id to remove")
    remove_parser.add_argument("--file", default=None, help="Catalog path (default: MODEL_OFFERINGS_FILE)")
    remove_parser.set_defaults(func=_run_models_remove)

    # Subcommand "role": assign a chat offering to a background/internal task
    # (PRP-0115, UDR-0096). Offline authoring of the catalog `roles` block.
    role_parser = models_sub.add_parser("role", help="Assign task models (session title, memory, ...) to offerings")
    role_sub = role_parser.add_subparsers(dest="role_action")
    role_sub.required = True

    role_list = role_sub.add_parser("list", help="List task roles and their current offering assignments")
    role_list.add_argument("--file", default=None, help="Catalog path (default: MODEL_OFFERINGS_FILE)")
    role_list.set_defaults(func=_run_role_list)

    role_set = role_sub.add_parser("set", help="Assign a chat offering to a task role")
    role_set.add_argument("role", help="Task role key (see `models role list`)")
    role_set.add_argument("offering_id", help="A chat offering id to bind to this role")
    role_set.add_argument("--file", default=None, help="Catalog path (default: MODEL_OFFERINGS_FILE)")
    role_set.set_defaults(func=_run_role_set)

    role_clear = role_sub.add_parser("clear", help="Unset a task role (falls back to the session / default model)")
    role_clear.add_argument("role", help="Task role key (see `models role list`)")
    role_clear.add_argument("--file", default=None, help="Catalog path (default: MODEL_OFFERINGS_FILE)")
    role_clear.set_defaults(func=_run_role_clear)


def _add_offering_options(parser: argparse.ArgumentParser, argparse_mod: Any, *, include_id: bool = True) -> None:
    """Attach the offering fields as optional flags (prompted when omitted)."""
    if include_id:
        parser.add_argument("--id", default=None, help="Offering id (unique; shown in the model selector)")
    parser.add_argument("--provider", choices=_PROVIDERS, default=None, help="Model provider")
    parser.add_argument("--model-ref", default=None, help="Model id / deployment name passed to the connector")
    parser.add_argument(
        "--operation", choices=_OPERATIONS, action="append", default=None, help="Operation (repeatable; default chat)"
    )
    parser.add_argument("--endpoint", default=None, help="Azure/Foundry endpoint (may use ${VAR})")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible gateway base URL (may use ${VAR})")
    parser.add_argument("--api-version", default=None, help="API version (optional)")
    parser.add_argument("--hosting", choices=_HOSTINGS, default=None, help="Hosting (anthropic provider only)")
    parser.add_argument("--family", choices=_FAMILIES, default=None, help="Option-catalog family override")
    parser.add_argument("--context-window", type=int, default=None, help="Context window (positive int)")
    parser.add_argument("--api-key-env", default=None, help="NAME of the env var holding the API key (never the value)")
    parser.add_argument("--auth-profile", default=None, help="auth_profiles entry name (alternative to --api-key-env)")
    parser.add_argument(
        "--default", action=argparse_mod.BooleanOptionalAction, default=None, help="Make this the default chat model"
    )
    parser.add_argument("--file", default=None, help="Catalog path (default: MODEL_OFFERINGS_FILE)")
    parser.add_argument("-y", "--yes", action="store_true", help="Non-interactive; do not prompt for missing fields")


# ---- list (read-only) -----------------------------------------------------


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


# ---- offline authoring (writes model_offerings.jsonc) ---------------------


def _resolve_path(args: argparse.Namespace):
    from pathlib import Path

    from app import models_catalog

    if getattr(args, "file", None):
        return Path(args.file)
    return models_catalog.catalog_path() or Path("model_offerings.jsonc")


def _load_raw(path: Path) -> dict[str, Any]:
    from app import models_catalog

    try:
        data = models_catalog.read_raw_catalog(path)
    except models_catalog.CatalogError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    if data is None:
        return {"offerings": []}
    if not isinstance(data.get("offerings"), list):
        data["offerings"] = []
    return data


def _prompt(label: str, *, default: str | None = None, choices: tuple[str, ...] | None = None) -> str:
    """Prompt for a value, showing a default and (optionally) the choices."""
    suffix = f" {list(choices)}" if choices else ""
    shown = f" [{default}]" if default else ""
    while True:
        raw = input(f"{label}{suffix}{shown}: ").strip()
        if not raw and default is not None:
            return default
        if not raw:
            continue
        if choices and raw not in choices:
            print(f"  choose one of {list(choices)}")
            continue
        return raw


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _build_offering(args: argparse.Namespace, existing: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble an offering dict from flags, prompting for missing fields when a TTY.

    ``existing`` (edit) supplies current values as defaults; None (add) uses
    provider-derived suggestions. Returns a raw dict (``${VAR}`` / env-var NAMES
    kept verbatim) suitable for ``write_catalog`` (which validates it).
    """
    from app import models_catalog

    base: dict[str, Any] = dict(existing or {})
    prompt_ok = not args.yes and _interactive()

    def pick(
        flag: Any,
        key: str,
        *,
        prompt_label: str | None,
        choices: tuple[str, ...] | None = None,
        default: str | None = None,
    ) -> str | None:
        if flag is not None:
            return flag
        cur = base.get(key)
        if cur is not None:
            default = str(cur)
        if prompt_ok and prompt_label is not None:
            val = _prompt(prompt_label, default=default, choices=choices)
            return val
        return default

    offering_id = pick(
        getattr(args, "id", None), "id", prompt_label="Offering id" if existing is None else None
    ) or base.get("id")
    provider = pick(
        args.provider,
        "provider",
        prompt_label="Provider",
        choices=_PROVIDERS,
        default=base.get("provider") or "azure-openai",
    )
    model_ref = pick(args.model_ref, "model_ref", prompt_label="Model ref (deployment / model id)")

    ops = args.operation or (list(base.get("operations", [])) if existing else None)
    if not ops:
        ops = [_prompt("Operation", default="chat", choices=_OPERATIONS)] if prompt_ok else ["chat"]

    offering: dict[str, Any] = {"id": offering_id, "provider": provider, "model_ref": model_ref, "operations": ops}

    # Default flag (chat only): explicit flag, else keep existing, else prompt / False.
    if args.default is not None:
        make_default = args.default
    elif existing is not None:
        make_default = bool(base.get("default", False))
    elif prompt_ok and "chat" in ops:
        make_default = _prompt("Make this the default chat model?", default="y", choices=("y", "n")) == "y"
    else:
        make_default = False
    if make_default:
        offering["default"] = True

    # Connection facts.
    endpoint_default = base.get("endpoint") or (
        "${AZURE_OPENAI_ENDPOINT}" if provider in ("azure-openai", "foundry") else None
    )
    endpoint = (
        pick(args.endpoint, "endpoint", prompt_label="Endpoint (blank to skip; ${VAR} ok)", default=endpoint_default)
        if (provider in ("azure-openai", "foundry") or args.endpoint or base.get("endpoint"))
        else args.endpoint
    )
    base_url = pick(
        args.base_url,
        "base_url",
        prompt_label="Base URL (OpenAI-compatible gateway; blank to skip)"
        if provider in ("anthropic", "openai")
        else None,
        default=base.get("base_url"),
    )
    conn = (
        ("endpoint", endpoint),
        ("base_url", base_url),
        ("api_version", args.api_version or base.get("api_version")),
    )
    offering.update({key: val for key, val in conn if val})

    if provider == "anthropic":
        hosting = pick(
            args.hosting, "hosting", prompt_label="Hosting", choices=_HOSTINGS, default=base.get("hosting") or "direct"
        )
        if hosting:
            offering["hosting"] = hosting

    family = args.family or base.get("family")
    if family:
        offering["family"] = family

    ctx = args.context_window if args.context_window is not None else base.get("context_window")
    if ctx:
        offering["context_window"] = int(ctx)

    # Auth reference: env-var NAME only (never a value).
    if args.auth_profile or base.get("auth_profile"):
        offering["auth_profile"] = args.auth_profile or base.get("auth_profile")
    else:
        default_key_env = base.get("api_key_env")
        if default_key_env is None:
            default_key_env = _DEFAULT_API_KEY_ENV.get(provider, "")
        key_env = args.api_key_env
        if key_env is None and prompt_ok:
            key_env = _prompt("API key env-var NAME (blank for Entra ID / none)", default=default_key_env or "") or ""
        elif key_env is None:
            key_env = default_key_env or ""
        if key_env:
            offering["api_key_env"] = key_env
            status = "set" if models_catalog.detect_env([key_env]).get(key_env) else "NOT set"
            print(f"  note: environment variable {key_env} is currently {status}")

    return offering


def _write(data: dict[str, Any], path: Path) -> None:
    from app import models_catalog

    try:
        written = models_catalog.write_catalog(data, path)
    except models_catalog.CatalogError as exc:
        print(f"ERROR: {exc}")
        print("Nothing was written. Fix the offering and try again.")
        sys.exit(1)
    print(f"Wrote {written}")


def _run_models_add(args: argparse.Namespace) -> None:
    path = _resolve_path(args)
    data = _load_raw(path)
    offering = _build_offering(args, existing=None)
    if not offering.get("id") or not offering.get("provider") or not offering.get("model_ref"):
        print(
            "ERROR: id, provider, and model_ref are required (pass --id/--provider/--model-ref or run interactively)."
        )
        sys.exit(1)
    if any(o.get("id") == offering["id"] for o in data["offerings"] if isinstance(o, dict)):
        print(f"ERROR: an offering with id '{offering['id']}' already exists; use `models edit {offering['id']}`.")
        sys.exit(1)
    data["offerings"].append(offering)
    _write(data, path)


def _run_models_edit(args: argparse.Namespace) -> None:
    path = _resolve_path(args)
    data = _load_raw(path)
    idx = next((i for i, o in enumerate(data["offerings"]) if isinstance(o, dict) and o.get("id") == args.id), None)
    if idx is None:
        print(f"ERROR: no offering with id '{args.id}' in {path}.")
        sys.exit(1)
    updated = _build_offering(args, existing=dict(data["offerings"][idx]))
    updated["id"] = args.id  # id is the key; not renamed by edit
    data["offerings"][idx] = updated
    _write(data, path)


def _run_models_remove(args: argparse.Namespace) -> None:
    path = _resolve_path(args)
    data = _load_raw(path)
    before = len(data["offerings"])
    data["offerings"] = [o for o in data["offerings"] if not (isinstance(o, dict) and o.get("id") == args.id)]
    if len(data["offerings"]) == before:
        print(f"ERROR: no offering with id '{args.id}' in {path}.")
        sys.exit(1)
    _write(data, path)


# ---- role assignments (writes model_offerings.jsonc `roles`) --------------


def _run_role_list(args: argparse.Namespace) -> None:
    from app import models_catalog

    path = _resolve_path(args)
    data = _load_raw(path)
    roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}
    chat_ids = [
        o.get("id")
        for o in data.get("offerings", [])
        if isinstance(o, dict) and "chat" in (o.get("operations") or ["chat"])
    ]

    if getattr(args, "json_output", False):
        output_json({"roles": roles, "registry": models_catalog.role_registry(), "chat_offerings": chat_ids})
        return

    print("Task model assignments (roles):")
    for role in models_catalog.role_registry():
        key = role["key"]
        bound = roles.get(key)
        if isinstance(bound, dict):
            bound = bound.get("model")
        shown = bound or "(follow session / default)"
        print(f"  {key:<24} {shown}")
        print(f"    {role['description']}")
    if chat_ids:
        print(f"\nChat offerings available: {', '.join(str(c) for c in chat_ids)}")


def _run_role_set(args: argparse.Namespace) -> None:
    from app import models_catalog

    if args.role not in {r.key for r in models_catalog.TASK_ROLES}:
        valid = ", ".join(r.key for r in models_catalog.TASK_ROLES)
        print(f"ERROR: unknown role '{args.role}'. Valid roles: {valid}")
        sys.exit(1)

    path = _resolve_path(args)
    data = _load_raw(path)
    chat_ids = {
        o.get("id")
        for o in data.get("offerings", [])
        if isinstance(o, dict) and "chat" in (o.get("operations") or ["chat"])
    }
    if args.offering_id not in chat_ids:
        print(
            f"ERROR: '{args.offering_id}' is not a chat offering in {path}. "
            f"Available chat offerings: {', '.join(str(c) for c in sorted(chat_ids)) or '(none)'}"
        )
        sys.exit(1)

    roles = dict(data.get("roles")) if isinstance(data.get("roles"), dict) else {}
    roles[args.role] = args.offering_id
    data["roles"] = roles
    _write(data, path)


def _run_role_clear(args: argparse.Namespace) -> None:
    path = _resolve_path(args)
    data = _load_raw(path)
    roles = dict(data.get("roles")) if isinstance(data.get("roles"), dict) else {}
    if args.role in roles:
        del roles[args.role]
    data["roles"] = roles
    _write(data, path)


def offer_first_model_setup() -> None:
    """Interactive "set up your first model" step, chained from ``chatwalaau init``.

    Called by the init command (PRP-0111 / UDR-0090 D5). Skips silently when a
    catalog already exists or when there is no TTY; otherwise offers to author the
    first offering with the same wizard as ``models add``.
    """
    import argparse as _argparse

    from app import models_catalog

    path = models_catalog.catalog_path()
    if path is not None and path.is_file():
        print(f"A model offering catalog already exists ({path.name}); skipping first-model setup.")
        return
    if not _interactive():
        print("To configure your first model later, run: chatwalaau models add")
        return
    print()
    answer = input("Set up your first model now? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        print("Skipped. Run `chatwalaau models add` when you are ready.")
        return
    # Build an all-None namespace so _build_offering prompts for everything.
    ns = _argparse.Namespace(
        id=None,
        provider=None,
        model_ref=None,
        operation=None,
        endpoint=None,
        base_url=None,
        api_version=None,
        hosting=None,
        family=None,
        context_window=None,
        api_key_env=None,
        auth_profile=None,
        default=None,
        file=None,
        yes=False,
    )
    _run_models_add(ns)
