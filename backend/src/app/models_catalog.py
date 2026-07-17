"""Model Offering Catalog (CTR-0174, PRP-0109, UDR-0087).

An operator-owned JSONC file (default ``model_offerings.jsonc``, configurable
via ``MODEL_OFFERINGS_FILE``) declares the models this deployment serves. Each
entry -- an :class:`Offering` -- self-describes its provider, model_ref,
optional endpoint / base_url / hosting / api_version, an auth reference, its
operations, an option-catalog ``family`` override, a ``context_window``, and a
``default`` flag. A gateway that fronts several first-party model families is
expressed as SEVERAL offerings sharing an ``endpoint`` / ``base_url`` -- no new
provider class is needed (UDR-0087 D5).

Chat-model routing (PRP-0113, UDR-0094 D1): the catalog is the SOLE routing
source for a non-demo deployment. The legacy per-provider ``*_MODELS`` env
namespaces have been REMOVED -- there is no env-namespace fallback lane. A
non-demo runtime with no chat offering does NOT crash: the server boots with an
empty agent registry and a WARNING (``app.agui.agent_registry``) and surfaces the
actionable error only when a chat model is actually requested. ``active_catalog()``
itself returns ``None`` (no file present) without raising so read-only callers
stay safe.

- File PRESENT  -> the CATALOG lane. The offerings are the single source of
  truth for model routing.
- File ABSENT (non-demo) -> ``active_catalog()`` returns ``None``; there is no
  chat model, the agent registry boots empty with a warning, and chat errors with
  guidance on use (UDR-0094 D1). The optional embeddings / image offering lanes
  (CTR-0075 / CTR-0049) simply stay on their retained env configuration.

DEMO_MODE is orthogonal (UDR-0094 D3): ``active_catalog()`` returns ``None``
when ``DEMO_MODE=true`` so the demo short-circuit in the agent registry
(UDR-0041 / UDR-0045 D7) routes through ``DEMO_MODELS`` and the catalog is never
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

# Default per-model context window used when an offering does not declare its own
# ``context_window`` (PRP-0113, UDR-0094 D5). This replaces the removed
# ``MODEL_MAX_CONTEXT_TOKENS`` env config and matches the prior
# ``settings.get_max_context_tokens`` fallback so an offering without an explicit
# window keeps the same 128000-token limit it had on the retired legacy lane.
DEFAULT_CONTEXT_WINDOW = 128000

# Default Azure API versions for the non-chat offerings when the offering omits
# ``api_version`` (PRP-0114, UDR-0095 D5). These replace the removed
# ``IMAGE_API_VERSION`` Settings field and the hardcoded RAG embedder literal,
# mirroring the ``DEFAULT_CONTEXT_WINDOW`` pattern above so a minimal offering
# keeps the same verified-working versions it had on the retired env lane.
DEFAULT_IMAGE_API_VERSION = "2025-04-01-preview"
DEFAULT_EMBEDDING_API_VERSION = "2024-10-21"

# Allowed values for the image offering's optional ``image_defaults`` block
# (PRP-0114, UDR-0095 D3). These mirror the enums of the removed IMAGE_* Settings
# fields; ``compression`` is a 0-100 integer (jpeg/webp only) and is validated
# separately. Values are the operator DEFAULTS -- the per-session
# ``state.image_options`` and an explicit LLM tool argument still override them.
_IMAGE_DEFAULT_ENUMS: dict[str, frozenset[str]] = {
    "size": frozenset({"auto", "1024x1024", "1024x1536", "1536x1024"}),
    "quality": frozenset({"auto", "low", "medium", "high"}),
    "format": frozenset({"png", "jpeg", "webp"}),
    "background": frozenset({"auto", "transparent", "opaque"}),
}
_IMAGE_DEFAULT_KEYS = frozenset({*_IMAGE_DEFAULT_ENUMS, "compression"})

# Keys that would embed a raw secret in the catalog file; rejected at load so
# an operator is steered to api_key_env / auth_profiles (UDR-0087 D4).
_INLINE_SECRET_KEYS = ("api_key", "apiKey", "apikey")

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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
    # Optional image output-behavior defaults (PRP-0114, UDR-0095 D3). Valid ONLY
    # on an image offering; None on every other offering. Keys are a subset of
    # {size, quality, format, background, compression}.
    image_defaults: dict[str, Any] | None = None
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
        """Chat offerings in the AUTHORED FILE ORDER (UDR-0093 D1).

        The array order is the SSOT for presentation order: it is what the Model
        Settings screen (CTR-0176) lets the operator drag, and it is what the chat
        model selector (CTR-0071) renders.

        Until PRP-0112 this method hoisted the ``default`` offering to index 0
        (UDR-0087 D3, now superseded). That made position 1 permanently owned by
        the ``default`` flag, so an operator whose most-used model was not their
        default could never place it first -- and a drag-to-reorder feature would
        have looked broken. ``default`` now means ONLY "which model is preselected"
        and MUST NOT influence position.

        NOTE for anyone touching this: ``AgentRegistry`` used to derive its default
        as ``configured[0]``, which was correct only BECAUSE of the hoist. It now
        asks ``default_offering()`` explicitly (UDR-0093 D2). Re-introducing a hoist
        here, or reverting the registry to positional derivation, silently changes
        which model answers.
        """
        return [o for o in self._offerings if o.is_chat]

    def default_offering(self) -> Offering | None:
        """The explicit ``default: true`` chat offering, else the first chat one.

        The fallback (first chat offering in file order) is UDR-0087's original
        decision and is NOT superseded: it answers WHICH model is default, not
        where it is displayed.
        """
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
                f"offering '{offering_id}': {field_name} references undefined environment variable ${{{name}}}"
            )
        return resolved

    return _VAR_RE.sub(repl, value)


def _require_str(entry: dict[str, Any], key: str, offering_id: str) -> str:
    val = entry.get(key)
    if not isinstance(val, str) or not val.strip():
        raise CatalogError(f"offering '{offering_id}': '{key}' is required and must be a non-empty string")
    return val.strip()


def _parse_image_defaults(raw: Any, offering_id: str, operations: list[str]) -> dict[str, Any] | None:
    """Validate an offering's optional ``image_defaults`` block (PRP-0114, UDR-0095 D3).

    Returns a normalized dict (only the provided keys), or None when absent.
    Raises :class:`CatalogError` when the block is present on a non-image offering,
    contains an unknown key, or carries an out-of-enum / out-of-range value.
    """
    if raw is None:
        return None
    if "image" not in operations:
        raise CatalogError(
            f"offering '{offering_id}': 'image_defaults' is only valid on an image offering "
            "(add \"image\" to 'operations')"
        )
    if not isinstance(raw, dict):
        raise CatalogError(f"offering '{offering_id}': 'image_defaults' must be an object")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _IMAGE_DEFAULT_KEYS:
            raise CatalogError(
                f"offering '{offering_id}': unknown image_defaults key {key!r} "
                f"(expected one of {sorted(_IMAGE_DEFAULT_KEYS)})"
            )
        if value is None:
            continue
        if key == "compression":
            if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 100):
                raise CatalogError(f"offering '{offering_id}': image_defaults.compression must be an integer 0-100")
            out[key] = value
            continue
        allowed = _IMAGE_DEFAULT_ENUMS[key]
        if not isinstance(value, str) or value not in allowed:
            raise CatalogError(
                f"offering '{offering_id}': image_defaults.{key} must be one of {sorted(allowed)}, got {value!r}"
            )
        out[key] = value
    return out or None


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
            f"offering '{offering_id}': unknown provider {provider!r} (expected one of {sorted(VALID_PROVIDERS)})"
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
        raise CatalogError(f"offering '{offering_id}': family must be one of {sorted(VALID_FAMILIES)}, got {family!r}")

    context_window = entry.get("context_window")
    if context_window is not None and (
        not isinstance(context_window, int) or isinstance(context_window, bool) or context_window <= 0
    ):
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

    image_defaults = _parse_image_defaults(entry.get("image_defaults"), offering_id, operations)

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
        image_defaults=image_defaults,
        metadata=entry.get("metadata", {}) if isinstance(entry.get("metadata"), dict) else {},
    )


def _parse_auth_profiles(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("auth_profiles", {})
    if not isinstance(raw, dict):
        raise CatalogError("'auth_profiles' must be an object")
    profiles: dict[str, str] = {}
    for name, spec in raw.items():
        if (
            not isinstance(spec, dict)
            or not isinstance(spec.get("api_key_env"), str)
            or not spec["api_key_env"].strip()
        ):
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
        raise CatalogError(f"at most one chat offering may set default: true (got {[o.id for o in defaults]})")

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
    """Return the active :class:`Catalog`, or None when no catalog is present.

    Returns None when DEMO_MODE is on (the catalog is ignored for a demo
    deployment, UDR-0087 D10) or when no catalog file is present. A non-demo
    deployment with no catalog file has NO chat model: the agent registry boots
    empty with a warning and chat errors with guidance on use (PRP-0113,
    UDR-0094 D1); this function does not raise so read-only callers stay safe.
    Resolved once and cached.
    """
    global _active
    if _active is not _UNSET:
        return _active

    if settings.demo_mode:
        # DEMO_MODE is orthogonal: routing comes from DEMO_MODELS and the catalog
        # is never consulted (UDR-0087 D10 / UDR-0094 D3).
        logger.info("Model routing source: DEMO_MODE (in-process demo models; catalog ignored)")
        _active = None
        return None

    path = _catalog_path()
    if path is None or not path.is_file():
        # No catalog present. Non-demo: there is no chat model; the agent registry
        # boots empty with a warning and chat errors with guidance on use
        # (UDR-0094 D1). The optional embeddings / image offering lanes stay on
        # their retained env configuration.
        configured = (settings.model_offerings_file or "").strip()
        detail = f"MODEL_OFFERINGS_FILE={configured} not found" if configured else "MODEL_OFFERINGS_FILE unset"
        logger.warning(
            "No Model Offering Catalog present (%s); no chat model is configured. "
            "Author one with `chatwalaau models add` or the Model Settings screen.",
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
    _active = catalog
    return catalog


def reset_active() -> None:
    """Discard the cached active catalog so the next resolve re-reads the file.

    The public reload hook (PRP-0111 / UDR-0090 D2): the management API (CTR-0175)
    and any hot-reload path call this BEFORE rebuilding the AgentRegistry, so a
    freshly written ``model_offerings.jsonc`` takes effect without a restart.
    """
    global _active
    _active = _UNSET


def reset_for_tests() -> None:
    """Discard the cached active catalog so the next call re-resolves it."""
    reset_active()


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
    """Resolved config for the single embeddings offering, or None.

    Consumed by the RAG embedder (CTR-0075) as the SOLE source of the embedding
    model identity for a non-demo deployment (PRP-0114, UDR-0095 D1). None means no
    catalog / no embeddings offering: the consumer then degrades gracefully (RAG
    unavailable) except under DEMO_MODE, where DemoEmbedder is used regardless
    (UDR-0095 D4). ``api_version`` defaults to ``DEFAULT_EMBEDDING_API_VERSION`` and
    ``endpoint`` to the shared ``AZURE_OPENAI_ENDPOINT`` when the offering omits them.
    """
    catalog = active_catalog()
    if catalog is None:
        return None
    return _resolved(catalog.embeddings_offering())


def image_config() -> ResolvedModelConfig | None:
    """Resolved config for the single image offering, or None.

    Consumed by image generation (CTR-0049) as the SOLE source of the image model
    identity for a non-demo deployment (PRP-0114, UDR-0095 D1). None means no
    catalog / no image offering: the tools are then not registered (graceful) and
    the mask-edit endpoint (CTR-0053) refuses -- except under DEMO_MODE, which uses
    the demo image tools regardless (UDR-0095 D4). ``api_version`` defaults to
    ``DEFAULT_IMAGE_API_VERSION`` and ``endpoint`` to the shared
    ``AZURE_OPENAI_ENDPOINT`` when the offering omits them. Image generation stays on
    the dedicated Azure Images API (UDR-0045 D7 preserved).
    """
    catalog = active_catalog()
    if catalog is None:
        return None
    return _resolved(catalog.image_offering())


def image_output_defaults() -> dict[str, Any]:
    """The image offering's ``image_defaults`` block, or ``{}`` (PRP-0114, UDR-0095 D3).

    Consumed by image generation (CTR-0049) as the operator DEFAULT tier for the
    output-behavior options (size / quality / format / compression / background),
    replacing the removed CTR-0006 ``IMAGE_*`` settings. Returns an empty dict when
    there is no catalog, no image offering, or the offering declares no defaults;
    the per-session ``state.image_options`` and an explicit LLM tool argument still
    override these (precedence in app.image_gen.tools._resolve_option).
    """
    catalog = active_catalog()
    if catalog is None:
        return {}
    offering = catalog.image_offering()
    if offering is None or not offering.image_defaults:
        return {}
    return dict(offering.image_defaults)


# ---- Write side + management snapshot (CTR-0174 write path, PRP-0111 / UDR-0090)


# Canonical key order for a serialized offering (readability; UDR-0090 D3).
_OFFERING_KEY_ORDER = (
    "id",
    "provider",
    "model_ref",
    "operations",
    "hosting",
    "family",
    "endpoint",
    "base_url",
    "api_version",
    "context_window",
    "default",
    "api_key_env",
    "auth_profile",
    "image_defaults",
    "metadata",
)


def catalog_path() -> Path | None:
    """Public accessor for the resolved catalog path (or None when unset).

    Used by the management API (CTR-0175) to snapshot the file for rollback on a
    failed hot reload (UDR-0090 D2). None means ``MODEL_OFFERINGS_FILE`` is empty.
    """
    return _catalog_path()


def read_raw_catalog(path: Path | None = None) -> dict[str, Any] | None:
    """Return the on-disk catalog as a RAW dict (no ``${VAR}`` interpolation), or None.

    Unlike :func:`active_catalog` / :func:`load_catalog`, this does NOT interpolate
    ``${VAR}`` placeholders or flatten ``auth_profile`` references -- it returns the
    operator-authored structure verbatim so the management API (CTR-0175) and the
    CLI (CTR-0082) can edit and re-emit it (UDR-0090 D3). Returns None when no file
    is present (legacy lane). Raises :class:`CatalogError` on unreadable / non-JSON
    content so callers can surface a clear message.
    """
    if path is None:
        path = _catalog_path()
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"failed to read model offerings catalog {path}: {exc}") from exc
    try:
        data = json.loads(_strip_jsonc_comments(raw))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"model offerings catalog {path} is not valid JSON/JSONC: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError("model offerings catalog must be a JSON object")
    return data


def referenced_env_names(data: dict[str, Any]) -> list[str]:
    """Collect every environment-variable NAME a RAW catalog references (ordered, unique).

    Includes ``api_key_env`` on each offering, the ``api_key_env`` of each
    ``auth_profiles`` entry, and any ``${VAR}`` inside ``endpoint`` / ``base_url``.
    NAMES only -- values are never read here (UDR-0090 D4).
    """
    names: list[str] = []
    seen: set[str] = set()

    def add(name: Any) -> None:
        if isinstance(name, str) and name.strip() and name not in seen:
            seen.add(name)
            names.append(name)

    profiles = data.get("auth_profiles")
    if isinstance(profiles, dict):
        for spec in profiles.values():
            if isinstance(spec, dict):
                add(spec.get("api_key_env"))

    offerings = data.get("offerings")
    if isinstance(offerings, list):
        for entry in offerings:
            if not isinstance(entry, dict):
                continue
            add(entry.get("api_key_env"))
            for fld in ("endpoint", "base_url"):
                val = entry.get(fld)
                if isinstance(val, str):
                    for match in _VAR_RE.finditer(val):
                        add(match.group(1))
    return names


def detect_env(names: list[str]) -> dict[str, bool]:
    """Map each env-var NAME to whether it is set (non-empty) in the environment.

    Booleans ONLY -- secret values are never returned (UDR-0090 D4). Drives the
    GUI "detected / not-set" indicator (CTR-0176).
    """
    return {name: bool((os.environ.get(name) or "").strip()) for name in names}


def _normalize_offering(entry: dict[str, Any]) -> dict[str, Any]:
    """Return an offering dict in canonical key order, dropping None / empty values."""
    out: dict[str, Any] = {}
    for key in _OFFERING_KEY_ORDER:
        if key not in entry:
            continue
        val = entry[key]
        if val is None:
            continue
        if key in ("metadata", "image_defaults") and isinstance(val, dict) and not val:
            continue
        out[key] = val
    # Preserve any unknown-but-present keys (forward-compat) after the known ones.
    for key, val in entry.items():
        if key not in out and val is not None:
            out[key] = val
    return out


def serialize_catalog(data: dict[str, Any]) -> str:
    """Serialize a RAW catalog dict to formatted JSON (JSONC-valid; UDR-0090 D3).

    Comments and hand-authored formatting are NOT preserved: the writer emits
    clean, deterministic JSON with a stable key order. The result is valid JSONC
    (a strict-JSON subset) and round-trips through :func:`read_raw_catalog`.
    """
    out: dict[str, Any] = {}
    profiles = data.get("auth_profiles")
    if isinstance(profiles, dict) and profiles:
        out["auth_profiles"] = profiles
    offerings = data.get("offerings")
    out["offerings"] = (
        [_normalize_offering(o) for o in offerings if isinstance(o, dict)] if isinstance(offerings, list) else []
    )
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


def write_catalog(data: dict[str, Any], path: Path | None = None) -> Path:
    """Validate + atomically write a RAW catalog dict (UDR-0090 D2 / D3 / D8).

    Validation parity with the load path: :func:`parse_catalog` enforces every
    CTR-0174 invariant (raising :class:`CatalogError`) BEFORE any bytes are
    written. The write is atomic (temp file + ``os.replace``) so a crash mid-write
    cannot leave a truncated ``model_offerings.jsonc``. This does NOT reset the
    active cache or rebuild agents -- callers do that (the API rebuilds; the
    offline CLI does not). Returns the path written.
    """
    if path is None:
        path = _catalog_path()
    if path is None:
        raise CatalogError("MODEL_OFFERINGS_FILE is unset; set it to a path to enable catalog management")
    parse_catalog(data)  # validate (raises CatalogError) -- validation parity (D8)
    serialized = serialize_catalog(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(serialized, encoding="utf-8")
    tmp.replace(path)
    return path


def catalog_status() -> dict[str, Any]:
    """Non-raising snapshot for the management API (CTR-0175 GET).

    Returns the RAW on-disk catalog (or an empty shell), whether the catalog lane
    is in effect, DEMO_MODE, a best-effort validity check, the resolved path, and
    ``env_status`` booleans for every referenced variable NAME. Never raises and
    never returns secret values (UDR-0090 D4).
    """
    demo = bool(settings.demo_mode)
    path = _catalog_path()
    present = path is not None and path.is_file()
    data: dict[str, Any] = {"offerings": [], "auth_profiles": {}}
    valid = True
    errors: list[str] = []
    if present:
        try:
            raw = read_raw_catalog(path)
            if raw is not None:
                data = raw
        except CatalogError as exc:
            valid = False
            errors.append(str(exc))
        else:
            try:
                parse_catalog(data)
            except CatalogError as exc:
                valid = False
                errors.append(str(exc))
    names = referenced_env_names(data) if present else []
    return {
        "active": bool(present and not demo),
        "demo_mode": demo,
        "present": bool(present),
        "path": str(path) if path is not None else None,
        "valid": valid,
        "errors": errors,
        "auth_profiles": data.get("auth_profiles", {}) if isinstance(data.get("auth_profiles"), dict) else {},
        "offerings": data.get("offerings", []) if isinstance(data.get("offerings"), list) else [],
        "env_status": detect_env(names),
    }
