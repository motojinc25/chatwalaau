"""Application Settings store (CTR-0198, PRP-0136, UDR-0120).

``app_settings.jsonc`` holds the operator-tunable runtime knobs that used to live
in ``.env``. This module owns reading it, coercing its values, applying them onto
the ``Settings`` singleton, and writing it back without losing anything it does
not understand.

Four rules from UDR-0120 shape the code here, and each has a failure mode that
motivated it:

D2 (sole source) -- a migrated key's value comes from this file or from the
``Settings`` field default, NEVER from ``.env``. :func:`residual_env_keys`
detects a leftover variable so startup can name it; nothing consults its value.
The rejected alternative, env-as-fallback, has the failure mode "I changed it in
the GUI and nothing happened", which is invisible from the screen the operator is
looking at.

D4 (defaults derived) -- values are coerced against
``Settings.model_fields[key].annotation`` BEFORE assignment. The originally
proposed ``validate_assignment=True`` is not implementable on this model: six
``mode="after"`` validators normalise by assigning to ``self``, and with that flag
on, each assignment re-enters the validator that made it, so ``Settings()``
recurses infinitely at construction. Coercing here gives the same guarantee and
lands the check where D7's degrade branch has to live anyway.

D5 (unknown keys preserved) -- an unknown key does NOT fail the load and is NOT
dropped. It is carried in :attr:`StoreDocument.unknown` and merged back by
:func:`serialize_store`, because the write surface is a full-document PUT and a
serializer that forgot the bag would delete every preserved key on the first GUI
save -- the defect UDR-0093 D4 already found in ``auth_profiles``. This is
deliberately asymmetric with UDR-0112 D7 (unknown offering capability -> 400):
a configuration file outlives the binary that validates it, so a store written by
a newer release must not stop an older one from BOOTING after a rollback.

D7 (degrade, never fail-fast) -- a known key holding an uncoercible or
out-of-enum value falls back to the derived default with a warning, mirroring how
UDR-0096 D5 treats a ``roles`` binding that points at a deleted offering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.app_settings.descriptors import (
    DESCRIPTORS,
    RENAMED_FROM,
    SCOPE_REBUILD,
    SCOPE_RESTART,
    annotation_for,
    default_for,
    descriptor,
    known_keys,
)
from app.core.config import settings
from app.mcp.config import _strip_jsonc_comments

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# The top-level block that carries the settings map. Nesting one level (rather
# than putting keys at the document root) keeps a future sibling block -- a
# migration record, an audit stamp -- from muddying the unknown-key
# determination, the same reason `roles` sits beside `offerings` in the catalog.
SETTINGS_BLOCK = "settings"


class SettingsStoreError(Exception):
    """Raised when the store file cannot be read or is not a JSON object."""


# Values that an existing operator configuration may legitimately carry but that
# are not offered as GUI choices. These are NORMALISED, not degraded: they name a
# real behaviour, so degrading them to the default would silently CHANGE what the
# system does, which is not what D7's degrade rule is for (that rule exists for
# values that no longer mean anything).
_VALUE_ALIASES: dict[str, dict[str, str]] = {
    # app.agent.compaction treats these as "compaction disabled".
    "compaction_strategy": {"": "none", "off": "none", "disabled": "none"},
}


@dataclass
class StoreDocument:
    """A parsed store: coerced known values, preserved unknown keys, warnings."""

    values: dict[str, Any] = field(default_factory=dict)
    unknown: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    path: Path | None = None
    present: bool = False
    extra_blocks: dict[str, Any] = field(default_factory=dict)


def store_path() -> Path | None:
    """Resolve the configured store path, or None when APP_SETTINGS_FILE is empty.

    A relative path resolves against the working directory (mirroring the catalog
    / MCP / commands resolvers). A configured-but-missing file is NOT an error --
    it simply means every key keeps its derived default.
    """
    raw = (settings.app_settings_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def read_raw_store(path: Path | None = None) -> dict[str, Any] | None:
    """Return the on-disk document verbatim, or None when no file is present."""
    if path is None:
        path = store_path()
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsStoreError(f"failed to read application settings {path}: {exc}") from exc
    try:
        data = json.loads(_strip_jsonc_comments(raw))
    except json.JSONDecodeError as exc:
        raise SettingsStoreError(f"application settings {path} is not valid JSON/JSONC: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsStoreError("application settings must be a JSON object")
    return data


def coerce_value(key: str, raw: Any) -> tuple[Any, str | None]:
    """Coerce one operator value to its declared type, degrading on failure (D4/D7).

    Returns ``(value, warning)``. ``warning`` is None on success; otherwise the
    value is the derived default and the warning explains the fallback. This
    never raises, because a bad value in a configuration file must not stop the
    process (D5's downgrade-safety argument applies to values as well as keys).
    """
    desc = descriptor(key)
    if desc is None:
        return raw, None

    candidate = raw
    aliases = _VALUE_ALIASES.get(key)
    if aliases and isinstance(candidate, str):
        candidate = aliases.get(candidate.strip().lower(), candidate)

    try:
        value = TypeAdapter(annotation_for(key)).validate_python(candidate)
    except ValidationError:
        fallback = default_for(key)
        return fallback, (f"{desc.env_name}: {raw!r} is not a valid {desc.type}; using the default {fallback!r}")

    if desc.enum is not None and str(value) not in desc.enum:
        fallback = default_for(key)
        return fallback, (f"{desc.env_name}: {raw!r} is not one of {list(desc.enum)}; using the default {fallback!r}")

    return value, None


def parse_store(data: dict[str, Any] | None) -> StoreDocument:
    """Split a raw document into coerced known values, unknown keys, and warnings.

    Never raises on content: an unknown key is preserved (D5) and a bad value
    degrades (D7). Only unreadable / non-JSON input fails, and that happens in
    :func:`read_raw_store` before this is reached.
    """
    doc = StoreDocument()
    if data is None:
        return doc

    doc.present = True
    doc.extra_blocks = {k: v for k, v in data.items() if k not in {SETTINGS_BLOCK, "schema_version"}}

    block = data.get(SETTINGS_BLOCK)
    if block is None:
        return doc
    if not isinstance(block, dict):
        doc.warnings.append(f"'{SETTINGS_BLOCK}' must be an object; ignoring it")
        return doc

    valid = known_keys()
    for raw_key, raw_value in block.items():
        # A renamed predecessor is absorbed ONCE under its successor (D7). The
        # NEW name wins if both are present, so an absorbed value never
        # overwrites a value the operator set deliberately.
        key = raw_key
        if raw_key not in valid and raw_key in RENAMED_FROM:
            successor = RENAMED_FROM[raw_key]
            if successor in block:
                doc.warnings.append(f"{raw_key} was renamed to {successor} and both are set; keeping {successor}")
                continue
            doc.warnings.append(f"{raw_key} was renamed to {successor}; migrating the value")
            key = successor

        if key not in valid:
            doc.unknown[raw_key] = raw_value
            continue

        value, warning = coerce_value(key, raw_value)
        if warning:
            doc.warnings.append(warning)
        doc.values[key] = value

    return doc


def load_store(path: Path | None = None) -> StoreDocument:
    """Read and parse the store. Returns an empty document when no file exists."""
    if path is None:
        path = store_path()
    doc = parse_store(read_raw_store(path))
    doc.path = path
    return doc


# ---- Applying onto the Settings singleton (D3) ----------------------------


def apply_values(values: dict[str, Any]) -> set[str]:
    """Assign coerced values onto the singleton; return the scopes that were touched.

    Values are assumed ALREADY coerced (:func:`coerce_value` / :func:`parse_store`),
    so the plain assignment is type-correct. A returned scope tells the caller what
    still has to happen: ``rebuild`` needs the CTR-0070 agent rebuild, ``restart``
    needs the operator (UDR-0120 D6 -- the app never restarts itself).

    A key whose value already equals the live one contributes NO scope, so saving
    an unchanged form does not claim a restart is required.
    """
    touched: set[str] = set()
    for key, value in values.items():
        desc = descriptor(key)
        if desc is None:
            continue
        if getattr(settings, key, None) == value:
            continue
        setattr(settings, key, value)
        touched.add(desc.scope)
    return touched


def apply_defaults_for_absent(values: dict[str, Any]) -> set[str]:
    """Reset owned keys that the document does NOT set back to their defaults.

    Absent means unset means the derived default (D7), so a key removed from the
    file must not keep the value a previous apply left on the singleton -- that
    would make deletion silently inert.
    """
    missing = {d.key: default_for(d.key) for d in DESCRIPTORS if d.key not in values}
    return apply_values(missing)


def apply_document(doc: StoreDocument) -> set[str]:
    """Apply a parsed document in full: set what it carries, default what it omits."""
    touched = apply_values(doc.values)
    return touched | apply_defaults_for_absent(doc.values)


def needs_rebuild(scopes: set[str]) -> bool:
    """Whether an applied change requires the CTR-0070 agent rebuild."""
    return SCOPE_REBUILD in scopes


def needs_restart(scopes: set[str]) -> bool:
    """Whether an applied change requires an operator-driven process restart."""
    return SCOPE_RESTART in scopes


# ---- Residual .env detection (D2) ----------------------------------------


def _env_file_path() -> Path:
    """The .env the process was configured from (pydantic-settings default)."""
    return Path.cwd() / ".env"


def residual_env_keys() -> list[str]:
    """Return migrated env-var NAMES still set in .env or the process environment.

    Their VALUES are never read -- the store is the sole source (D2). This exists
    only so startup can NAME them, which the operator needs in order to transcribe
    them into the GUI. A count would not be actionable.
    """
    owned = {d.env_name: d.key for d in DESCRIPTORS}
    found: list[str] = [name for name in owned if os.environ.get(name) is not None]

    env_path = _env_file_path()
    if env_path.is_file():
        try:
            from app.core.env_template import parse_active_assignments

            active, _ = parse_active_assignments(env_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            active = {}
        for name in active:
            if name in owned and name not in found:
                found.append(name)

    return sorted(found)


def residual_env_values() -> dict[str, str]:
    """Raw values of residual migrated keys, for the ``settings migrate`` CLI only.

    The runtime NEVER calls this: reading these values at runtime would be exactly
    the env fallback D2 forbids. The CLI reads them once so an operator can carry a
    pre-PRP-0136 configuration into the store in one step instead of by hand.
    """
    owned = {d.env_name: d.key for d in DESCRIPTORS}
    out: dict[str, str] = {}

    env_path = _env_file_path()
    if env_path.is_file():
        try:
            from app.core.env_template import parse_active_assignments

            active, _ = parse_active_assignments(env_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            active = {}
        for name, raw in active.items():
            if name in owned:
                out[owned[name]] = raw

    # A real process env var wins over the file, matching how the process was
    # actually configured before the migration.
    for name, key in owned.items():
        value = os.environ.get(name)
        if value is not None:
            out[key] = value

    return out


# ---- Serialization (D5: unknown keys must survive the write) --------------


def serialize_store(doc_values: dict[str, Any], unknown: dict[str, Any] | None = None) -> str:
    """Render a store document as canonical JSON.

    Known keys are emitted in DESCRIPTOR order (so a hand-diff stays stable across
    saves), then the preserved unknown keys in their original order. Dropping the
    unknown bag here is the one mistake that silently destroys operator data on a
    full-document PUT, so it is merged unconditionally -- there is no flag to skip
    it.
    """
    body: dict[str, Any] = {}
    for desc in DESCRIPTORS:
        if desc.key in doc_values:
            body[desc.key] = doc_values[desc.key]
    for key, value in (unknown or {}).items():
        if key not in body:
            body[key] = value

    document = {"schema_version": SCHEMA_VERSION, SETTINGS_BLOCK: body}
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write_store(
    doc_values: dict[str, Any],
    unknown: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path:
    """Atomically write the store (temp file + replace), returning the path written."""
    if path is None:
        path = store_path()
    if path is None:
        raise SettingsStoreError(
            "APP_SETTINGS_FILE is unset; set it to a path to enable application settings management"
        )
    serialized = serialize_store(doc_values, unknown)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(serialized, encoding="utf-8")
    tmp.replace(path)
    return path


# ---- Status snapshot for the management API (CTR-0199 GET) ----------------


def store_status() -> dict[str, Any]:
    """Non-raising snapshot: live values, descriptors, groups, unknown keys, warnings.

    Never raises -- an unreadable file is reported as ``valid: false`` with the
    message, so the App Settings screen can render an actionable empty state
    instead of a blank page.
    """
    from app.app_settings.descriptors import descriptor_registry, group_registry

    path = store_path()
    valid = True
    error: str | None = None
    doc = StoreDocument()
    try:
        doc = load_store(path)
    except SettingsStoreError as exc:
        valid = False
        error = str(exc)

    # Report the LIVE singleton values, not the file's -- they are what the system
    # is actually using, which is the question an operator opening the screen is
    # asking. They agree with the file except where a value degraded (D7).
    values = {d.key: getattr(settings, d.key, default_for(d.key)) for d in DESCRIPTORS}

    return {
        "schema_version": SCHEMA_VERSION,
        "settings": values,
        "descriptors": descriptor_registry(),
        "groups": group_registry(),
        "unknown": doc.unknown,
        "warnings": doc.warnings,
        "residual_env_keys": residual_env_keys(),
        "path": str(path) if path else None,
        "present": doc.present,
        "valid": valid,
        "error": error,
        "demo_mode": settings.demo_mode,
    }


__all__ = [
    "SCHEMA_VERSION",
    "SETTINGS_BLOCK",
    "SettingsStoreError",
    "StoreDocument",
    "apply_defaults_for_absent",
    "apply_document",
    "apply_values",
    "coerce_value",
    "load_store",
    "needs_rebuild",
    "needs_restart",
    "parse_store",
    "read_raw_store",
    "residual_env_keys",
    "residual_env_values",
    "serialize_store",
    "store_path",
    "store_status",
    "write_store",
]
