"""Model Offering Catalog (CTR-0174, PRP-0109, UDR-0087).

An operator-owned JSONC file (default ``model_offerings.jsonc``, configurable
via ``MODEL_OFFERINGS_FILE``) declares the models this deployment serves. Each
entry -- an :class:`Offering` -- self-describes its provider, model_ref,
optional endpoint / base_url / hosting / api_version, an auth reference, its
operations, an option-catalog ``family`` override, a ``context_window``, and a
``default`` flag. A gateway that fronts several first-party model families is
expressed as SEVERAL offerings sharing an ``endpoint`` / ``base_url`` -- no new
provider class is needed (UDR-0087 D5).

Two lanes (UDR-0087 D1):

- File PRESENT  -> the CATALOG lane. The offerings are the single source of
  truth for model routing; the legacy ``*_MODELS`` env namespaces are IGNORED
  for routing (one startup WARNING enumerates them, D11).
- File ABSENT   -> the LEGACY lane. ``active_catalog()`` returns ``None`` and
  every consumer (``app.providers``, CTR-0069 surfaces, the RAG embedder, the
  Images API) uses its existing env configuration byte-for-byte.

DEMO_MODE always takes the legacy lane here: ``active_catalog()`` returns
``None`` when ``DEMO_MODE=true`` so the demo short-circuit in the agent
registry (UDR-0041 / UDR-0045 D7) is unaffected and the catalog is never
consulted for a demo deployment (UDR-0087 D10).

Secrets are NEVER written in the file: an offering references an environment
variable by NAME (``api_key_env`` inline or via a named ``auth_profiles``
entry), or -- for the Entra ID lanes -- falls back to ``app.azure_credential``
(UDR-0087 D4). ``${VAR}`` placeholders in ``endpoint`` / ``base_url`` are
interpolated from the environment at load; an inline ``api_key`` value is
rejected.

Validation is fail-fast at load (UDR-0087 D3): at least one ``chat`` offering,
at most one ``default: true`` chat offering, unique ids, a known provider, a
v1 operation (``chat`` / ``embeddings`` / ``image``; anything else -- audio,
managed agents -- is rejected, not silently ignored), at most one
``embeddings`` offering and at most one ``image`` offering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from app.core.config import settings
from app.mcp.config import _strip_jsonc_comments

logger = logging.getLogger(__name__)

VALID_PROVIDERS = frozenset({"azure-openai", "anthropic", "openai", "foundry"})
VALID_OPERATIONS = frozenset({"chat", "embeddings", "image"})
VALID_FAMILIES = frozenset({"openai-reasoning", "anthropic-adaptive", "bare"})
VALID_HOSTINGS = frozenset({"direct", "foundry"})

# Keys that would embed a raw secret in the catalog file; rejected at load so
# an operator is steered to api_key_env / auth_profiles (UDR-0087 D4).
_INLINE_SECRET_KEYS = ("api_key", "apiKey", "apikey")

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The legacy env namespaces ignored (with a warning) when a catalog is active
# (UDR-0087 D11). Kept here so the message stays in one place.
_LEGACY_NAMESPACES = ("AZURE_OPENAI_MODELS", "ANTHROPIC_MODELS", "OPENAI_MODELS", "FOUNDRY_MODELS")


class CatalogError(ValueError):
    """A model_offerings.jsonc file was present but malformed or invalid.

    Raised at load so a bad catalog fails startup fast with an explicit message
    (UDR-0087 D3), rather than silently degrading to an empty model list.
    """


@dataclass(frozen=True)
class Offering:
    """One model offering (UDR-0087 D2).

    ``model_ref`` is the value passed to the connector as ``model`` (an OpenAI
    model id, an Azure / Foundry deployment name, or a Claude id). ``id`` is the
    operator-chosen, catalog-unique identifier shown in the model selector and
    persisted as the per-message model (UDR-0087 D12). The connector CLASS is
    derived by the owning provider from ``provider`` (+ ``hosting`` for
    anthropic); it is never named in the file.
    """

    id: str
    provider: str
    model_ref: str
    operations: tuple[str, ...] = ("chat",)
    endpoint: str | None = None
    base_url: str | None = None
    api_version: str | None = None
    hosting: str | None = None
    family: str | None = None
    context_window: int | None = None
    default: bool = False
    api_key_env: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_chat(self) -> bool:
        return "chat" in self.operations

    @property
    def is_embeddings(self) -> bool:
        return "embeddings" in self.operations

    @property
    def is_image(self) -> bool:
        return "image" in self.operations

    def api_key(self) -> str | None:
        """Resolve the referenced API key from the environment, or None."""
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


class Catalog:
    """An ordered, validated set of offerings (the CATALOG lane SSOT)."""

    def __init__(self, offerings: list[Offering]) -> None:
        self._offerings = list(offerings)
        self._by_id = {o.id: o for o in offerings}

    def get(self, offering_id: str | None) -> Offering | None:
        if offering_id is None:
            return None
        return self._by_id.get(offering_id)

    def all(self) -> list[Offering]:
        return list(self._offerings)

    def chat_offerings(self) -> list[Offering]:
        """Chat offerings with the default hoisted first (UDR-0087 D3).

        The registry treats the first entry as the default model (CTR-0069),
        so hoisting the chosen default here makes ``resolve_models()`` and
        ``GET /api/model`` agree without a separate default channel.
        """
        chat = [o for o in self._offerings if o.is_chat]
        default = self.default_offering()
        if default is None:
            return chat
        return [default, *[o for o in chat if o.id != default.id]]

    def default_offering(self) -> Offering | None:
        """The explicit ``default: true`` chat offering, else the first chat one."""
        chat = [o for o in self._offerings if o.is_chat]
        if not chat:
            return None
        for o in chat:
            if o.default:
                return o
        return chat[0]

    def embeddings_offering(self) -> Offering | None:
        return next((o for o in self._offerings if o.is_embeddings), None)

    def image_offering(self) -> Offering | None:
        return next((o for o in self._offerings if o.is_image), None)


# ---- Loading + validation (UDR-0087 D1/D2/D3/D4) --------------------------


def _catalog_path() -> Path | None:
    """Resolve the configured catalog path, or None when unset.

    Empty ``MODEL_OFFERINGS_FILE`` -> None (legacy lane). A relative path is
    resolved against the current working directory (mirroring the MCP /
    commands resolvers). A configured-but-missing file is NOT an error: it is
    the legacy lane (UDR-0087 D1), so callers treat a missing file as "no
    catalog".
    """
    raw = (settings.model_offerings_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _interpolate(value: str, offering_id: str, field_name: str) -> str:
    """Replace ``${VAR}`` with ``os.environ[VAR]``; unresolved -> CatalogError.

    The error names the variable, never its value.
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise CatalogError(
                f"offering '{offering_id}': {field_name} references undefined "
                f"environment variable ${{{name}}}"
            )
        return resolved

    return _VAR_RE.sub(repl, value)


def _require_str(entry: dict[str, Any], key: str, offering_id: str) -> str:
    val = entry.get(key)
    if not isinstance(val, str) or not val.strip():
        raise CatalogError(f"offering '{offering_id}': '{key}' is required and must be a non-empty string")
    return val.strip()


def _parse_offering(entry: Any, index: int, auth_profiles: dict[str, str]) -> Offering:
    if not isinstance(entry, dict):
        raise CatalogError(f"offering #{index}: entry must be an object")

    offering_id = entry.get("id")
    if not isinstance(offering_id, str) or not offering_id.strip():
        raise CatalogError(f"offering #{index}: 'id' is required and must be a non-empty string")
    offering_id = offering_id.strip()

    for secret_key in _INLINE_SECRET_KEYS:
        if secret_key in entry:
            raise CatalogError(
                f"offering '{offering_id}': inline secret '{secret_key}' is not allowed; "
                "reference an environment variable via 'api_key_env' or an 'auth_profiles' entry"
            )

    provider = _require_str(entry, "provider", offering_id)
    if provider not in VALID_PROVIDERS:
        raise CatalogError(
            f"offering '{offering_id}': unknown provider {provider!r} "
            f"(expected one of {sorted(VALID_PROVIDERS)})"
        )

    model_ref = _require_str(entry, "model_ref", offering_id)

    raw_ops = entry.get("operations", ["chat"])
    if not isinstance(raw_ops, list) or not raw_ops:
        raise CatalogError(f"offering '{offering_id}': 'operations' must be a non-empty list")
    operations: list[str] = []
    for op in raw_ops:
        if op not in VALID_OPERATIONS:
            raise CatalogError(
                f"offering '{offering_id}': unsupported operation {op!r} "
                f"(v1 supports {sorted(VALID_OPERATIONS)}; audio and managed agents are deferred)"
            )
        if op not in operations:
            operations.append(op)

    hosting = entry.get("hosting")
    if hosting is not None:
        if hosting not in VALID_HOSTINGS:
            raise CatalogError(
                f"offering '{offering_id}': hosting must be one of {sorted(VALID_HOSTINGS)}, got {hosting!r}"
            )
        if provider != "anthropic":
            raise CatalogError(f"offering '{offering_id}': 'hosting' applies only to the anthropic provider")

    family = entry.get("family")
    if family is not None and family not in VALID_FAMILIES:
        raise CatalogError(
            f"offering '{offering_id}': family must be one of {sorted(VALID_FAMILIES)}, got {family!r}"
        )

    context_window = entry.get("context_window")
    if context_window is not None and (not isinstance(context_window, int) or isinstance(context_window, bool) or context_window <= 0):
        raise CatalogError(f"offering '{offering_id}': 'context_window' must be a positive integer")

    default = entry.get("default", False)
    if not isinstance(default, bool):
        raise CatalogError(f"offering '{offering_id}': 'default' must be a boolean")
    if default and "chat" not in operations:
        raise CatalogError(f"offering '{offering_id}': 'default' may only be set on a chat offering")

    # Auth reference: api_key_env inline, or via a named auth_profile.
    api_key_env = entry.get("api_key_env")
    profile_name = entry.get("auth_profile")
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise CatalogError(f"offering '{offering_id}': 'api_key_env' must be a string (an env var name)")
    if profile_name is not None:
        if not isinstance(profile_name, str) or profile_name not in auth_profiles:
            raise CatalogError(f"offering '{offering_id}': unknown auth_profile {profile_name!r}")
        api_key_env = api_key_env or auth_profiles[profile_name]

    endpoint = entry.get("endpoint")
    if endpoint is not None:
        endpoint = _interpolate(str(endpoint), offering_id, "endpoint")
    base_url = entry.get("base_url")
    if base_url is not None:
        base_url = _interpolate(str(base_url), offering_id, "base_url")
    api_version = entry.get("api_version")

    return Offering(
        id=offering_id,
        provider=provider,
        model_ref=model_ref,
        operations=tuple(operations),
        endpoint=endpoint or None,
        base_url=base_url or None,
        api_version=(api_version or None) if isinstance(api_version, str) else None,
        hosting=hosting,
        family=family,
        context_window=context_window,
        default=default,
        api_key_env=(api_key_env or None),
        metadata=entry.get("metadata", {}) if isinstance(entry.get("metadata"), dict) else {},
    )


def _parse_auth_profiles(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("auth_profiles", {})
    if not isinstance(raw, dict):
        raise CatalogError("'auth_profiles' must be an object")
    profiles: dict[str, str] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("api_key_env"), str) or not spec["api_key_env"].strip():
            raise CatalogError(f"auth_profile '{name}': must be an object with a non-empty string 'api_key_env'")
        for secret_key in _INLINE_SECRET_KEYS:
            if secret_key in spec:
                raise CatalogError(f"auth_profile '{name}': inline secret '{secret_key}' is not allowed")
        profiles[name] = spec["api_key_env"].strip()
    return profiles


def parse_catalog(data: Any) -> Catalog:
    """Validate a parsed JSONC object into a :class:`Catalog` (UDR-0087 D2/D3).

    Raises :class:`CatalogError` on any structural or semantic problem.
    """
    if not isinstance(data, dict):
        raise CatalogError("model offerings catalog must be a JSON object")
    raw_offerings = data.get("offerings")
    if not isinstance(raw_offerings, list) or not raw_offerings:
        raise CatalogError("'offerings' must be a non-empty list")

    auth_profiles = _parse_auth_profiles(data)
    offerings = [_parse_offering(entry, i, auth_profiles) for i, entry in enumerate(raw_offerings)]

    # Unique ids.
    seen: set[str] = set()
    for o in offerings:
        if o.id in seen:
            raise CatalogError(f"duplicate offering id {o.id!r}")
        seen.add(o.id)

    # Required-chat + at-most-one-default (D3).
    chat = [o for o in offerings if o.is_chat]
    if not chat:
        raise CatalogError("at least one 'chat' offering is required")
    defaults = [o for o in chat if o.default]
    if len(defaults) > 1:
        raise CatalogError(
            f"at most one chat offering may set default: true (got {[o.id for o in defaults]})"
        )

    # At most one embeddings / image offering (D7).
    if len([o for o in offerings if o.is_embeddings]) > 1:
        raise CatalogError("at most one 'embeddings' offering is supported (v1)")
    if len([o for o in offerings if o.is_image]) > 1:
        raise CatalogError("at most one 'image' offering is supported (v1)")

    return Catalog(offerings)


def load_catalog(path: Path) -> Catalog:
    """Read + parse the catalog file at ``path`` (raises CatalogError on failure)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"failed to read model offerings catalog {path}: {exc}") from exc
    try:
        data = json.loads(_strip_jsonc_comments(raw))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"model offerings catalog {path} is not valid JSON/JSONC: {exc}") from exc
    return parse_catalog(data)


# ---- Active-catalog cache + ignore-with-warning (UDR-0087 D10/D11) --------

_UNSET = object()
_active: Any = _UNSET


def active_catalog() -> Catalog | None:
    """Return the active :class:`Catalog`, or None for the legacy lane.

    Returns None when DEMO_MODE is on (the catalog is ignored for a demo
    deployment, UDR-0087 D10) or when no catalog file is present (the legacy
    env-namespace lane, D1). Resolved once and cached; the first time a catalog
    is found to be active while a legacy ``*_MODELS`` namespace is also set, one
    WARNING enumerates the ignored keys (D11).
    """
    global _active
    if _active is not _UNSET:
        return _active

    if settings.demo_mode:
        # DEMO_MODE always uses the legacy lane (the catalog is ignored, D10).
        logger.info("Model routing source: DEMO_MODE (in-process demo models; catalog ignored)")
        _active = None
        return None

    path = _catalog_path()
    if path is None or not path.is_file():
        # Legacy lane: routing comes from the per-provider .env namespaces.
        configured = (settings.model_offerings_file or "").strip()
        detail = f"MODEL_OFFERINGS_FILE={configured} not found" if configured else "MODEL_OFFERINGS_FILE unset"
        logger.info(
            "Model routing source: .env namespaces (AZURE_OPENAI_MODELS / ANTHROPIC_MODELS / "
            "OPENAI_MODELS / FOUNDRY_MODELS) -- no Model Offering Catalog (%s)",
            detail,
        )
        _active = None
        return None

    catalog = load_catalog(path)  # raises CatalogError -> fail fast

    logger.info(
        "Model routing source: Model Offering Catalog (%s) -- %d offering(s), default=%s",
        path.name,
        len(catalog.all()),
        catalog.default_offering().id if catalog.default_offering() else "(none)",
    )
    ignored = [ns for ns in _LEGACY_NAMESPACES if (os.environ.get(ns) or "").strip()]
    if ignored:
        logger.warning(
            "Model Offering Catalog active (%s); the legacy model namespaces %s are IGNORED "
            "for routing. Remove them from .env or delete the catalog file to use them.",
            path.name,
            ", ".join(ignored),
        )
    _active = catalog
    return catalog


def reset_for_tests() -> None:
    """Discard the cached active catalog so the next call re-resolves it."""
    global _active
    _active = _UNSET


# ---- Convenience accessors used by app.providers / CTR-0069 surfaces ------


def offering_for(offering_id: str | None) -> Offering | None:
    """Return the active-catalog offering for ``offering_id``, or None (legacy)."""
    catalog = active_catalog()
    if catalog is None:
        return None
    return catalog.get(offering_id)


def offering_family(offering_id: str | None) -> str | None:
    """Return the option-catalog family override for an offering, or None."""
    offering = offering_for(offering_id)
    return offering.family if offering is not None else None


def catalog_context_window(offering_id: str | None) -> int | None:
    """Return the offering's declared ``context_window``, or None (legacy)."""
    offering = offering_for(offering_id)
    return offering.context_window if offering is not None else None


# ---- Optional non-chat offering lanes (UDR-0087 D7) -----------------------


@dataclass(frozen=True)
class ResolvedModelConfig:
    """The connection facts a non-chat consumer needs from an offering.

    ``deployment`` is the model / deployment name to pass to the SDK client;
    ``endpoint`` is an Azure/Foundry endpoint (use the AzureOpenAI SDK);
    ``base_url`` is an OpenAI-compatible base URL (use the OpenAI SDK);
    ``api_key`` is the resolved key (None -> the consumer's existing credential
    lane, e.g. Entra ID via app.azure_credential).
    """

    deployment: str
    endpoint: str | None
    base_url: str | None
    api_version: str | None
    api_key: str | None


def _resolved(offering: Offering | None) -> ResolvedModelConfig | None:
    if offering is None:
        return None
    return ResolvedModelConfig(
        deployment=offering.model_ref,
        endpoint=offering.endpoint,
        base_url=offering.base_url,
        api_version=offering.api_version,
        api_key=offering.api_key(),
    )


def embedding_config() -> ResolvedModelConfig | None:
    """Resolved config for the single embeddings offering, or None (legacy).

    Consumed by the RAG embedder (CTR-0075) in place of its dedicated env
    configuration when an ``embeddings`` offering is present (UDR-0087 D7);
    None -> the existing EMBEDDING_DEPLOYMENT_NAME / AZURE_OPENAI_ENDPOINT path.
    """
    catalog = active_catalog()
    if catalog is None:
        return None
    return _resolved(catalog.embeddings_offering())


def image_config() -> ResolvedModelConfig | None:
    """Resolved config for the single image offering, or None (legacy).

    Consumed by image generation (CTR-0049) in place of IMAGE_DEPLOYMENT_NAME /
    AZURE_OPENAI_ENDPOINT when an ``image`` offering is present (UDR-0087 D7).
    Image generation stays on the dedicated Images API (UDR-0045 D7 preserved).
    """
    catalog = active_catalog()
    if catalog is None:
        return None
    return _resolved(catalog.image_offering())
