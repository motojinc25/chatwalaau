"""GitHub skill source adapter (CTR-0204, PRP-0165).

Implements CTR-0203 against a public GitHub repository. Three API requests per
repository, then one download:

    GET /repos/{repo}                      -- default branch + repository license
    GET /repos/{repo}/git/ref/heads/{br}   -- the head commit SHA (small payload)
    GET /repos/{repo}/git/trees/{sha}?recursive=1
                                           -- every path, every directory tree SHA
    codeload.github.com/{repo}/tar.gz/{sha} -- the bytes

Two choices in there are load-bearing.

**The tree gives the revision for free.** Every directory in a recursive tree
response carries its own tree SHA, which is an exact identifier of that
directory's content. ``revision_of`` returns it, so "has upstream moved?" is a
string comparison against the ledger and never downloads anything (UDR-0145 D1).

**The tarball is one request, per-file reads are not.** Frontmatter (name,
description, license) can only come from the bytes of each SKILL.md, and a
repository publishes tens of skills. Fetching them individually through the
Contents API would exhaust the unauthenticated 60-requests/hour limit inside a
single refresh; ``codeload`` serves the whole repository once and is not part of
that budget (UDR-0144 D6). The same download is what ``fetch_skill`` extracts
from, pinned to the commit the catalog recorded.
"""

from __future__ import annotations

import io
import logging
from pathlib import PurePosixPath
import tarfile
from typing import Any

import httpx

from app.core.config import settings
from app.skills.frontmatter import parse_frontmatter
from app.skills.sources.base import CatalogEntry, FetchedSkill, SourceDef, SourceError

logger = logging.getLogger(__name__)

_API = "https://api.github.com"
_CODELOAD = "https://codeload.github.com"
_SKILL_FILE = "SKILL.md"
# Script extensions mirror the provider's discovery filter so `has_scripts` means
# "MAF will advertise run_skill_script for this skill", not "there is a file here".
_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".js", ".mjs", ".cjs")
# A repository tarball is not a skill payload, so it is bounded separately and
# generously: the per-skill caps (SKILL_INSTALL_MAX_BYTES / _FILES) still decide
# what may be written to disk.
_MAX_ARCHIVE_BYTES = 134_217_728  # 128 MiB


class GitHubSkillSource:
    """CTR-0203 adapter for one GitHub repository."""

    def __init__(self, definition: SourceDef) -> None:
        self._def = definition
        self.id = definition.id
        self.display_name = definition.display_name
        self._archive: bytes | None = None
        self._archive_commit: str | None = None

    # -- HTTP -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = (settings.skill_source_github_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _timeout(self) -> float:
        return float(max(1, settings.skill_source_timeout_seconds))

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Any:
        try:
            response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SourceError(f"{self._def.repo}: request failed ({exc.__class__.__name__})") from exc
        if response.status_code in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise SourceError(
                    f"{self._def.repo}: GitHub API rate limit reached. Set SKILL_SOURCE_GITHUB_TOKEN "
                    "or try again later."
                )
            raise SourceError(f"{self._def.repo}: GitHub refused the request (HTTP {response.status_code})")
        if response.status_code == 404:
            raise SourceError(f"{self._def.repo}: not found (private, renamed or deleted)")
        if response.status_code >= 400:
            raise SourceError(f"{self._def.repo}: GitHub returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self._def.repo}: GitHub returned a non-JSON response") from exc

    async def _download_archive(self, client: httpx.AsyncClient, commit: str) -> bytes:
        """Download the repository tarball at ``commit``, bounded and cached."""
        if self._archive is not None and self._archive_commit == commit:
            return self._archive
        url = f"{_CODELOAD}/{self._def.repo}/tar.gz/{commit}"
        buffer = bytearray()
        try:
            async with client.stream("GET", url, headers=self._headers()) as response:
                if response.status_code >= 400:
                    raise SourceError(f"{self._def.repo}: archive download failed (HTTP {response.status_code})")
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_ARCHIVE_BYTES:
                        raise SourceError(f"{self._def.repo}: archive exceeds {_MAX_ARCHIVE_BYTES} bytes")
        except httpx.HTTPError as exc:
            raise SourceError(f"{self._def.repo}: archive download failed ({exc.__class__.__name__})") from exc
        self._archive = bytes(buffer)
        self._archive_commit = commit
        return self._archive

    # -- Archive helpers --------------------------------------------------

    @staticmethod
    def _archive_members(archive: bytes, prefix: str) -> dict[str, bytes]:
        """Extract regular files under ``prefix`` (repo-relative) as relpath -> bytes.

        The tarball's own top-level directory (``repo-<sha>/``) is stripped. Only
        REGULAR files are read: a symlink or device member is skipped, because MAF
        rejects symlinked entries during discovery anyway (UDR-0128 D4), so writing
        one would produce a skill the runtime silently will not read.
        """
        out: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                parts = member.name.split("/", 1)
                if len(parts) != 2:
                    continue
                rel = parts[1]
                if prefix:
                    if not rel.startswith(prefix + "/"):
                        continue
                    rel = rel[len(prefix) + 1 :]
                if not rel:
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                out[rel] = handle.read()
        return out

    # -- CTR-0203 ---------------------------------------------------------

    async def list_skills(self) -> list[CatalogEntry]:
        """Enumerate every publishable skill in this repository."""
        async with httpx.AsyncClient(timeout=self._timeout(), follow_redirects=True) as client:
            meta = await self._get_json(client, f"{_API}/repos/{self._def.repo}")
            if not isinstance(meta, dict):
                raise SourceError(f"{self._def.repo}: unexpected repository payload")
            branch = str(meta.get("default_branch") or "main")
            repo_license = _license_from_repo(meta)

            ref = await self._get_json(client, f"{_API}/repos/{self._def.repo}/git/ref/heads/{branch}")
            commit = ""
            if isinstance(ref, dict) and isinstance(ref.get("object"), dict):
                commit = str(ref["object"].get("sha") or "")
            if not commit:
                raise SourceError(f"{self._def.repo}: could not resolve the head commit of {branch}")

            tree = await self._get_json(client, f"{_API}/repos/{self._def.repo}/git/trees/{commit}?recursive=1")
            if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
                raise SourceError(f"{self._def.repo}: unexpected tree payload")
            if tree.get("truncated"):
                raise SourceError(
                    f"{self._def.repo}: the repository tree is too large for one listing "
                    "(GitHub truncated it), so its skills cannot be enumerated reliably."
                )

            archive = await self._download_archive(client, commit)

        return self._entries_from_tree(tree["tree"], commit, repo_license, archive)

    def _entries_from_tree(
        self,
        tree: list[Any],
        commit: str,
        repo_license: dict[str, Any] | None,
        archive: bytes,
    ) -> list[CatalogEntry]:
        blobs: dict[str, dict[str, Any]] = {}
        trees: dict[str, str] = {}
        for node in tree:
            if not isinstance(node, dict):
                continue
            path = str(node.get("path") or "")
            if not path:
                continue
            if node.get("type") == "blob":
                blobs[path] = node
            elif node.get("type") == "tree":
                trees[path] = str(node.get("sha") or "")

        # Candidate skill directories: a directory DIRECTLY holding a non-empty
        # SKILL.md, under one of the configured paths.
        candidates: set[str] = set()
        for path, node in blobs.items():
            if PurePosixPath(path).name != _SKILL_FILE:
                continue
            if int(node.get("size") or 0) <= 0:
                continue
            skill_dir = str(PurePosixPath(path).parent)
            skill_dir = "" if skill_dir == "." else skill_dir
            if not skill_dir:
                continue  # a SKILL.md at the repository root has no name to install under
            if not self._under_configured_path(skill_dir):
                continue
            candidates.add(skill_dir)

        # Parent absorption (MAF 1.11 #6849): everything beneath a skill root
        # belongs to that skill, so a nested candidate is not an independent skill.
        skill_dirs = sorted(
            d for d in candidates if not any(d.startswith(other + "/") for other in candidates if other != d)
        )

        members = self._archive_members(archive, "") if skill_dirs else {}
        entries: list[CatalogEntry] = []
        for skill_dir in skill_dirs:
            name = PurePosixPath(skill_dir).name
            text = (members.get(f"{skill_dir}/{_SKILL_FILE}") or b"").decode("utf-8", errors="replace")
            front = parse_frontmatter(text)

            file_count = 0
            total_bytes = 0
            has_scripts = False
            for path, node in blobs.items():
                if not path.startswith(skill_dir + "/"):
                    continue
                file_count += 1
                total_bytes += int(node.get("size") or 0)
                if path.lower().endswith(_SCRIPT_SUFFIXES):
                    has_scripts = True

            warnings: list[str] = []
            declared = front.get("name", "")
            if declared and declared != name:
                warnings.append("name_mismatch")

            entries.append(
                CatalogEntry(
                    id=f"{self._def.group}/{name}",
                    group=self._def.group,
                    name=name,
                    description=front.get("description", ""),
                    source_id=self.id,
                    repo=self._def.repo,
                    repo_path=skill_dir,
                    revision=trees.get(skill_dir) or str(blobs[f"{skill_dir}/{_SKILL_FILE}"].get("sha") or ""),
                    source_commit=commit,
                    license=_license_for_skill(front.get("license"), repo_license),
                    file_count=file_count,
                    total_bytes=total_bytes,
                    has_scripts=has_scripts,
                    warnings=warnings,
                )
            )
        return entries

    def _under_configured_path(self, skill_dir: str) -> bool:
        for prefix in self._def.paths:
            clean = prefix.strip("/")
            if not clean:
                return True
            if skill_dir == clean or skill_dir.startswith(clean + "/"):
                return True
        return False

    def revision_of(self, entry: CatalogEntry) -> str:
        """The opaque upstream revision -- here, the skill directory's tree SHA."""
        return entry.revision

    async def fetch_skill(self, entry: CatalogEntry) -> FetchedSkill:
        """Download the skill's files at the commit the catalog recorded."""
        if not entry.source_commit:
            raise SourceError(f"{entry.id}: the catalog entry has no commit to fetch")
        async with httpx.AsyncClient(timeout=self._timeout(), follow_redirects=True) as client:
            archive = await self._download_archive(client, entry.source_commit)
        members = self._archive_members(archive, entry.repo_path)
        if not members:
            raise SourceError(f"{entry.id}: nothing found at {entry.repo_path} in {entry.repo}")
        return FetchedSkill(members=members, revision=entry.revision, source_commit=entry.source_commit)


def _license_from_repo(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Repository-level license, or None when GitHub reports none.

    None means UNKNOWN and is rendered as such. It is never smoothed into a
    plausible default: a guessed license is worse than an admitted gap, because
    the operator consents on what this value says (UDR-0147 D4).
    """
    raw = meta.get("license")
    if not isinstance(raw, dict):
        return None
    spdx = str(raw.get("spdx_id") or "").strip()
    if not spdx or spdx.upper() == "NOASSERTION":
        return None
    return {
        "spdx": spdx,
        "name": str(raw.get("name") or spdx),
        "url": str(raw.get("url") or ""),
        "inherited_from": "repository",
    }


def _license_for_skill(declared: str | None, repo_license: dict[str, Any] | None) -> dict[str, Any] | None:
    """A skill's own ``license:`` wins; otherwise the repository license is inherited."""
    value = (declared or "").strip()
    if value:
        return {"spdx": value, "name": value, "url": "", "inherited_from": "skill"}
    return repo_license


__all__ = ["GitHubSkillSource"]
