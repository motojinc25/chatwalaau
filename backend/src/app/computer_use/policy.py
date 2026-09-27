"""Target-window policy and local secrets (PRP-0189 Section 2.10, UDR-0171 D3 / D4).

Two windows are never a target, whatever the operator configures:

* the chat itself (a window titled ``ChatWala...``) -- the agent must not click its own
  Stop button or type into its own composer;
* the secure desktop (UAC, credential prompts, the lock screen), which cannot be driven
  from a user session anyway.

Every other window may be targeted. The window allowlist and the default denial of
command shells / admin consoles were withdrawn before release (PRP-0189 amendment A1):
the operator runs this on their own desktop, and the kill switches plus the target lock
are the safety model (UDR-0171 D3 as amended).

Secrets are resolved locally from ``COMPUTER_USE_SECRETS_FILE`` on each use and never
cached, so a value never reaches the model, a result, a trace or a log (D4).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from app.computer_use.backend import WindowInfo

logger = logging.getLogger(__name__)

_CHAT_TITLE = re.compile(r"chatwala", re.IGNORECASE)
ALWAYS_DENIED_PROCESSES = frozenset({"consent.exe", "credentialuibroker.exe", "logonui.exe", "lockapp.exe"})


def denial_reason(target: WindowInfo) -> str | None:
    """Why ``target`` may not be targeted, or None when it may."""
    if _CHAT_TITLE.search(target.title or ""):
        return "the ChatWalaau window itself is never a target"
    if (target.process or "").lower() in ALWAYS_DENIED_PROCESSES:
        return "secure-desktop and credential windows are never a target"
    return None


# ---- Secrets ------------------------------------------------------------------------------


class SecretUnavailable(Exception):
    pass


def _load() -> dict[str, str]:
    raw = (settings.computer_use_secrets_file or "").strip()
    if not raw:
        raise SecretUnavailable("COMPUTER_USE_SECRETS_FILE is not set")
    path = Path(raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # The path is operator configuration, not a secret; the content never is logged.
        raise SecretUnavailable(f"the secrets file could not be read ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise SecretUnavailable("the secrets file must hold a JSON object of name: value")
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str | int | float)}


def secret_value(name: str) -> str:
    values = _load()
    if name not in values:
        raise SecretUnavailable(f"no secret named {name!r}")
    return values[name]


def secret_values() -> set[str]:
    """Every value, for the secret-literal check (D4). Empty when no file is set."""
    try:
        return set(_load().values())
    except SecretUnavailable:
        return set()


__all__ = [
    "ALWAYS_DENIED_PROCESSES",
    "SecretUnavailable",
    "denial_reason",
    "secret_value",
    "secret_values",
]
