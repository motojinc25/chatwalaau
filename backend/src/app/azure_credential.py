"""Shared Azure OpenAI credential resolution (PRP-0058, PRP-0059, UDR-0034).

Centralizes the credential decision for every Azure OpenAI client in the
runtime so that all consumers (MAF AgentRegistry, MAF DevUI agent, Whisper
STT, image generation, RAG ingest embedder, RAG query embedder) agree on
which lane is active.

Four lanes (PRP-0059 extends PRP-0058's original two):

- AZURE_OPENAI_API_KEY set   -> api_key= shape on every Azure OpenAI client.
- AZURE_OPENAI_API_KEY unset, dispatched by AZURE_CREDENTIAL_MODE:
  - cli (default)    -> AzureCliCredential() (localhost development).
  - managed-identity -> ManagedIdentityCredential() (Azure App Service,
                        Container Apps, AKS, Functions, VM).
  - default          -> DefaultAzureCredential(exclude_interactive_browser_credential=True)
                        (auto-discovery chain).

Precedence: API key > AZURE_CREDENTIAL_MODE. This matches UDR-0034
Decision 2 (unchanged from PRP-0058).

The module is consumed from both the main backend process and the batch
MCP server entry point (`python -m app.mcp_batch.server`). Both processes
call `dotenv.load_dotenv()` before reaching consumers, so reading the
value with `os.environ.get()` works uniformly without forcing the batch
server to construct the full `Settings` object.

Caching: credential instances and bearer-token provider callables are
cached at module scope per active mode on first use, preserving the
per-process reuse pattern established by PRP-0047 for the RAG embedder.
The env vars themselves are read on every call so test fixtures that
mutate the process environment take effect immediately.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from azure.identity import (
    AzureCliCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from azure.core.credentials import TokenCredential

logger = logging.getLogger(__name__)

AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"

VALID_CREDENTIAL_MODES = frozenset({"cli", "managed-identity", "default"})

# Module-level caches: one credential per mode, one bearer-token
# provider per mode. Reset only by reset_for_tests().
_credential_cache: dict[str, TokenCredential] = {}
_token_provider_cache: dict[str, Callable[[], str]] = {}

# One INFO log line per lane per process (UDR-0034 Decision 7,
# extended by PRP-0059 for the new lane names).
_logged_lanes: set[str] = set()


def _api_key() -> str:
    """Return the configured API key (stripped) or empty string."""
    return os.environ.get("AZURE_OPENAI_API_KEY", "").strip()


def _mode() -> str:
    """Return the configured credential mode (normalised), defaulting to 'cli'.

    Empty or unset is treated as 'cli' so pre-PRP-0059 deployments
    keep their existing behaviour with no operator action.
    """
    raw = (os.environ.get("AZURE_CREDENTIAL_MODE") or "").strip().lower()
    return raw or "cli"


def _tenant_id() -> str | None:
    """Return AZURE_TENANT_ID stripped, or None if unset.

    When set, this pin is forwarded to AzureCliCredential and
    DefaultAzureCredential so token acquisition targets the same
    tenant that hosts the Azure OpenAI resource. Without it, the
    Azure OpenAI service rejects requests with HTTP 400:

        "Token tenant <...> does not match resource tenant."

    when the operator's default `az account show` tenant is a
    different tenant than the resource's home tenant. ManagedIdentity
    is tenant-bound by the identity assignment, so the value is not
    forwarded to it.
    """
    raw = (os.environ.get("AZURE_TENANT_ID") or "").strip()
    return raw or None


def _get_credential() -> TokenCredential:
    """Return the process-cached TokenCredential for the active mode."""
    mode = _mode()
    if mode not in VALID_CREDENTIAL_MODES:
        raise ValueError(f"AZURE_CREDENTIAL_MODE must be one of {sorted(VALID_CREDENTIAL_MODES)}, got {mode!r}")
    cached = _credential_cache.get(mode)
    if cached is not None:
        return cached
    tenant_id = _tenant_id()
    if mode == "cli":
        cred: TokenCredential = AzureCliCredential(tenant_id=tenant_id)
    elif mode == "managed-identity":
        # Managed Identity is tenant-bound at the identity assignment;
        # tenant_id is not a constructor parameter.
        cred = ManagedIdentityCredential()
    else:  # mode == "default"
        cred = DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
            tenant_id=tenant_id,
        )
    _credential_cache[mode] = cred
    return cred


def _get_token_provider() -> Callable[[], str]:
    """Return the process-cached bearer-token provider callable for the active mode."""
    mode = _mode()
    cached = _token_provider_cache.get(mode)
    if cached is not None:
        return cached
    provider = get_bearer_token_provider(_get_credential(), AZURE_OPENAI_SCOPE)
    _token_provider_cache[mode] = provider
    return provider


def get_active_lane() -> str:
    """Return a short name for the active credential lane.

    One of: "api-key", "cli", "managed-identity", "default".
    Useful for diagnostic surfaces (CLI banner, INFO log).
    """
    if _api_key():
        return "api-key"
    return _mode()


_LANE_DESCRIPTIONS = {
    "api-key": "api-key (AZURE_OPENAI_API_KEY set)",
    "cli": "cli (AzureCliCredential)",
    "managed-identity": "managed-identity (ManagedIdentityCredential)",
    "default": "default (DefaultAzureCredential)",
}


def _log_active_lane(lane: str) -> None:
    """Emit one INFO line per lane observed in this process (no values)."""
    if lane in _logged_lanes:
        return
    _logged_lanes.add(lane)
    description = _LANE_DESCRIPTIONS.get(lane, lane)
    logger.info("Azure OpenAI credential lane: %s", description)


def get_chat_client_credential_kwargs() -> dict[str, Any]:
    """Return constructor kwargs for MAF `OpenAIChatClient` and peers.

    Spread with `**` into the constructor::

        client = OpenAIChatClient(
            model=model,
            azure_endpoint=settings.azure_openai_endpoint or None,
            **get_chat_client_credential_kwargs(),
        )

    Returns:
        {"api_key": <key>}            when AZURE_OPENAI_API_KEY is set
        {"credential": <cached cred>} otherwise (mode-selected)
    """
    key = _api_key()
    if key:
        _log_active_lane("api-key")
        return {"api_key": key}
    cred = _get_credential()
    _log_active_lane(_mode())
    return {"credential": cred}


def get_azure_openai_kwargs() -> dict[str, Any]:
    """Return constructor kwargs for `openai.AzureOpenAI`.

    Spread with `**` into the constructor::

        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version="2024-10-21",
            **get_azure_openai_kwargs(),
        )

    Returns:
        {"api_key": <key>}                          when AZURE_OPENAI_API_KEY is set
        {"azure_ad_token_provider": <cached cb>}    otherwise (mode-selected)
    """
    key = _api_key()
    if key:
        _log_active_lane("api-key")
        return {"api_key": key}
    provider = _get_token_provider()
    _log_active_lane(_mode())
    return {"azure_ad_token_provider": provider}


def reset_for_tests() -> None:
    """Discard module-level caches and forget logged lanes.

    Tests that toggle AZURE_OPENAI_API_KEY or AZURE_CREDENTIAL_MODE
    between scenarios call this so a new credential instance is
    constructed and the INFO log line is re-emitted for the next lane.
    """
    _credential_cache.clear()
    _token_provider_cache.clear()
    _logged_lanes.clear()
