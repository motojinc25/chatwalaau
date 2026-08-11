"""Application Settings Management API (CTR-0199, PRP-0136, UDR-0120).

Three endpoints let an operator READ and WRITE the application settings store
(CTR-0198) at runtime:

    GET  /api/app-settings         -- live values, the descriptor registry, the
                                      group registry, preserved unknown keys,
                                      load warnings, residual .env key NAMES, and
                                      the resolved path.
    PUT  /api/app-settings         -- full-document write: coerce -> write ->
                                      apply by scope -> rebuild if needed, then
                                      report whether a restart is still required.
    POST /api/app-settings/reload  -- re-read the file so a hand-edit or a CLI
                                      write while the server runs is picked up.

All endpoints are gated by CTR-0083 (``verify_api_key``), with the loopback
bypass that keeps localhost-first development zero-config -- satisfying
system-model invariant 7 for a mutating CAP-002 surface.

Two behaviours here exist because of specific past failures:

- The write is a FULL-DOCUMENT PUT, so it round-trips the preserved unknown-key
  bag (UDR-0120 D5). Omitting ``unknown`` from the payload PRESERVES what is on
  disk rather than deleting it; only an explicit ``{}`` clears it. A surface that
  replaces a whole document and forgets a field it does not edit silently
  destroys operator data -- exactly what UDR-0093 D4 found in ``auth_profiles``.
- The apply is ALL-OR-NOTHING (the CTR-0175 precedent): the prior file bytes are
  snapshotted before the write and restored if the agent rebuild fails, so a
  failed apply leaves the previous configuration serving.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.app_settings import descriptors as descriptors_mod
from app.app_settings import store as store_mod
from app.auth import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/app-settings", tags=["App Settings"])


class SettingsPayload(BaseModel):
    """A full application settings document submitted by the App Settings screen.

    ``unknown`` is deliberately OPTIONAL with a None default rather than an empty
    dict: omitting it means "keep whatever is on disk", so a client that has not
    been taught about preserved keys cannot delete them by accident. An explicit
    ``{}`` is the deliberate clear, which is what the per-key delete control in
    the GUI sends.
    """

    model_config = ConfigDict(extra="ignore")

    settings: dict[str, Any] = Field(default_factory=dict)
    unknown: dict[str, Any] | None = None


def register_app_settings(app: FastAPI, *, agent_registry) -> None:
    """Mount the application settings endpoints, closing over the agent registry.

    The registry (CTR-0070) is needed so a ``rebuild``-scope key can be applied in
    place -- the same live singleton the AG-UI and OpenAI-API surfaces share, and
    the same rebuild CTR-0175 performs for the catalog.
    """

    async def _rebuild_agents() -> None:
        # Local import avoids a module-level import cycle (agent_factory pulls in
        # a wide slice of the app).
        from app.agui.agent_factory import rebuild_agent_registry

        await rebuild_agent_registry(agent_registry)

    def _coerce_payload(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Coerce submitted values, rejecting only what is structurally wrong.

        A value that will not coerce is a 400 HERE, unlike the file load path
        which degrades (D7). The asymmetry is intentional: a file may predate the
        binary and must keep booting, but a PUT is a live operator action whose
        input is on screen and correctable, so silently storing something other
        than what was typed would be worse than an error message.
        """
        coerced: dict[str, Any] = {}
        errors: list[str] = []
        valid = descriptors_mod.known_keys()

        for key, value in raw.items():
            if key not in valid:
                errors.append(f"{key} is not a known setting")
                continue
            result, warning = store_mod.coerce_value(key, value)
            if warning:
                errors.append(warning)
                continue
            coerced[key] = result

        return coerced, errors

    @router.get("", dependencies=[Depends(verify_api_key)])
    async def get_app_settings() -> dict:
        """Return live values, descriptors, groups, unknown keys, and warnings."""
        return store_mod.store_status()

    @router.put("", dependencies=[Depends(verify_api_key)])
    async def put_app_settings(body: SettingsPayload) -> dict:
        """Validate, write, and apply the document; report if a restart remains."""
        path = store_mod.store_path()
        if path is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "settings_path_unset",
                    "message": (
                        "APP_SETTINGS_FILE is unset; set it to a path to enable application settings management."
                    ),
                },
            )

        coerced, errors = _coerce_payload(body.settings)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_settings", "message": "; ".join(errors)},
            )

        # Omitted `unknown` preserves the on-disk bag (see the class docstring).
        unknown = body.unknown
        if unknown is None:
            try:
                unknown = store_mod.load_store(path).unknown
            except store_mod.SettingsStoreError:
                unknown = {}

        prior_bytes = path.read_bytes() if path.is_file() else None

        try:
            store_mod.write_store(coerced, unknown, path)
        except (store_mod.SettingsStoreError, OSError) as exc:
            raise HTTPException(status_code=400, detail={"error": "write_failed", "message": str(exc)}) from None

        # Which restart-scope keys actually CHANGED. Captured before the apply,
        # because afterwards the singleton already holds the new value and the
        # comparison would report nothing. Reporting every submitted restart-scope
        # key instead would tell an operator to restart for a field they re-saved
        # unchanged.
        from app.core.config import settings as _live

        changed_restart_keys = sorted(
            key
            for key, value in coerced.items()
            if (d := descriptors_mod.descriptor(key))
            and d.scope == descriptors_mod.SCOPE_RESTART
            and getattr(_live, key, None) != value
        )

        scopes = store_mod.apply_values(coerced)
        scopes |= store_mod.apply_defaults_for_absent(coerced)

        if store_mod.needs_rebuild(scopes):
            try:
                await _rebuild_agents()
            except Exception:
                # All-or-nothing: restore the prior file and the prior values, then
                # rebuild back so the previous configuration keeps serving (D2).
                if prior_bytes is not None:
                    path.write_bytes(prior_bytes)
                else:
                    path.unlink(missing_ok=True)
                try:
                    store_mod.apply_document(store_mod.load_store(path))
                    await _rebuild_agents()
                except Exception:
                    logger.exception("Application settings rollback rebuild failed")
                logger.exception("Application settings apply failed during agent rebuild")
                raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None

        status = store_mod.store_status()
        status["restart_required"] = store_mod.needs_restart(scopes)
        status["restart_required_keys"] = changed_restart_keys
        status["rebuilt"] = store_mod.needs_rebuild(scopes)

        logger.info("Application settings updated (%s)", path.name)
        return status

    @router.post("/reload", dependencies=[Depends(verify_api_key)])
    async def reload_app_settings() -> dict:
        """Re-read the store from disk and apply it, rebuilding agents if needed."""
        try:
            doc = store_mod.load_store()
        except store_mod.SettingsStoreError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_settings", "message": str(exc)}) from None

        scopes = store_mod.apply_document(doc)
        if store_mod.needs_rebuild(scopes):
            try:
                await _rebuild_agents()
            except Exception:
                logger.exception("Application settings reload failed during agent rebuild")
                raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None

        status = store_mod.store_status()
        status["restart_required"] = store_mod.needs_restart(scopes)
        status["rebuilt"] = store_mod.needs_rebuild(scopes)
        return status

    app.include_router(router)


__all__ = ["register_app_settings", "router"]
