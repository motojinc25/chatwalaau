"""Skill catalog snapshot: refresh, read, and the merged operator view (CTR-0202, PRP-0165).

The catalog is a SNAPSHOT with a timestamp, not a live view of the upstream
repositories (UDR-0147). Three consequences are implemented here rather than
described anywhere else:

- Refresh is per-source atomic. A source that fails keeps its PREVIOUS block and
  its previous skills, marked ``stale`` with the error attached, while the others
  update. All-or-nothing would let one rate-limited repository blank a catalog
  that was working a second ago.
- The index holds POINTERS AND METADATA ONLY -- never a skill body. Nothing that
  is fetched here is redistributed by this repository, which is what keeps the
  licensing question at the operator's own server (UDR-0147 D2).
- A ledger entry with no catalog row is reported as ``in_catalog: false``, which
  the UI must render as "not in the current catalog" and never as "deleted
  upstream". A snapshot cannot tell the difference between an upstream deletion, a
  stale refresh and a source that failed (UDR-0147 D5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from app.core.config import settings
from app.demo import is_demo_mode
from app.skills import state as state_mod
from app.skills.hashing import hash_skill_dir
from app.skills.sources import CatalogEntry, SourceDef, SourceError, build_source, load_source_defs

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

INSTALLED_SOURCE_ID = "installed"


def install_available() -> bool:
    """True when the WRITE side of Skills is usable.

    Demo mode is checked through the single chokepoint (UDR-0041 D1) and wins over
    the setting: installing places third-party executable content on the server,
    which a public demo must never do (UDR-0144 D4).
    """
    return bool(settings.skill_install_enabled) and not is_demo_mode()


# ---------------------------------------------------------------------------
# Index file
# ---------------------------------------------------------------------------


def read_catalog() -> dict[str, Any]:
    """Read the catalog snapshot, or an empty document when there is none."""
    path = state_mod.catalog_path()
    if path is None or not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "generated_at": "", "sources": [], "skills": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Skill catalog file is unreadable: %s (treating as empty)", path, exc_info=True)
        return {"schema_version": SCHEMA_VERSION, "generated_at": "", "sources": [], "skills": []}
    if not isinstance(raw, dict):
        return {"schema_version": SCHEMA_VERSION, "generated_at": "", "sources": [], "skills": []}
    raw.setdefault("sources", [])
    raw.setdefault("skills", [])
    raw.setdefault("generated_at", "")
    return raw


def write_catalog(document: dict[str, Any]) -> None:
    """Persist the catalog snapshot atomically."""
    path = state_mod.catalog_path()
    if path is None:
        return
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except OSError:
        logger.warning("Could not write the skill catalog file: %s", path, exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def catalog_entries() -> dict[str, CatalogEntry]:
    """Parsed catalog entries keyed by id."""
    out: dict[str, CatalogEntry] = {}
    for row in read_catalog().get("skills") or []:
        if not isinstance(row, dict):
            continue
        entry = CatalogEntry.from_json(row)
        if entry is not None:
            out[entry.id] = entry
    return out


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


async def _list_one(definition: SourceDef) -> tuple[SourceDef, list[CatalogEntry] | None, str]:
    """List one source, converting any failure into an error string."""
    try:
        source = build_source(definition)
        entries = await source.list_skills()
        return (definition, entries, "")
    except SourceError as exc:
        return (definition, None, str(exc))
    except Exception as exc:  # a broken adapter must not fail the whole refresh
        logger.exception("Skill source %s failed during listing", definition.id)
        return (definition, None, f"{definition.repo}: {exc.__class__.__name__}")


async def refresh_catalog() -> dict[str, Any]:
    """Rebuild the catalog snapshot from every configured source."""
    definitions = load_source_defs()
    previous = read_catalog()
    prev_sources = {str(s.get("id")): s for s in (previous.get("sources") or []) if isinstance(s, dict) and s.get("id")}
    prev_skills: dict[str, list[dict[str, Any]]] = {}
    for row in previous.get("skills") or []:
        if isinstance(row, dict) and row.get("source_id"):
            prev_skills.setdefault(str(row["source_id"]), []).append(row)

    results = await asyncio.gather(*(_list_one(d) for d in definitions))

    now = state_mod.now_iso()
    sources_out: list[dict[str, Any]] = []
    skills_out: list[dict[str, Any]] = []
    failures = 0

    for definition, entries, error in results:
        block = definition.to_json()
        if entries is None:
            failures += 1
            prior = prev_sources.get(definition.id, {})
            kept = prev_skills.get(definition.id, [])
            block.update(
                {
                    "commit": prior.get("commit", ""),
                    "license": prior.get("license"),
                    "fetched_at": prior.get("fetched_at", ""),
                    "error": error,
                    "stale": True,
                    "skill_count": len(kept),
                }
            )
            skills_out.extend(kept)
            logger.warning("Skill catalog: source %s kept its previous entries (%s)", definition.id, error)
        else:
            commit = entries[0].source_commit if entries else ""
            license_block = next((e.license for e in entries if e.license), None)
            block.update(
                {
                    "commit": commit,
                    "license": license_block,
                    "fetched_at": now,
                    "error": None,
                    "stale": False,
                    "skill_count": len(entries),
                }
            )
            skills_out.extend(entry.to_json() for entry in entries)
        sources_out.append(block)

    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "sources": sources_out,
        "skills": sorted(skills_out, key=lambda s: str(s.get("id", ""))),
    }
    write_catalog(document)
    logger.info(
        "Skill catalog refreshed: %d skill(s) across %d source(s)%s",
        len(skills_out),
        len(definitions),
        f", {failures} source(s) kept stale entries" if failures else "",
    )
    return document


# ---------------------------------------------------------------------------
# Merged view
# ---------------------------------------------------------------------------


def _skill_row(
    entry: CatalogEntry,
    *,
    in_catalog: bool,
    installed: state_mod.InstalledSkill | None,
    disabled: set[str],
) -> dict[str, Any]:
    """One row of the merged view: catalog metadata plus the four live states."""
    row = entry.to_json()
    row["in_catalog"] = in_catalog
    row["installed"] = installed is not None
    row["update_available"] = False
    row["locally_modified"] = False
    row["missing"] = False
    row["installed_at"] = installed.installed_at if installed else ""
    row["enabled"] = entry.name not in disabled

    if installed is None:
        return row

    target = state_mod.skill_install_dir(installed.group, installed.name)
    if target is None or not target.is_dir():
        # The ledger says installed and the folder is not there: an ephemeral
        # SKILLS_DIR, an unmounted volume, or an out-of-band delete (UDR-0143 D4).
        row["missing"] = True
        return row

    current_hash, _files = hash_skill_dir(target)
    if installed.content_hash and current_hash and current_hash != installed.content_hash:
        row["locally_modified"] = True
    if in_catalog and installed.source_revision and entry.revision and entry.revision != installed.source_revision:
        row["update_available"] = True
    return row


def _entry_from_ledger(installed: state_mod.InstalledSkill) -> CatalogEntry:
    """Synthesize a catalog-shaped entry for an installed skill with no catalog row."""
    return CatalogEntry(
        id=installed.id,
        group=installed.group,
        name=installed.name,
        description="",
        source_id=installed.source_id,
        repo=installed.repo,
        repo_path=installed.repo_path,
        revision=installed.source_revision,
        source_commit=installed.source_commit,
        license=installed.license,
        file_count=len(installed.files),
        total_bytes=0,
        has_scripts=any(f.lower().endswith((".py", ".sh", ".bash", ".js", ".mjs", ".cjs")) for f in installed.files),
        warnings=[],
    )


def build_view() -> dict[str, Any]:
    """The CTR-0205 GET payload: catalog, ledger and disk state in one document."""
    document = read_catalog()
    entries = catalog_entries()
    state = state_mod.read_state()

    rows: list[dict[str, Any]] = [
        _skill_row(
            entry,
            in_catalog=True,
            installed=state.installed.get(entry.id),
            disabled=state.disabled,
        )
        for entry in entries.values()
    ]
    # Ledger rows with no catalog entry come LAST in construction but FIRST in the
    # UI: they are the skills that will not receive updates, so the operator has to
    # see them (UDR-0147 D5).
    rows += [
        _skill_row(
            _entry_from_ledger(installed),
            in_catalog=False,
            installed=installed,
            disabled=state.disabled,
        )
        for skill_id, installed in state.installed.items()
        if skill_id not in entries
    ]

    rows.sort(key=lambda r: (str(r.get("source_id", "")), str(r.get("name", "")).lower()))
    installed_count = sum(1 for r in rows if r["installed"])

    sources: list[dict[str, Any]] = [
        {
            "id": INSTALLED_SOURCE_ID,
            "display_name": "Installed",
            "repo": "",
            "paths": [],
            "group": "",
            "commit": "",
            "license": None,
            "fetched_at": "",
            "error": None,
            "stale": False,
            "skill_count": installed_count,
        }
    ]
    sources.extend(block for block in (document.get("sources") or []) if isinstance(block, dict))

    root = state_mod.skills_root()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": document.get("generated_at", ""),
        "install_enabled": bool(settings.skill_install_enabled),
        "demo_mode": is_demo_mode(),
        "available": install_available(),
        "skills_dir": str(root) if root is not None else "",
        "sources": sources,
        "skills": rows,
    }


__all__ = [
    "INSTALLED_SOURCE_ID",
    "SCHEMA_VERSION",
    "build_view",
    "catalog_entries",
    "install_available",
    "read_catalog",
    "refresh_catalog",
    "write_catalog",
]
