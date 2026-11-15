"""API Key Management API (CTR-0283, PRP-0253, UDR-0234).

Every route requires ``verify_key_admin``: a signed-in session, a loopback peer or a
Bearer ``admin`` key (D7). A secret is returned only by issue and reissue (D4); no route
ever returns a hash. A caller cannot grant a scope above its own (D2). Responses carry
``Cache-Control: no-store``.

PRP-0254 (UDR-0235 D5): a key may carry per-key ``/v1/responses`` limits, and the list
reports each key's usage plus the global limits and usage (CTR-0285).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StrictInt

from app.api_keys import store as key_store
from app.auth import Identity, verify_key_admin
from app.core import config

router = APIRouter(prefix="/api/api-keys", tags=["API Keys"])

_NO_STORE = {"Cache-Control": "no-store"}


class KeyLimits(BaseModel):
    """Per-key /v1/responses limits (PRP-0254). 0 or absent = no limit."""

    model_config = ConfigDict(extra="forbid")

    requests_per_minute: StrictInt | None = None
    requests_per_day: StrictInt | None = None
    requests_per_month: StrictInt | None = None
    total_tokens_per_day: StrictInt | None = None
    total_tokens_per_month: StrictInt | None = None


class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    scope: str
    expires_at: str | None = None
    limits: KeyLimits | None = None


class UpdateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    scope: str | None = None
    expires_at: str | None = None
    enabled: bool | None = None
    limits: KeyLimits | None = None


def _actor(identity: Identity) -> str:
    return f"{identity.source}:{identity.subject}"


def _check_grant(identity: Identity, scope: str | None) -> None:
    if scope is not None and scope in key_store.SCOPES and not key_store.scope_allows(identity.scope, scope):
        raise HTTPException(status_code=403, detail=f"Your credential cannot grant the {scope!r} scope.")


def _translate(exc: key_store.KeyStoreError) -> HTTPException:
    if isinstance(exc, key_store.KeyNotFound):
        return HTTPException(status_code=404, detail="No API key has that id.")
    if isinstance(exc, key_store.DuplicateKeyName):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, key_store.InvalidKeyInput):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


def _auth_status() -> dict[str, Any]:
    settings = config.settings
    return {
        "app_require_auth_on_lan": bool(settings.app_require_auth_on_lan),
        "web_auth_enabled": bool(getattr(settings, "web_auth_enabled", False)),
        "store_path_configured": bool(getattr(settings, "api_keys_file", "")),
    }


def _limits_payload(limits: KeyLimits | None) -> dict[str, int] | None:
    return None if limits is None else limits.model_dump(exclude_none=True)


@router.get("")
async def list_keys(response: Response, identity: Identity = Depends(verify_key_admin)) -> dict[str, Any]:
    response.headers.update(_NO_STORE)
    store = key_store.get_store()
    records = store.list()
    from app.openai_api import quota

    snapshot = quota.guard().snapshot(record.id for record in records)
    keys = []
    for record in records:
        item = record.public()
        usage = snapshot["keys"].get(record.id, {})
        item["usage"] = {
            name: usage.get(name, 0)
            for name in ("requests_today", "requests_month", "total_tokens_today", "total_tokens_month")
        }
        keys.append(item)
    return {
        "keys": keys,
        "auth": _auth_status(),
        "caller": {"source": identity.source, "scope": identity.scope, "key_id": identity.key_id},
        "responses_limits": {
            "limits": quota.global_limit_settings(),
            "usage": snapshot["usage"],
            "timezone": snapshot["timezone"],
            "in_flight": snapshot["in_flight"],
        },
    }


@router.post("", status_code=201)
async def create_key(
    body: CreateKeyRequest, response: Response, identity: Identity = Depends(verify_key_admin)
) -> dict[str, Any]:
    response.headers.update(_NO_STORE)
    _check_grant(identity, body.scope)
    try:
        record, secret = key_store.get_store().create(
            name=body.name,
            scope=body.scope,
            expires_at=body.expires_at,
            actor=_actor(identity),
            limits=_limits_payload(body.limits),
        )
    except key_store.KeyStoreError as exc:
        raise _translate(exc) from exc
    return {"key": record.public(), "secret": secret}


@router.patch("/{key_id}")
async def update_key(
    key_id: str, body: UpdateKeyRequest, response: Response, identity: Identity = Depends(verify_key_admin)
) -> dict[str, Any]:
    response.headers.update(_NO_STORE)
    _check_grant(identity, body.scope)
    sent = body.model_dump(exclude_unset=True)
    if "limits" in sent:
        sent["limits"] = _limits_payload(body.limits)
    store = key_store.get_store()
    try:
        current = store.get(key_id)
        # A narrower caller may not edit a key above its own scope either.
        _check_grant(identity, current.scope)
        record = store.update(key_id, actor=_actor(identity), **sent)
    except key_store.KeyStoreError as exc:
        raise _translate(exc) from exc
    return {"key": record.public()}


@router.post("/{key_id}/reissue")
async def reissue_key(
    key_id: str, response: Response, identity: Identity = Depends(verify_key_admin)
) -> dict[str, Any]:
    response.headers.update(_NO_STORE)
    store = key_store.get_store()
    try:
        _check_grant(identity, store.get(key_id).scope)
        record, secret = store.reissue(key_id, actor=_actor(identity))
    except key_store.KeyStoreError as exc:
        raise _translate(exc) from exc
    return {"key": record.public(), "secret": secret}


@router.delete("/{key_id}", status_code=204)
async def delete_key(key_id: str, identity: Identity = Depends(verify_key_admin)) -> Response:
    store = key_store.get_store()
    try:
        _check_grant(identity, store.get(key_id).scope)
        store.delete(key_id, actor=_actor(identity))
    except key_store.KeyStoreError as exc:
        raise _translate(exc) from exc
    return Response(status_code=204, headers=_NO_STORE)
