"""CLI entry point for ChatWalaʻau (CTR-0033 v5, PRP-0052, PRP-0057).

Usage:
    chatwalaau                          Start the server
    chatwalaau init                     Initialize .env configuration
    chatwalaau env diff                 Report .env drift vs the template (PRP-0064)
    chatwalaau env sync                 Re-render .env to the template (PRP-0064)
    chatwalaau hash-password            Generate AUTH_PASSWORD_HASH (PRP-0057)
    chatwalaau chat "message"           Chat with the agent
    chatwalaau sessions list            List sessions
    chatwalaau templates list           List templates
    chatwalaau models list              List models
    chatwalaau tts "text"               Text-to-speech
    chatwalaau upload <file> -s <id>    Upload file
    chatwalaau --version                Show version
    chatwalaau --help                   Show help
"""

import argparse
import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys


def _get_version() -> str:
    try:
        return importlib.metadata.version("chatwalaau")
    except importlib.metadata.PackageNotFoundError:
        import tomllib

        pyproject = Path(__file__).resolve().parent.parent.parent.parent / "pyproject.toml"
        if pyproject.exists():
            with pyproject.open("rb") as f:
                return tomllib.load(f).get("project", {}).get("version", "0.0.0")
        return "0.0.0"


def _check_azure_login() -> bool:
    """Check if Azure CLI is logged in."""
    try:
        result = subprocess.run(
            "az account show",
            capture_output=True,
            text=True,
            timeout=30,
            shell=True,
        )
        if result.returncode != 0:
            print(f"  az account show failed: {result.stderr.strip()}")
        return result.returncode == 0
    except FileNotFoundError:
        print("  Azure CLI (az) not found in PATH.")
        return False
    except subprocess.TimeoutExpired:
        print("  Azure CLI login check timed out.")
        return False


def _resolve_active_lane() -> str:
    """Mirror app.azure_credential.get_active_lane() without importing it.

    Importing the helper would force eager construction of azure-identity
    credentials before uvicorn starts (and before pydantic-settings has
    validated the env). This duplicates the precedence logic in 12 lines
    so the CLI banner can describe the active lane without that side effect.
    """
    if os.environ.get("AZURE_OPENAI_API_KEY", "").strip():
        return "api-key"
    mode = (os.environ.get("AZURE_CREDENTIAL_MODE") or "").strip().lower()
    return mode or "cli"


def _run_serve(args: argparse.Namespace) -> None:
    """Start the FastAPI server via uvicorn."""
    # PRP-0059: az-login precheck only applies to the cli credential lane.
    # When the operator has selected api-key, managed-identity, or default
    # mode, `az account show` is irrelevant (cloud hosts cannot run it,
    # and the api-key lane bypasses Entra ID entirely).
    active_lane = _resolve_active_lane()
    if not args.skip_auth_check and active_lane == "cli" and not _check_azure_login():
        print("ERROR: Azure CLI is not logged in.")
        print()
        print("Please run:")
        print("  az login")
        print()
        print("Or skip this check with:")
        print("  chatwalaau --skip-auth-check")
        print()
        print("Alternatively, set AZURE_OPENAI_API_KEY (api-key lane) or")
        print("AZURE_CREDENTIAL_MODE=managed-identity / default in .env.")
        sys.exit(1)
    if active_lane != "cli":
        print(f"Skipping Azure CLI login check (credential lane: {active_lane})")

    import uvicorn

    from app.core.config import settings

    host = args.host or settings.app_host
    port = args.port or settings.app_port
    ssl_certfile = args.ssl_certfile or settings.app_ssl_certfile or None
    ssl_keyfile = args.ssl_keyfile or settings.app_ssl_keyfile or None

    if bool(ssl_certfile) != bool(ssl_keyfile):
        print("ERROR: --ssl-certfile and --ssl-keyfile must both be provided.")
        sys.exit(1)

    protocol = "https" if ssl_certfile else "http"
    print(f"ChatWalaʻau v{_get_version()}")
    print(f"Starting server on {protocol}://{host}:{port}")
    if ssl_certfile:
        print(f"TLS certificate: {ssl_certfile}")
        print(f"TLS private key: {ssl_keyfile}")
    print()

    uvicorn_kwargs: dict = {
        "host": host,
        "port": port,
        "log_config": None,
    }
    if ssl_certfile and ssl_keyfile:
        uvicorn_kwargs["ssl_certfile"] = ssl_certfile
        uvicorn_kwargs["ssl_keyfile"] = ssl_keyfile

    uvicorn.run("app.main:app", **uvicorn_kwargs)


def _run_init(args: argparse.Namespace) -> None:
    """Initialize .env configuration from template."""
    env_path = Path(args.output)
    if env_path.exists() and not args.force:
        print(f"ERROR: {env_path} already exists.")
        print("To reconcile it with this release's template without losing your")
        print("values, run `chatwalaau env diff` then `chatwalaau env sync`.")
        print("Use --force only to discard the existing file and regenerate it.")
        sys.exit(1)

    template_path = Path(__file__).resolve().parent.parent / "templates" / ".env.template"
    if not template_path.exists():
        print("ERROR: .env template not found in package.")
        sys.exit(1)

    content = template_path.read_text(encoding="utf-8")
    env_path.write_text(content, encoding="utf-8")
    print(f"Created {env_path}")
    print()
    print("Next steps:")
    print("  1. Edit .env and set AZURE_OPENAI_ENDPOINT")
    print("  2. Run: az login")
    print("  3. Run: chatwalaau")


def _add_client_options(parser: argparse.ArgumentParser) -> None:
    """Add global client options shared by all client subcommands."""
    import os

    parser.add_argument(
        "--base-url",
        default=os.environ.get("CHATWALAAU_URL", "http://localhost:8000"),
        help="Server base URL (default: CHATWALAAU_URL or http://localhost:8000)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CHATWALAAU_API_KEY", os.environ.get("API_KEY")),
        help="Bearer token (default: CHATWALAAU_API_KEY or API_KEY)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Machine-readable JSON output",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip TLS certificate verification",
    )


def main() -> None:
    """CLI entry point."""
    # Ensure UTF-8 stdout/stderr on Windows so template-derived output
    # (e.g. the okina U+02BB in the .env template header printed by
    # `env diff` / `env sync`) does not crash under the cp1252 console
    # default. Mirrors the reconfigure in app.main.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    from dotenv import load_dotenv

    load_dotenv()

    version = _get_version()
    parser = argparse.ArgumentParser(
        prog="chatwalaau",
        description="ChatWalaʻau - The localhost AI Agent.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"chatwalaau {version}",
    )

    subparsers = parser.add_subparsers(dest="command")

    # Default serve arguments (on root parser)
    parser.add_argument("--host", default=None, help="Bind host (default: APP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: APP_PORT or 8000)")
    parser.add_argument(
        "--skip-auth-check",
        action="store_true",
        help="Skip Azure CLI login check",
    )
    parser.add_argument(
        "--ssl-certfile",
        metavar="PATH",
        default=None,
        help="Path to SSL certificate file (PEM format). "
        "Must be used together with --ssl-keyfile. "
        "Enables HTTPS mode for Secure Context on LAN.",
    )
    parser.add_argument(
        "--ssl-keyfile",
        metavar="PATH",
        default=None,
        help="Path to SSL private key file (PEM format). "
        "Must be used together with --ssl-certfile. "
        "Enables HTTPS mode for Secure Context on LAN.",
    )

    # init subcommand
    init_parser = subparsers.add_parser("init", help="Initialize .env configuration from template")
    init_parser.add_argument("--output", "-o", default=".env", help="Output file path (default: .env)")
    init_parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing file")

    # hash-password subcommand (CTR-0093, PRP-0057)
    from app.cli.hash_password import register_hash_password_parser

    register_hash_password_parser(subparsers)

    # env subcommand: .env reconciliation (CTR-0097, PRP-0064)
    from app.cli.env_tools import register_env_parser

    register_env_parser(subparsers)

    # --- Client subcommands (PRP-0041) ---

    # chat subcommand (CTR-0081)
    from app.cli.chat import register_chat_parser

    register_chat_parser(subparsers, _add_client_options)

    # sessions subcommand (CTR-0082)
    from app.cli.sessions import register_sessions_parser

    register_sessions_parser(subparsers, _add_client_options)

    # templates subcommand (CTR-0082)
    from app.cli.templates import register_templates_parser

    register_templates_parser(subparsers, _add_client_options)

    # models subcommand (CTR-0082)
    from app.cli.models import register_models_parser

    register_models_parser(subparsers, _add_client_options)

    # tts subcommand (CTR-0082)
    from app.cli.tts import register_tts_parser

    register_tts_parser(subparsers, _add_client_options)

    # upload subcommand (CTR-0082)
    from app.cli.upload import register_upload_parser

    register_upload_parser(subparsers, _add_client_options)

    args = parser.parse_args()

    if args.command == "init":
        _run_init(args)
    elif args.command in ("chat", "sessions", "templates", "models", "tts", "upload", "hash-password", "env"):
        # Client and utility subcommands
        args.func(args)
    else:
        _run_serve(args)


if __name__ == "__main__":
    main()
