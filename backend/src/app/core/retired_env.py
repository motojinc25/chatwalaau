"""Retired provider environment variables are removed from the process (v0.163.0).

PRP-0113 / UDR-0094 moved chat-model configuration into the Model Offering Catalog:
Anthropic on Microsoft Foundry is reached through an offering's ``base_url`` (UDR-0094
D7) and authenticates through its ``api_key_env`` or the shared Entra ID credential.
ChatWalaʻau stopped reading ``ANTHROPIC_FOUNDRY_RESOURCE`` /
``ANTHROPIC_FOUNDRY_BASE_URL`` / ``ANTHROPIC_FOUNDRY_API_KEY``.

Two libraries did not. ``app.main`` loads ``.env`` into ``os.environ``, and both
``agent_framework_anthropic.AnthropicFoundryClient`` (its settings loader) and the
Anthropic SDK's ``AsyncAnthropicFoundry`` fall back to those three names whenever an
argument is ``None``. A leftover ``ANTHROPIC_FOUNDRY_RESOURCE`` therefore made a
catalog ``base_url`` fail with "base_url and resource are mutually exclusive", and a
leftover ``ANTHROPIC_FOUNDRY_API_KEY`` would ride into the Entra ID lane -- the
catalog was not the only source of truth it was designed to be.

So, immediately after ``.env`` is loaded, the three names are REMOVED from the process
environment, unless the catalog itself references one of them (as an ``api_key_env``
or a ``${VAR}`` inside ``endpoint`` / ``base_url``), in which case it is the
operator's chosen variable and stays. Each removal is named once at WARNING during
startup (the PRP-0179 retired-key advisory pattern). Nothing here ever fails startup.

The scrub is idempotent and is repeated by the Anthropic provider right before it
builds a Foundry-hosted client: ``agent_framework_declarative.AgentFactory()`` calls
``load_dotenv()`` on every construction (ChatWalaʻau uses it to validate declarative
agent YAML), which puts a retired value from ``.env`` back after startup.
"""

from __future__ import annotations

import logging
import os

RETIRED_ANTHROPIC_FOUNDRY_ENV_VARS = (
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY",
)

_removed: list[str] = []


def _catalog_referenced_names() -> set[str]:
    try:
        from app import models_catalog

        path = models_catalog.catalog_path()
        if path is None or not path.is_file():
            return set()
        raw = models_catalog.read_raw_catalog(path)
        return set(models_catalog.referenced_env_names(raw)) if isinstance(raw, dict) else set()
    except Exception:  # an unreadable catalog is reported by the catalog loader itself
        return set()


def scrub_retired_provider_env() -> list[str]:
    """Remove the retired Anthropic-on-Foundry variables from ``os.environ``.

    Returns the names removed by THIS call. Idempotent. Never raises.
    """
    try:
        present = [name for name in RETIRED_ANTHROPIC_FOUNDRY_ENV_VARS if name in os.environ]
        if not present:
            return []
        referenced = _catalog_referenced_names()
        removed = []
        for name in present:
            if name in referenced:
                continue
            os.environ.pop(name, None)
            removed.append(name)
            if name not in _removed:
                _removed.append(name)
        return removed
    except Exception:  # a scrub must never break startup
        return []


def warn_scrubbed_env(logger: logging.Logger | None = None) -> None:
    """Name each removed variable once at WARNING (startup advisory)."""
    log = logger or logging.getLogger(__name__)
    for name in _removed:
        log.warning(
            "%s is set in the environment but was retired with the Model Offering Catalog "
            "(PRP-0113); it has been removed from this process so it cannot override the "
            "catalog. Configure Anthropic on Foundry in model_offerings.jsonc instead: "
            "'base_url': 'https://<resource>.services.ai.azure.com/anthropic' and, for key "
            "auth, 'api_key_env'. Delete it from .env to silence this warning.",
            name,
        )
