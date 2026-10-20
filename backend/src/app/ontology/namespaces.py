"""Ontology IRI namespaces under the operator's domain (PRP-0218, UDR-0200, CTR-0170 v13).

Stage 1 of the IRI plan: NAMING only (nothing here serves or redirects an IRI).

An ontology's namespace is decided ONCE -- when it is created, or set once later on an
entry that has none -- and STORED in its catalog entry (D2), never recomputed from the
current ``ontology_iri_base`` App Setting. From the stored ``ontology_iri`` (D3)::

    ontology      {ontology_iri}           https://kg.acme.co.jp/plant
    vocabulary    {ontology_iri}#          https://kg.acme.co.jp/plant#Pump
    individuals   {ontology_iri}/id/       https://kg.acme.co.jp/plant/id/P-101

An entry WITHOUT a stored namespace (every ontology created before v0.194.0, and new
ones under the default base) keeps the original form exactly: ontology
``https://chatwalaau.com/ontology/{id}``, vocabulary AND individuals ``...{id}#``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_IRI_BASE = "https://chatwalaau.com/ontology/"
MAX_IRI_LENGTH = 200
SHORT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")
# Paths stages 2 (resolving) and 3 (SPARQL Protocol) may need under the base (D5).
RESERVED_SHORT_NAMES = frozenset(
    {
        "api",
        "id",
        "sparql",
        "ontology",
        "ontologies",
        "static",
        "assets",
        "admin",
        "health",
        "docs",
        "login",
        "logout",
        "auth",
        "catalog",
        "graph",
        "graphs",
        "query",
        "search",
        "well-known",
    }
)
_FORBIDDEN = re.compile(r"[\s<>\"{}|\\^`]")


class NamespaceError(ValueError):
    """A refused namespace request; ``code`` is the API error (422 / 409)."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __reduce__(self) -> tuple[Any, ...]:
        return (NamespaceError, (self.code, self.message, self.status))

    @property
    def detail(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _split(raw: Any, what: str, code: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        raise NamespaceError(code, f"The {what} is empty.")
    if len(text) > MAX_IRI_LENGTH:
        raise NamespaceError(code, f"The {what} is longer than {MAX_IRI_LENGTH} characters.")
    if _FORBIDDEN.search(text):
        raise NamespaceError(code, f"The {what} contains a space or a character not allowed in an IRI.")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise NamespaceError(code, f"The {what} must be an absolute http:// or https:// IRI.")
    if parts.username is not None or parts.password is not None:
        raise NamespaceError(code, f"The {what} must not contain a user name or password.")
    if parts.query or parts.fragment or "#" in text or "?" in text:
        raise NamespaceError(code, f"The {what} must not contain a query (?) or a fragment (#).")
    host = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))


def normalize_iri_base(raw: Any) -> str:
    """The App Settings ``ontology_iri_base`` value, checked (D1); raises NamespaceError."""
    text = _split(raw, "IRI base", "invalid_iri_base")
    if not text.endswith("/"):
        raise NamespaceError("invalid_iri_base", "The IRI base must end with '/'.")
    if text != DEFAULT_IRI_BASE and text.startswith(DEFAULT_IRI_BASE):
        raise NamespaceError(
            "invalid_iri_base", f"IRIs under {DEFAULT_IRI_BASE} belong to ChatWalaʻau; use your own domain."
        )
    return text


def is_default_base(base: str) -> bool:
    return base == DEFAULT_IRI_BASE


def current_base(value: Any) -> str:
    """The usable IRI base from a setting value (an invalid one falls back to the default)."""
    try:
        return normalize_iri_base(value)
    except NamespaceError:
        return DEFAULT_IRI_BASE


def check_short_name(raw: Any) -> str:
    """A short name (D5): 2-40 of a-z 0-9 '-', starting with a letter, not reserved."""
    text = str(raw or "").strip()
    if not SHORT_NAME_RE.match(text):
        raise NamespaceError(
            "invalid_short_name",
            "A short name has 2 to 40 characters: lower-case letters, digits and '-', "
            "starting with a letter and not ending with '-'.",
        )
    if text in RESERVED_SHORT_NAMES:
        raise NamespaceError("reserved_short_name", f"'{text}' is reserved; choose another short name.")
    return text


def normalize_ontology_iri(raw: Any) -> str:
    """A custom ontology IRI (D5): no trailing '/' or '#', not under the product's namespace."""
    text = _split(raw, "ontology IRI", "invalid_ontology_iri")
    if text.endswith("/"):
        raise NamespaceError("invalid_ontology_iri", "The ontology IRI must not end with '/'.")
    if urlsplit(text).path in ("", "/"):
        raise NamespaceError("invalid_ontology_iri", "The ontology IRI needs a path after the host name.")
    if text.lower().startswith(DEFAULT_IRI_BASE):
        raise NamespaceError(
            "invalid_ontology_iri", f"IRIs under {DEFAULT_IRI_BASE} belong to ChatWalaʻau; use your own domain."
        )
    return text


def normalize_block(raw: Any) -> dict[str, Any] | None:
    """The catalog ``namespace`` block, tolerant (an unusable block is dropped -> legacy form)."""
    if not isinstance(raw, dict):
        return None
    try:
        iri = _split(raw.get("ontology_iri"), "ontology IRI", "invalid_ontology_iri")
    except NamespaceError:
        return None
    if iri.endswith("/"):
        return None
    short = raw.get("short_name")
    short = short if isinstance(short, str) and SHORT_NAME_RE.match(short) else None
    return {"ontology_iri": iri, "short_name": short, "custom": raw.get("custom") is True}


@dataclass(frozen=True)
class Namespace:
    """The three IRIs of one ontology (D3) and how they were decided."""

    ontology: str
    vocabulary: str
    individuals: str
    short_name: str | None
    custom: bool
    fixed: bool  # True when stored in the catalog entry (cannot change any more)

    def to_json(self) -> dict[str, Any]:
        return {
            "ontology": self.ontology,
            "vocabulary": self.vocabulary,
            "individuals": self.individuals,
            "short_name": self.short_name,
            "custom": self.custom,
            "fixed": self.fixed,
        }


def legacy_ontology_iri(ontology_id: str) -> str:
    return f"{DEFAULT_IRI_BASE}{ontology_id}"


def of(ontology_id: str, block: dict[str, Any] | None) -> Namespace:
    """The namespace of an entry from its stored block (None = the original form)."""
    if block is None:
        ontology = legacy_ontology_iri(ontology_id)
        return Namespace(ontology, f"{ontology}#", f"{ontology}#", None, False, False)
    ontology = block["ontology_iri"]
    return Namespace(
        ontology, f"{ontology}#", f"{ontology}/id/", block.get("short_name"), bool(block.get("custom")), True
    )


def decide(ontology_id: str, *, short_name: Any, ontology_iri: Any, base: str) -> dict[str, Any] | None:
    """The block a NEW ontology (or a first-time set) gets (D4); None = the original form."""
    given_short = str(short_name or "").strip()
    given_iri = str(ontology_iri or "").strip()
    if given_short and given_iri:
        raise NamespaceError("invalid_namespace", "Give a short name or an ontology IRI, not both.")
    if given_iri:
        return {"ontology_iri": normalize_ontology_iri(given_iri), "short_name": None, "custom": True}
    if is_default_base(base):
        if given_short:
            raise NamespaceError(
                "short_name_needs_base",
                "A short name needs an IRI base of your own (App Settings > Ontology > IRI base), "
                "or give a full ontology IRI.",
            )
        return None
    name = check_short_name(given_short) if given_short else None
    return {"ontology_iri": f"{base}{name or ontology_id}", "short_name": name, "custom": False}


def clashes(a: str, b: str) -> bool:
    """Two ontology IRIs clash when equal or when one is inside the other's namespace."""
    return a == b or a.startswith((f"{b}/", f"{b}#")) or b.startswith((f"{a}/", f"{a}#"))


__all__ = [
    "DEFAULT_IRI_BASE",
    "RESERVED_SHORT_NAMES",
    "SHORT_NAME_RE",
    "Namespace",
    "NamespaceError",
    "check_short_name",
    "clashes",
    "current_base",
    "decide",
    "is_default_base",
    "legacy_ontology_iri",
    "normalize_block",
    "normalize_iri_base",
    "normalize_ontology_iri",
    "of",
]
