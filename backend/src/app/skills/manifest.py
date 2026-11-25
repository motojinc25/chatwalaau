"""Skill manifests: import a skills-lock.json or a ChatWalaau manifest, export ours (PRP-0266 C6).

The server never runs ``npx skills`` or any npm command (UDR-0247 D6). It reads
what that CLI writes instead:

    skills-lock.json, version 1 / 2 (the project lock of vercel-labs/skills)
    {
      "version": 1,
      "skills": {
        "pnpm": {"source": "antfu/skills", "sourceType": "github",
                 "skillPath": "skills/pnpm/SKILL.md", "computedHash": "...",
                 "commit": "..."}                     <- version 2 only
      }
    }

Each ``github`` entry becomes a source (the repository, the skill folder as its
path, the commit as a pinned ref when there is one). ``computedHash`` is the CLI's
own algorithm and is never compared; the ledger computes its own hash.

The export is a ChatWalaau manifest only (``chatwalaau-skills.json``): a lock file
carrying our hash would make the CLI report every skill as changed.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
from pathlib import PurePosixPath
from typing import Any

from app.skills import catalog as catalog_mod
from app.skills import state as state_mod
from app.skills.install import InstallError, install_skill
from app.skills.sources import (
    LOCAL_GROUP,
    UnsupportedSourceError,
    curated_source_defs,
    parse_source_spec,
    slug,
)
from app.skills.user_sources import require_writable

logger = logging.getLogger(__name__)

MANIFEST_KIND = "chatwalaau-skills"
MANIFEST_SCHEMA_VERSION = 1


def export_manifest() -> dict[str, Any]:
    """The ChatWalaau manifest of this deployment's added sources and installs."""
    state = state_mod.read_state()
    skills = []
    local = []
    for record in sorted(state.installed.values(), key=lambda r: r.id):
        if record.source_id == LOCAL_GROUP:
            local.append(record.name)
            continue
        skills.append(
            {
                "id": record.id,
                "source_id": record.source_id,
                "repo": record.repo,
                "repo_path": record.repo_path,
                "commit": record.source_commit,
                "ref": record.ref,
                "trust": record.trust,
            }
        )
    return {
        "kind": MANIFEST_KIND,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": state_mod.now_iso(),
        "sources": [
            {k: v for k, v in src.to_json().items() if k not in ("added_by", "added_at")}
            for src in sorted(state.sources.values(), key=lambda s: s.id)
        ],
        "skills": skills,
        "local": local,
    }


def _detect(document: Any) -> str:
    if isinstance(document, dict) and document.get("kind") == MANIFEST_KIND:
        return "chatwalaau"
    if isinstance(document, dict) and isinstance(document.get("skills"), dict) and "version" in document:
        return "skills-lock"
    raise InstallError(
        "unsupported_manifest",
        "Expected a skills-lock.json (version 1 or 2) or a chatwalaau-skills.json manifest.",
        status=400,
    )


def _existing_source_for(repo: str, ref: str) -> tuple[str, str] | None:
    """(source id, group) of an active source that already publishes ``repo`` at ``ref``."""
    for definition in curated_source_defs():
        if definition.repo.lower() == repo.lower() and definition.ref == ref:
            return definition.id, definition.group
    for source in state_mod.read_state().sources.values():
        if source.repo.lower() == repo.lower() and source.ref == ref:
            return source.id, source.group
    return None


def _plan_from_lock(document: dict[str, Any]) -> dict[str, Any]:
    """Turn a skills-lock.json into sources to add and skills to install."""
    unsupported: list[dict[str, str]] = []
    groups: OrderedDict[tuple[str, str], list[tuple[str, str]]] = OrderedDict()
    for name, raw in (document.get("skills") or {}).items():
        if not isinstance(raw, dict):
            unsupported.append({"name": str(name), "reason": "not an object"})
            continue
        if str(raw.get("sourceType") or "") != "github":
            unsupported.append({"name": str(name), "reason": f"source type {raw.get('sourceType')!r} is not supported"})
            continue
        try:
            spec = parse_source_spec(str(raw.get("source") or ""))
        except UnsupportedSourceError as exc:
            unsupported.append({"name": str(name), "reason": str(exc)})
            continue
        skill_path = str(raw.get("skillPath") or "").strip("/")
        folder = str(PurePosixPath(skill_path).parent) if skill_path.endswith("SKILL.md") else skill_path
        folder = "" if folder == "." else folder
        if not folder:
            unsupported.append({"name": str(name), "reason": "a skill at the repository root has no folder name"})
            continue
        ref = str(raw.get("commit") or "").strip() or spec.ref
        groups.setdefault((spec.repo, ref), []).append((str(name), folder))
    return _plan(groups, unsupported)


def _plan_from_manifest(document: dict[str, Any]) -> dict[str, Any]:
    unsupported = [
        {"name": str(name), "reason": "skills written here travel by ZIP, not by manifest"}
        for name in document.get("local") or []
    ]
    groups: OrderedDict[tuple[str, str], list[tuple[str, str]]] = OrderedDict()
    for raw in document.get("skills") or []:
        if not isinstance(raw, dict):
            continue
        repo = str(raw.get("repo") or "")
        folder = str(raw.get("repo_path") or "").strip("/")
        name = PurePosixPath(folder).name if folder else str(raw.get("id") or "").rpartition("/")[2]
        if not repo or not folder:
            unsupported.append({"name": name or "?", "reason": "no repository or path"})
            continue
        ref = str(raw.get("ref") or "")
        groups.setdefault((repo, ref), []).append((name, folder))
    return _plan(groups, unsupported)


def _plan(groups: OrderedDict, unsupported: list[dict[str, str]]) -> dict[str, Any]:
    taken = {"local", "installed"}
    for definition in curated_source_defs():
        taken.update({definition.id, definition.group})
    for source in state_mod.read_state().sources.values():
        taken.update({source.id, source.group})

    sources_to_add: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    for (repo, ref), items in groups.items():
        existing = _existing_source_for(repo, ref)
        if existing is not None:
            source_id, group = existing
        else:
            base = slug(repo.replace("/", "-") + (f"-{ref[:7]}" if ref else ""))
            source_id, n = base, 2
            while source_id in taken:
                source_id, n = f"{base[:60]}-{n}", n + 1
            taken.add(source_id)
            group = source_id
            sources_to_add.append(
                {"id": source_id, "repo": repo, "ref": ref, "paths": sorted({folder for _n, folder in items})}
            )
        for name, folder in items:
            skills.append(
                {
                    "name": name,
                    "id": f"{group}/{PurePosixPath(folder).name}",
                    "source_id": source_id,
                    "repo": repo,
                    "repo_path": folder,
                    "ref": ref,
                }
            )
    return {"sources_to_add": sources_to_add, "skills": skills, "unsupported": unsupported}


async def import_manifest(
    document: Any, *, apply: bool = False, select: list[str] | None = None, added_by: str = ""
) -> dict[str, Any]:
    """Preview (default) or apply a manifest import. Returns the plan and, on apply, the results."""
    require_writable()
    kind = _detect(document)
    plan = _plan_from_lock(document) if kind == "skills-lock" else _plan_from_manifest(document)
    plan["format"] = kind
    if not apply:
        return plan

    for src in plan["sources_to_add"]:
        state_mod.save_source(
            state_mod.AddedSource(
                id=src["id"],
                display_name=src["repo"],
                repo=src["repo"],
                paths=list(src["paths"]),
                group=src["id"],
                ref=src["ref"],
                added_by=added_by,
                added_at=state_mod.now_iso(),
            )
        )
    for source_id in sorted({s["source_id"] for s in plan["skills"]}):
        try:
            await catalog_mod.refresh_catalog(source_id)
        except KeyError:
            continue

    wanted = set(select) if select else None
    results = []
    for skill in plan["skills"]:
        if wanted is not None and skill["name"] not in wanted and skill["id"] not in wanted:
            continue
        try:
            await install_skill(skill["id"])
            results.append({"id": skill["id"], "status": "installed"})
        except InstallError as exc:
            results.append({"id": skill["id"], "status": "failed", "error": exc.code, "message": exc.message})
    plan["results"] = results
    logger.info(
        "Skill manifest imported (%s): %d source(s) added, %d installed, %d failed",
        kind,
        len(plan["sources_to_add"]),
        sum(1 for r in results if r["status"] == "installed"),
        sum(1 for r in results if r["status"] == "failed"),
    )
    return plan


__all__ = ["MANIFEST_KIND", "export_manifest", "import_manifest"]
