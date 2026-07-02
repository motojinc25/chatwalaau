"""Webhook source registry (CTR-0151, PRP-0097, UDR-0075 D2).

The single extension point for webhook SOURCES. A source is one registration of an
ingress handler (the part that runs at the public ``/api/webhook/{source}`` boundary,
CTR-0149) plus display metadata. The first registered source is Microsoft Graph
(``msgraph``); a later source (GitHub, Stripe, ...) is a new registration with no
ingress / store / API / UI change.

Per-source ENABLE/DISABLE state and the RECEIPT records live in the store (CTR-0151);
this module owns only the in-memory source table and the ingress dispatch type.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IngressContext:
    """The normalized inbound request handed to a source's ingress handler."""

    source: str
    method: str
    headers: dict[str, str]
    query: dict[str, str]
    raw_body: bytes
    client_ip: str


@dataclass(frozen=True)
class IngressResponse:
    """The response a source's ingress handler returns to the public boundary."""

    status_code: int = 202
    media_type: str = "application/json"
    body: str = ""


# An ingress handler validates and (for an accepted notification) hands the payload to
# a runtime, then returns the response the public boundary should send (CTR-0149).
IngressHandler = Callable[[IngressContext], Awaitable[IngressResponse]]


@dataclass(frozen=True)
class WebhookSource:
    """A registered webhook source (CTR-0151)."""

    name: str
    label: str
    description: str
    handle: IngressHandler
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            **self.metadata,
        }


_SOURCES: dict[str, WebhookSource] = {}


def register_source(source: WebhookSource, *, replace: bool = False) -> None:
    """Register a webhook source. Idempotent unless ``replace`` is set."""
    if source.name in _SOURCES and not replace:
        return
    _SOURCES[source.name] = source


def get_source(name: str) -> WebhookSource | None:
    """Return a registered source by name, or None."""
    return _SOURCES.get(name)


def list_sources() -> list[WebhookSource]:
    """Return all registered sources (registration order)."""
    return list(_SOURCES.values())


def clear_sources() -> None:
    """Drop all registered sources (test/reload helper)."""
    _SOURCES.clear()


__all__ = [
    "IngressContext",
    "IngressHandler",
    "IngressResponse",
    "WebhookSource",
    "clear_sources",
    "get_source",
    "list_sources",
    "register_source",
]
