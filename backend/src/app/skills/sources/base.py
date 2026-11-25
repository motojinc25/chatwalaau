"""Skill source adapter protocol and the source table (CTR-0203, PRP-0165, PRP-0266).

A source knows three things and nothing else:

    list_skills()      -- what is publishable from this source, right now
    revision_of(entry) -- an OPAQUE token identifying that skill's exact content
    fetch_skill(entry) -- the bytes, as (relpath, data) pairs

``revision_of`` is opaque ON PURPOSE (UDR-0145 D5). The GitHub adapter returns a
git tree SHA -- an exact content identifier that costs nothing to obtain, which is
what lets "is there an update?" be answered without downloading anything. Nothing
outside the adapter is allowed to know that, so the next source is free to answer
with an ETag, a digest or a version string.

The built-in table is the CURATED set. Install resolves its target in the catalog
and installs nothing else (UDR-0144 D2), and the catalog is built from the curated
table (built-in, or ``SKILL_CATALOG_SOURCES_FILE`` replacing it) PLUS the sources an
administrator added by URL (PRP-0266, UDR-0247 D1). An added source is stored in
the CTR-0206 state file and labelled ``unverified``; there is still no route that
installs straight from a URL without the source being added first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

from app.core.config import settings

logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """A source could not be listed or fetched (network, rate limit, bad shape)."""


# Trust labels (UDR-0247 D3). A label, never a gate: scripts of an unverified skill
# get a warning at consent and nothing else beyond CODING_ENABLED and tool approval.
TRUST_CURATED = "curated"
TRUST_UNVERIFIED = "unverified"
TRUST_LOCAL = "local"

# Group and source id reserved for skills written in the product (UDR-0247 D5), and
# the pseudo-source the merged view lists first (app.skills.catalog).
LOCAL_GROUP = "local"
RESERVED_IDS = frozenset({LOCAL_GROUP, "installed"})

# Besides dot-folders, the folders an added source without an explicit path never
# scans. Tool folders such as .claude/, .agents/ and .cursor-plugin/ often hold a
# second copy of the same skills (playcanvas/skills does).
SKIPPED_DIR_NAMES = frozenset({"node_modules"})


@dataclass(frozen=True)
class SourceDef:
    """One configured source: a repository, the paths to scan, the install group.

    ``group`` is the on-disk normalization target. Upstream layouts differ --
    ``skills/.curated``, ``skills/.system``, ``skills/``, the repository root --
    and all of them land as ``<SKILLS_DIR>/<group>/<name>/`` so every installed
    skill sits at the one depth MAF discovers (UDR-0146 D1).

    ``ref`` (PRP-0266): empty tracks the default branch; a branch, tag or commit
    pins the source to it. ``trust`` is ``curated`` for the table and
    ``unverified`` for a source an administrator added.
    """

    id: str
    display_name: str
    repo: str
    paths: tuple[str, ...]
    group: str
    ref: str = ""
    trust: str = TRUST_CURATED

    @property
    def skips_hidden(self) -> bool:
        """True when dot-folders and node_modules are skipped (an added source scanning its whole repository)."""
        return self.trust == TRUST_UNVERIFIED and all(not p.strip("/") for p in self.paths)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "repo": self.repo,
            "paths": list(self.paths),
            "group": self.group,
            "ref": self.ref,
            "trust": self.trust,
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
    trust: str = TRUST_CURATED
    ref: str = ""

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
            "trust": self.trust,
            "ref": self.ref,
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
            # A schema_version 1 snapshot predates trust: everything in it was curated.
            trust=str(raw.get("trust") or TRUST_CURATED),
            ref=str(raw.get("ref") or ""),
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


# The built-in curated table (PRP-0165 C3). Six (repository, path) pairs across
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
        if group == LOCAL_GROUP or source_id in RESERVED_IDS:
            raise ValueError(f"source {source_id}: the names 'local' and 'installed' are reserved")
        out.append(
            SourceDef(
                id=source_id,
                display_name=str(row.get("display_name") or source_id),
                repo=repo,
                paths=paths,
                group=group,
                ref=str(row.get("ref") or "").strip(),
            )
        )
    if not out:
        raise ValueError("the source table is empty")
    return out


def curated_source_defs() -> list[SourceDef]:
    """Return the curated source table.

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


def url_sources_enabled() -> bool:
    """True when administrators may add sources by URL (App Setting, PRP-0266)."""
    return bool(getattr(settings, "skill_url_sources_enabled", True))


def added_source_defs() -> list[SourceDef]:
    """The sources an administrator added by URL, from the CTR-0206 state file."""
    from app.skills import state as state_mod

    return [record.to_def() for record in state_mod.read_state().sources.values()]


def load_source_defs() -> list[SourceDef]:
    """Return the active source table: the curated table plus the added sources.

    With ``skill_url_sources_enabled`` off the added sources are KEPT in the state
    file but leave the active table, so they are neither refreshed nor installed
    from until the setting is on again (UDR-0247 D1). An added source whose id or
    group a later curated table took over is left out rather than merged.
    """
    curated = curated_source_defs()
    if not url_sources_enabled():
        return curated
    taken = {d.id for d in curated} | {d.group for d in curated}
    return curated + [d for d in added_source_defs() if d.id not in taken and d.group not in taken]


# ---------------------------------------------------------------------------
# Source specs typed by an administrator (PRP-0266 C1)
# ---------------------------------------------------------------------------

_NAME_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_SHORTHAND = re.compile(rf"^(?P<owner>{_NAME_PART})/(?P<repo>{_NAME_PART})(?:@(?P<ref>[^\s@]+))?$")
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
ACCEPTED_FORMS = (
    "owner/repo",
    "owner/repo@<tag, branch or commit>",
    "https://github.com/owner/repo",
    "https://github.com/owner/repo/tree/<ref>/<path>",
)


class UnsupportedSourceError(ValueError):
    """The text is not a source form this deployment can read."""


@dataclass(frozen=True)
class SourceSpec:
    """A parsed source: repository, optional ref, optional path inside it."""

    repo: str
    ref: str = ""
    path: str = ""


def _clean_repo(owner: str, repo: str) -> str:
    repo = repo[:-4] if repo.lower().endswith(".git") else repo
    return f"{owner}/{repo}"


def parse_source_spec(text: str) -> SourceSpec:
    """Parse ``owner/repo[@ref]`` or a github.com URL. Raises UnsupportedSourceError.

    Only GitHub is read today (CTR-0204). Every other form -- GitLab, Azure Repos,
    git SSH URLs, local paths, direct archive URLs -- is refused with the list of
    accepted forms rather than guessed at.
    """
    raw = (text or "").strip()
    if not raw:
        raise UnsupportedSourceError("Enter a repository such as owner/repo or a GitHub URL.")
    match = _SHORTHAND.match(raw)
    if match:
        return SourceSpec(repo=_clean_repo(match["owner"], match["repo"]), ref=match["ref"] or "")
    parts = urlsplit(raw)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in _GITHUB_HOSTS:
        raise UnsupportedSourceError(
            "Only GitHub repositories are supported. Accepted forms: " + ", ".join(ACCEPTED_FORMS) + "."
        )
    if parts.username or parts.password or parts.query or parts.port:
        raise UnsupportedSourceError("A GitHub URL with credentials, a port or a query string is not accepted.")
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if len(segments) < 2 or not re.fullmatch(_NAME_PART, segments[0]) or not re.fullmatch(_NAME_PART, segments[1]):
        raise UnsupportedSourceError("The URL does not name a repository (https://github.com/owner/repo).")
    repo = _clean_repo(segments[0], segments[1])
    rest = segments[2:]
    if not rest:
        return SourceSpec(repo=repo)
    if rest[0] not in ("tree", "blob") or len(rest) < 2:
        raise UnsupportedSourceError("Use the repository URL, or a /tree/<ref>/<path> URL inside it.")
    ref = rest[1]
    path_parts = rest[2:]
    if path_parts and path_parts[-1] == "SKILL.md":
        path_parts = path_parts[:-1]
    if any(p in (".", "..") for p in path_parts):
        raise UnsupportedSourceError("The path inside the repository is not valid.")
    return SourceSpec(repo=repo, ref=ref, path="/".join(path_parts))


def slug(text: str) -> str:
    """A lowercase id / group candidate: letters, digits and hyphens."""
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return value[:64].strip("-") or "source"


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def valid_id(value: str) -> bool:
    """True for a usable source id or group name (never a reserved one)."""
    return bool(_ID_RE.match(value or "")) and value not in RESERVED_IDS


def build_source(definition: SourceDef) -> SkillSource:
    """Construct the adapter for one source definition.

    Only GitHub exists today. The dispatch lives here so a second scheme becomes a
    branch at one seam rather than a change at every call site.
    """
    from app.skills.sources.github import GitHubSkillSource

    return GitHubSkillSource(definition)


__all__ = [
    "ACCEPTED_FORMS",
    "DEFAULT_SOURCES",
    "LOCAL_GROUP",
    "RESERVED_IDS",
    "SKIPPED_DIR_NAMES",
    "TRUST_CURATED",
    "TRUST_LOCAL",
    "TRUST_UNVERIFIED",
    "CatalogEntry",
    "FetchedSkill",
    "SkillSource",
    "SourceDef",
    "SourceError",
    "SourceSpec",
    "UnsupportedSourceError",
    "added_source_defs",
    "build_source",
    "curated_source_defs",
    "load_source_defs",
    "parse_source_spec",
    "slug",
    "url_sources_enabled",
    "valid_id",
]
