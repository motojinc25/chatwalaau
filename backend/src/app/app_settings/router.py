"""Application Settings Management API (CTR-0199, PRP-0136, UDR-0120).

Three endpoints let an operator READ and WRITE the application settings store
(CTR-0198) at runtime:

    GET  /api/app-settings         -- live values, the descriptor registry, the
                                      group registry, preserved unknown keys,
                                      load warnings, residual .env key NAMES, and
                                      the resolved path.
    PATCH /api/app-settings        -- MERGE a subset of keys into the stored
                                      document (PRP-0184, UDR-0166 D8/D9), for a
                                      surface that owns two settings rather than
                                      the whole screen: the Built-in agent card
                                      and the narrow run-target picker.
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


class SettingsPatch(BaseModel):
    """A SUBSET of settings keys to merge into the stored document (UDR-0166 D8).

    The full-document PUT is the App Settings screen's contract: it owns every key
    on screen, so sending all of them is honest. A surface that owns TWO keys --
    the Built-in agent card's model + reasoning effort -- cannot use it without
    first reading and re-sending every unrelated value, which turns an unrelated
    concurrent edit into silent data loss. Hence a merge.
    """

    model_config = ConfigDict(extra="ignore")

    settings: dict[str, Any] = Field(default_factory=dict)


def _resolve_secret(key: str, value: Any) -> Any:
    """Turn a masked secret back into the stored one (UDR-0149 D2).

    GET never returns a secret's value -- it returns ``SECRET_MASK`` when one is
    set. A full-document PUT therefore carries the mask back for every save the
    operator makes for some OTHER reason, and storing it verbatim would overwrite
    the credential with eight asterisks on the first unrelated edit. The mask
    means "leave it alone"; anything else, including the empty string, is what
    the operator typed, and the empty string is how a secret is cleared.
    """
    desc = descriptors_mod.descriptor(key)
    if desc is None or not desc.secret:
        return value
    if isinstance(value, str) and value == descriptors_mod.SECRET_MASK:
        from app.core.config import settings as _settings

        return getattr(_settings, key, "")
    return value


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
            value = _resolve_secret(key, value)
            result, warning = store_mod.coerce_value(key, value)
            if warning:
                errors.append(warning)
                continue
            coerced[key] = result

        return coerced, errors

    async def _write_and_apply(coerced: dict[str, Any], unknown: dict[str, Any] | None) -> dict:
        """Write a validated document, apply it, rebuild when a scope demands it.

        Shared by PUT (the whole document) and PATCH (a merged subset) so the two
        cannot drift on the part that matters: the all-or-nothing rollback when the
        rebuild fails (UDR-0120 D2).
        """
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

        # Omitted `unknown` preserves the on-disk bag (see SettingsPayload).
        if unknown is None:
            try:
                unknown = store_mod.load_store(path).unknown
            except store_mod.SettingsStoreError:
                unknown = {}

        prior_bytes = path.read_bytes() if path.is_file() else None

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

        try:
            store_mod.write_store(coerced, unknown, path)
        except (store_mod.SettingsStoreError, OSError) as exc:
            raise HTTPException(status_code=400, detail={"error": "write_failed", "message": str(exc)}) from None

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

    @router.get("", dependencies=[Depends(verify_api_key)])
    async def get_app_settings() -> dict:
        """Return live values, descriptors, groups, unknown keys, and warnings."""
        return store_mod.store_status()

    @router.put("", dependencies=[Depends(verify_api_key)])
    async def put_app_settings(body: SettingsPayload) -> dict:
        """Validate, write, and apply the document; report if a restart remains."""
        coerced, errors = _coerce_payload(body.settings)
        # Cross-field rules run AFTER per-key coercion and BEFORE the write, so a
        # rule spanning two keys is enforced on the path an operator actually uses
        # (UDR-0141 D4). A `Settings` validator would not run here at all: apply
        # is an attribute assignment and the model does not set
        # `validate_assignment`, which is how an out-of-range bound used to save
        # successfully and then prevent the next start.
        # Cross-field rules run AFTER per-key coercion and BEFORE the write, so a
        # rule spanning two keys is enforced on the path an operator actually uses
        # (UDR-0141 D4).
        errors.extend(store_mod.validate_document(coerced))
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_settings", "message": "; ".join(errors)},
            )
        return await _write_and_apply(coerced, body.unknown)

    @router.patch("", dependencies=[Depends(verify_api_key)])
    async def patch_app_settings(body: SettingsPatch) -> dict:
        """Merge a SUBSET of keys into the stored document (PRP-0184, UDR-0166 D8).

        The submitted keys replace their stored counterparts; every other stored
        value and the preserved `unknown` bag are carried over untouched. Validation,
        the write, the apply and the rebuild are the PUT's, so a merged save is as
        atomic as a full one -- including the rollback when the rebuild fails.

        A merged save is still SERVER-WIDE (UDR-0166 D9): `core_agent_model` /
        `core_agent_effort` are rebuild-scope, so the answer changes for chat, the
        Teams channel, the CLI channel, the OpenAI-compatible API and every
        background lane at once. Saying so is the CALLING SURFACE's job -- this
        endpoint cannot know whether the operator was told.
        """
        coerced, errors = _coerce_payload(body.settings)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_settings", "message": "; ".join(errors)},
            )

        try:
            stored = store_mod.load_store().values
        except store_mod.SettingsStoreError:
            stored = {}
        merged = {**stored, **coerced}

        errors = store_mod.validate_document(merged)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_settings", "message": "; ".join(errors)},
            )
        return await _write_and_apply(merged, None)

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
