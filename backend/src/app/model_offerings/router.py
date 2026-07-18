"""Model Offering Catalog Management API (CTR-0175, PRP-0111, UDR-0090).

Three endpoints let an operator READ and WRITE the Model Offering Catalog
(FEAT-0059 / CTR-0174) at runtime, applying changes with in-process hot reload
so a new ``model_offerings.jsonc`` takes effect without a restart (UDR-0090 D2):

    GET  /api/model-offerings         -- the current catalog in structured form
                                         (offerings + auth_profiles + task-model
                                         roles + the role_registry, PRP-0115), the
                                         active lane, a validity check, and
                                         env_status booleans for referenced env-var
                                         NAMES (never secret values, D4).
    PUT  /api/model-offerings         -- validate (parity with the loader, D8),
                                         write the file as structured JSON
                                         (comments dropped, D3), then reset the
                                         cache and REBUILD all per-model agents
                                         (CTR-0070), responding only AFTER the
                                         rebuild (drives the SPA "applying"
                                         indicator). All-or-nothing: on any
                                         failure the prior catalog keeps serving.
    POST /api/model-offerings/reload  -- re-read the file from disk and rebuild
                                         (so a hand-edit or a CLI write while the
                                         server runs is picked up).

All endpoints are gated by CTR-0083 (``verify_api_key``); loopback bypass keeps
localhost-first development zero-config (UDR-0090 D6, matching CTR-0143). Under
DEMO_MODE the catalog is ignored for routing (UDR-0087 D10); the mutating
endpoints refuse with 409 and the GUI renders read-only (UDR-0090 D7).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import models_catalog
from app.auth import verify_api_key
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/model-offerings", tags=["Model Offerings"])


class CatalogPayload(BaseModel):
    """A raw Model Offering Catalog submitted by the management UI or CLI.

    Structurally permissive: the authoritative validation is
    ``models_catalog.parse_catalog`` (validation parity with the loader, D8),
    which rejects the whole payload with a clear message before any write.
    """

    model_config = ConfigDict(extra="allow")

    offerings: list[dict[str, Any]] = Field(default_factory=list)
    auth_profiles: dict[str, Any] = Field(default_factory=dict)
    # Task-model assignments (PRP-0115, UDR-0096). A role key -> offering id (or a
    # {"model": "<id>"} object). Round-tripped through the same validate -> write ->
    # hot-reload path; a full-document PUT must include it or a hand-authored block
    # would be erased (the PRP-0112 auth_profiles lesson).
    roles: dict[str, Any] = Field(default_factory=dict)


class EnvCheckRequest(BaseModel):
    """A batch of environment-variable NAMES to check for presence."""

    names: list[str] = Field(default_factory=list)


def register_model_offerings(app: FastAPI, *, agent_registry) -> None:
    """Mount the Model Offering Catalog management endpoints, closing over the registry.

    The router needs the live ``AgentRegistry`` (CTR-0070) so PUT / reload can
    rebuild it; the registry is the module-level singleton created in ``app.main``
    and shared by reference with the AG-UI / OpenAI-API surfaces (UDR-0090 D2).
    """

    def _guard_demo() -> None:
        if settings.demo_mode:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "demo_mode",
                    "message": "The Model Offering Catalog is disabled under DEMO_MODE.",
                },
            )

    async def _hot_reload() -> None:
        # Local import avoids a module-level import cycle
        # (agent_factory -> ... -> app.models_catalog consumers).
        from app.agui.agent_factory import rebuild_agent_registry

        models_catalog.reset_active()
        await rebuild_agent_registry(agent_registry)

    @router.get("", dependencies=[Depends(verify_api_key)])
    async def get_model_offerings() -> dict:
        """Return the current catalog, active lane, validity, and env_status."""
        return models_catalog.catalog_status()

    @router.post("/env-status", dependencies=[Depends(verify_api_key)])
    async def check_env_status(body: EnvCheckRequest) -> dict[str, bool]:
        """Report whether each requested env-var NAME is set (booleans only, D4).

        Lets the settings UI validate ``${VAR}`` / ``api_key_env`` references in
        real time -- against names that are not yet saved to the file -- without
        ever revealing a value. Read-only, so it is allowed under DEMO_MODE.
        """
        names = [n.strip() for n in body.names if isinstance(n, str) and n.strip()][:200]
        return models_catalog.detect_env(names)

    @router.put("", dependencies=[Depends(verify_api_key)])
    async def put_model_offerings(body: CatalogPayload) -> dict:
        """Validate, write, and hot-reload the catalog; return the refreshed status.

        Validation failure -> 400 and nothing is written (D8). A rebuild failure
        restores the prior file and rebuilds the prior agents -> 500, so the apply
        is all-or-nothing (D2).
        """
        _guard_demo()

        path = models_catalog.catalog_path()
        if path is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "catalog_path_unset",
                    "message": "MODEL_OFFERINGS_FILE is unset; set it to a path to enable catalog management.",
                },
            )

        data: dict[str, Any] = {"offerings": body.offerings, "auth_profiles": body.auth_profiles}
        if body.roles:
            data["roles"] = body.roles

        # Snapshot the prior file bytes for rollback BEFORE writing (D2).
        prior_bytes = path.read_bytes() if path.is_file() else None

        try:
            models_catalog.write_catalog(data, path)  # validates (parity, D8) + atomic write (D3)
        except models_catalog.CatalogError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_catalog", "message": str(exc)}) from None

        try:
            await _hot_reload()
        except Exception:
            # Restore the prior file and rebuild the prior agents so the apply is
            # all-or-nothing (D2); the prior catalog keeps serving.
            if prior_bytes is not None:
                path.write_bytes(prior_bytes)
            else:
                path.unlink(missing_ok=True)
            try:
                await _hot_reload()
            except Exception:
                logger.exception("Model offering catalog rollback rebuild failed")
            logger.exception("Model offering catalog apply failed during agent rebuild")
            raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None

        logger.info("Model Offering Catalog updated and hot-reloaded (%s)", path.name)
        return models_catalog.catalog_status()

    @router.post("/reload", dependencies=[Depends(verify_api_key)])
    async def reload_model_offerings() -> dict:
        """Re-read the catalog file from disk and rebuild so on-disk changes apply."""
        _guard_demo()
        try:
            await _hot_reload()
        except Exception:
            logger.exception("Model offering catalog reload failed during agent rebuild")
            raise HTTPException(status_code=500, detail={"error": "agent_rebuild_failed"}) from None
        return models_catalog.catalog_status()

    app.include_router(router)


__all__ = ["register_model_offerings", "router"]
