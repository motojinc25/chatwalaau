"""Skill source adapter protocol and the built-in source table (CTR-0203, PRP-0165).

A source knows three things and nothing else:

    list_skills()      -- what is publishable from this source, right now
    revision_of(entry) -- an OPAQUE token identifying that skill's exact content
    fetch_skill(entry) -- the bytes, as (relpath, data) pairs

``revision_of`` is opaque ON PURPOSE (UDR-0145 D5). The GitHub adapter returns a
git tree SHA -- an exact content identifier that costs nothing to obtain, which is
what lets "is there an update?" be answered without downloading anything. Nothing
outside the adapter is allowed to know that, so the next source is free to answer
with an ETag, a digest or a version string.

The built-in table is CLOSED. It is the allowlist: an id that is not in a catalog
built from this table cannot be installed, because install resolves its target in
the catalog and installs nothing else (UDR-0144 D2). `SKILL_CATALOG_SOURCES_FILE`
REPLACES the table, and it is a file on the server -- there is deliberately no
"install from URL" input anywhere in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings

logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """A source could not be listed or fetched (network, rate limit, bad shape)."""


@dataclass(frozen=True)
class SourceDef:
    """One configured source: a repository, the paths to scan, the install group.

    ``group`` is the on-disk normalization target. Upstream layouts differ --
    ``skills/.curated``, ``skills/.system``, ``skills/``, the repository root --
    and all of them land as ``<SKILLS_DIR>/<group>/<name>/`` so every installed
    skill sits at the one depth MAF discovers (UDR-0146 D1).
    """

    id: str
    display_name: str
    repo: str
    paths: tuple[str, ...]
    group: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "repo": self.repo,
            "paths": list(self.paths),
            "group": self.group,
        }


@dataclass
class CatalogEntry:
    """One publishable skill, as the catalog records it."""

    id: str
    group: str
    name: str
    description: str
    source_id: str
    repo: str
    repo_path: str
    revision: str
    source_commit: str
    license: dict[str, Any] | None = None
    file_count: int = 0
    total_bytes: int = 0
    has_scripts: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "name": self.name,
            "description": self.description,
            "source_id": self.source_id,
            "repo": self.repo,
            "repo_path": self.repo_path,
            "revision": self.revision,
            "source_commit": self.source_commit,
            "license": self.license,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "has_scripts": self.has_scripts,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> CatalogEntry | None:
        skill_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not skill_id or not name:
            return None
        license_raw = raw.get("license")
        return cls(
            id=skill_id,
            group=str(raw.get("group") or ""),
            name=name,
            description=str(raw.get("description") or ""),
            source_id=str(raw.get("source_id") or ""),
            repo=str(raw.get("repo") or ""),
            repo_path=str(raw.get("repo_path") or ""),
            revision=str(raw.get("revision") or ""),
            source_commit=str(raw.get("source_commit") or ""),
            license=license_raw if isinstance(license_raw, dict) else None,
            file_count=int(raw.get("file_count") or 0),
            total_bytes=int(raw.get("total_bytes") or 0),
            has_scripts=bool(raw.get("has_scripts")),
            warnings=[str(w) for w in (raw.get("warnings") or []) if isinstance(w, str)],
        )


@dataclass
class FetchedSkill:
    """The bytes of one skill: POSIX relpaths mapped to their content."""

    members: dict[str, bytes]
    revision: str
    source_commit: str


@runtime_checkable
class SkillSource(Protocol):
    """What every source adapter implements (CTR-0203)."""

    id: str
    display_name: str

    async def list_skills(self) -> list[CatalogEntry]: ...

    def revision_of(self, entry: CatalogEntry) -> str: ...

    async def fetch_skill(self, entry: CatalogEntry) -> FetchedSkill: ...


# The closed, built-in table (PRP-0165 C3). Six (repository, path) pairs across
# five sources; openai publishes two directories under one id and one group.
DEFAULT_SOURCES: tuple[SourceDef, ...] = (
    SourceDef(
        id="openai",
        display_name="OpenAI",
        repo="openai/skills",
        paths=("skills/.curated", "skills/.system"),
        group="openai",
    ),
    SourceDef(
        id="anthropic",
        display_name="Anthropic",
        repo="anthropics/skills",
        paths=("skills",),
        group="anthropic",
    ),
    SourceDef(
        id="huggingface",
        display_name="Hugging Face",
        repo="huggingface/skills",
        paths=("skills",),
        group="huggingface",
    ),
    SourceDef(
        id="gstack",
        display_name="gstack",
        repo="garrytan/gstack",
        paths=("",),
        group="gstack",
    ),
    SourceDef(
        id="nvidia",
        display_name="NVIDIA",
        repo="nvidia/skills",
        paths=("skills",),
        group="nvidia",
    ),
)


def _parse_source_defs(raw: Any) -> list[SourceDef]:
    """Parse an operator-supplied source table. Raises ValueError on a bad shape."""
    rows = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("expected a list of sources, or an object with a 'sources' list")
    out: list[SourceDef] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("every source must be an object")
        source_id = str(row.get("id") or "").strip()
        repo = str(row.get("repo") or "").strip()
        if not source_id or not repo:
            raise ValueError("every source needs an 'id' and a 'repo'")
        if source_id in seen:
            raise ValueError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        paths_raw = row.get("paths")
        if isinstance(paths_raw, str):
            paths: tuple[str, ...] = (paths_raw,)
        elif isinstance(paths_raw, list) and paths_raw:
            paths = tuple(str(p).strip("/") for p in paths_raw)
        else:
            paths = ("",)
        group = str(row.get("group") or source_id).strip().strip("/")
        if "/" in group or group in (".", ".."):
            raise ValueError(f"invalid group for source {source_id}: {group!r}")
        out.append(
            SourceDef(
                id=source_id,
                display_name=str(row.get("display_name") or source_id),
                repo=repo,
                paths=paths,
                group=group,
            )
        )
    if not out:
        raise ValueError("the source table is empty")
    return out


def load_source_defs() -> list[SourceDef]:
    """Return the active source table.

    `SKILL_CATALOG_SOURCES_FILE` replaces the built-in table when it is set and
    readable. A configured-but-broken file falls back to the built-in table with a
    warning rather than leaving the operator with no catalog at all: the built-in
    table is the safe set, so degrading to it is never an escalation.
    """
    configured = (settings.skill_catalog_sources_file or "").strip()
    if not configured:
        return list(DEFAULT_SOURCES)
    path = Path(configured)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _parse_source_defs(raw)
    except (OSError, ValueError) as exc:
        logger.warning(
            "SKILL_CATALOG_SOURCES_FILE could not be used (%s: %s); the built-in source table is in effect.",
            path,
            exc,
        )
        return list(DEFAULT_SOURCES)


def build_source(definition: SourceDef) -> SkillSource:
    """Construct the adapter for one source definition.

    Only GitHub exists today. The dispatch lives here so a second scheme becomes a
    branch at one seam rather than a change at every call site.
    """
    from app.skills.sources.github import GitHubSkillSource

    return GitHubSkillSource(definition)


__all__ = [
    "DEFAULT_SOURCES",
    "CatalogEntry",
    "FetchedSkill",
    "SkillSource",
    "SourceDef",
    "SourceError",
    "build_source",
    "load_source_defs",
]
