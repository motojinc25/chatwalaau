"""Skill sources an administrator adds by URL (CTR-0205 v2, PRP-0266 C1, UDR-0247 D1).

Adding a source is one deliberate, previewed act. Afterwards every install,
update and removal from it runs through the same checks as the curated table
(caps, paths, staging, ledger, collisions), because install still resolves only
catalog ids. An added source is always ``unverified`` (UDR-0247 D3).

Every function here is reached only through ``verify_key_admin`` routes, and each
one re-checks the write switches itself: demo mode and ``skill_install_enabled``
first, then ``skill_url_sources_enabled``.
"""

from __future__ import annotations

import logging

from app.skills import catalog as catalog_mod
from app.skills import state as state_mod
from app.skills.install import InstallError
from app.skills.sources import (
    ACCEPTED_FORMS,
    TRUST_UNVERIFIED,
    SourceDef,
    SourceError,
    UnsupportedSourceError,
    build_source,
    curated_source_defs,
    parse_source_spec,
    slug,
    url_sources_enabled,
    valid_id,
)

logger = logging.getLogger(__name__)


def require_writable() -> None:
    """Refuse unless installs and URL sources are both allowed."""
    if not catalog_mod.install_available():
        raise InstallError("install_disabled", "Skill installation is disabled in this deployment.", status=403)
    if not url_sources_enabled():
        raise InstallError(
            "url_sources_disabled",
            "Adding skill sources by URL is turned off (skill_url_sources_enabled).",
            status=403,
        )


def _spec_or_400(url: str):
    try:
        return parse_source_spec(url)
    except UnsupportedSourceError as exc:
        raise InstallError(
            "unsupported_source", str(exc), status=400, detail={"accepted_forms": list(ACCEPTED_FORMS)}
        ) from exc


def _taken() -> set[str]:
    """Ids and groups no new source may use."""
    taken = {"local", "installed"}
    for definition in curated_source_defs():
        taken.update({definition.id, definition.group})
    for source in state_mod.read_state().sources.values():
        taken.update({source.id, source.group})
    return taken


def _default_id(repo: str, taken: set[str]) -> str:
    base = slug(repo.replace("/", "-"))
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base[:60]}-{n}"
        n += 1
    return candidate


def _definition(url: str, *, source_id: str, display_name: str = "", group: str = "") -> SourceDef:
    spec = _spec_or_400(url)
    return SourceDef(
        id=source_id,
        display_name=display_name or spec.repo,
        repo=spec.repo,
        paths=(spec.path,) if spec.path else ("",),
        group=group or source_id,
        ref=spec.ref,
        trust=TRUST_UNVERIFIED,
    )


async def preview_source(url: str) -> dict:
    """Resolve ``url`` and list what it would publish. Writes nothing."""
    require_writable()
    spec = _spec_or_400(url)
    taken = _taken()
    suggested = _default_id(spec.repo, taken)
    definition = _definition(url, source_id=suggested)
    try:
        entries = await build_source(definition).list_skills()
    except SourceError as exc:
        raise InstallError("fetch_failed", str(exc), status=502) from exc
    return {
        "repo": spec.repo,
        "ref": spec.ref,
        "path": spec.path,
        "suggested_id": suggested,
        "commit": entries[0].source_commit if entries else "",
        "skills": [
            {
                "name": e.name,
                "description": e.description,
                "repo_path": e.repo_path,
                "file_count": e.file_count,
                "total_bytes": e.total_bytes,
                "has_scripts": e.has_scripts,
                "license": e.license,
                "warnings": list(e.warnings),
            }
            for e in entries
        ],
    }


async def add_source(
    url: str, *, source_id: str = "", display_name: str = "", group: str = "", added_by: str = ""
) -> state_mod.AddedSource:
    """Store a new added source and list it into the catalog."""
    require_writable()
    spec = _spec_or_400(url)
    taken = _taken()
    source_id = (source_id or "").strip() or _default_id(spec.repo, taken)
    group = (group or "").strip() or source_id
    for label, value in (("id", source_id), ("group", group)):
        if not valid_id(value):
            raise InstallError(
                "invalid_source_id",
                f"The source {label} {value!r} must be lowercase letters, digits, '.', '_' or '-' "
                "(at most 64), and not 'local' or 'installed'.",
                status=400,
            )
        if value in taken:
            raise InstallError(
                "source_exists", f"A source or group named {value!r} already exists.", status=409, detail={label: value}
            )
    record = state_mod.AddedSource(
        id=source_id,
        display_name=(display_name or "").strip() or spec.repo,
        repo=spec.repo,
        paths=[spec.path] if spec.path else [""],
        group=group,
        ref=spec.ref,
        added_by=added_by,
        added_at=state_mod.now_iso(),
    )
    state_mod.save_source(record)
    logger.info("Skill source added: %s (%s ref=%s) by %s", record.id, record.repo, record.ref or "default", added_by)
    await catalog_mod.refresh_catalog(record.id)
    return record


async def update_source_ref(source_id: str, ref: str) -> state_mod.AddedSource:
    """Pin an added source to a ref, or track its default branch again (empty)."""
    require_writable()
    record = state_mod.read_state().sources.get(source_id)
    if record is None:
        raise InstallError("unknown_source", f"{source_id} is not an added source.", status=404)
    ref = (ref or "").strip()
    if any(ch.isspace() for ch in ref) or "@" in ref:
        raise InstallError("invalid_ref", "A ref is a branch, tag or commit name without spaces.", status=400)
    record.ref = ref
    state_mod.save_source(record)
    logger.info("Skill source %s now %s", source_id, f"pinned to {ref}" if ref else "tracks its default branch")
    await catalog_mod.refresh_catalog(record.id)
    return record


def remove_source(source_id: str) -> state_mod.AddedSource:
    """Remove an added source and its catalog block. Installed skills stay."""
    require_writable()
    record = state_mod.read_state().sources.get(source_id)
    if record is None:
        raise InstallError("unknown_source", f"{source_id} is not an added source.", status=404)
    state_mod.drop_source(source_id)
    catalog_mod.drop_source_from_catalog(source_id)
    logger.info("Skill source removed: %s (%s)", source_id, record.repo)
    return record


__all__ = ["add_source", "preview_source", "remove_source", "require_writable", "update_source_ref"]
