"""Webhook Ingress Endpoint (CTR-0149, PRP-0097, UDR-0075 D3/D10).

The PUBLIC inbound boundary: ``GET/POST {WEBHOOK_INGRESS_BASE_PATH}/{source}`` (default
``/api/webhook/{source}``, e.g. ``/api/webhook/msgraph``). It looks up the registered
source (CTR-0151) and delegates to its ingress handler, which answers the Graph
validation handshake or ACKs a notification batch with 202 (UDR-0075 D3).

This is the ONLY webhook surface NOT gated by CTR-0083 (UDR-0075 D10): authentication
is NOT applicable (Graph is not an API_KEY holder). Boundary protection is the source's
own validation (validation handshake + clientState HMAC + optional allowlists, CTR-0150).
Because this router is owned by CAP-010 (not CAP-002) it is EXEMPT from the
CAP-002-scoped CTR-0083 mutating-endpoint invariant, the same exemption CAP-008/CAP-009
hold (system-model invariant note). The whole router 404s when WEBHOOK_ENABLED is false.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.config import settings
from app.webhook import store
from app.webhook.registry import IngressContext, get_source

logger = logging.getLogger(__name__)

_base = (settings.webhook_ingress_base_path or "/api/webhook").rstrip("/")
router = APIRouter(prefix=_base, tags=["Webhook Ingress"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


async def _dispatch(source: str, request: Request) -> Response:
    if not settings.webhook_enabled:
        raise HTTPException(status_code=404, detail={"error": "webhook_disabled"})
    src = get_source(source)
    if src is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_source"})
    if not store.is_source_enabled(source):
        # A disabled source still answers the validation handshake (so an operator can
        # validate before enabling) but drops notifications.
        token = request.query_params.get("validationToken") or request.query_params.get("validationtoken")
        if token is None:
            raise HTTPException(status_code=404, detail={"error": "source_disabled"})
    raw = await request.body()
    ctx = IngressContext(
        source=source,
        method=request.method,
        headers={k.lower(): v for k, v in request.headers.items()},
        query=dict(request.query_params),
        raw_body=raw,
        client_ip=_client_ip(request),
    )
    result = await src.handle(ctx)
    return Response(content=result.body, status_code=result.status_code, media_type=result.media_type)


@router.get("/{source}")
async def ingress_get(source: str, request: Request) -> Response:
    """Validation handshake (Graph sends ?validationToken=... ; echoed by the source)."""
    return await _dispatch(source, request)


@router.post("/{source}")
async def ingress_post(source: str, request: Request) -> Response:
    """Inbound notification batch; validated + ACKed (202) by the source handler."""
    return await _dispatch(source, request)


__all__ = ["router"]
