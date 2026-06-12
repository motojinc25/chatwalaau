"""CLI entry point for ChatWalaʻau.

Usage:
    chatwalaau              Start the server
    chatwalaau init         Initialize .env configuration from template
    chatwalaau --version    Show version
    chatwalaau --help       Show help
"""

import argparse
from pathlib import Path
import subprocess
import sys

from app.core.version import get_app_version


def _get_version() -> str:
    # UDR-0044 D2: delegate to the single shared version helper.
    return get_app_version()


def _check_azure_login() -> bool:
    """Check if Azure CLI is logged in."""
    try:
        result = subprocess.run(
            "az account show",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=True,
        )
        if result.returncode != 0:
            print(f"  az account show failed: {(result.stderr or '').strip()}")
        return result.returncode == 0
    except FileNotFoundError:
        print("  Azure CLI (az) not found in PATH.")
        return False
    except subprocess.TimeoutExpired:
        print("  Azure CLI login check timed out.")
        return False


def _run_serve(args: argparse.Namespace) -> None:
    """Start the FastAPI server via uvicorn."""
    from dotenv import load_dotenv

    load_dotenv()

    if not args.skip_auth_check and not _check_azure_login():
        print("ERROR: Azure CLI is not logged in.")
        print()
        print("Please run:")
        print("  az login")
        print()
        print("Or skip this check with:")
        print("  chatwalaau --skip-auth-check")
        sys.exit(1)

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
        print("Use --force to overwrite.")
        sys.exit(1)

    template_path = Path(__file__).parent / "templates" / ".env.template"
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


def main() -> None:
    """CLI entry point."""
    version = _get_version()
    parser = argparse.ArgumentParser(
        prog="chatwalaau",
        description="ChatWalaʻau - Hawaii-built localhost-first AI agent, powered by Microsoft Agent Framework.",
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
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize .env configuration from template",
    )
    init_parser.add_argument(
        "--output",
        "-o",
        default=".env",
        help="Output file path (default: .env)",
    )
    init_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing file",
    )

    args = parser.parse_args()

    if args.command == "init":
        _run_init(args)
    else:
        _run_serve(args)


if __name__ == "__main__":
    main()
