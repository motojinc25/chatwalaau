"""Ontology history: write log, versions, retention, diff and the trash (CTR-0170 v5, UDR-0184).

Every save already keeps the previous file as ``<id>.<ttl|trig>.bak-<UTC stamp>``
(UDR-0084 D10). This module makes those backups a product feature:

- **Log** (D1) -- one JSON line per write in ``<id>.history.jsonl`` (create, import,
  save, statements, restore, delete), appended under the ontology's write lock. It
  is metadata only: a missing or damaged log never blocks a write or a restore.
- **Versions** (D2) -- the current file plus the backups, newest first, each named
  by its backup file name (``current`` for the file) and labelled with the write
  that produced it, matched by revision. Backups without a log entry (made before
  v0.179.0) are "earlier versions".
- **Retention** (D3) -- after each write: keep the newest ``keep_recent``, the newest
  per UTC day within ``keep_days``, then drop the oldest while over ``max_mb``; the
  newest backup is never dropped. Only backups named in the log are pruned
  (PRP-0202 Q1): backups made before the log existed stay until removed by hand.
- **Diff** (D4) -- two versions parsed by the lossless codec, compared as quad sets
  (exact terms, graphs, blank-node labels) plus the document (prefixes, base, VERSION).
- **Trash** (D6) -- an id with backups and no catalog entry is a deleted ontology;
  it can be restored under the same id until ``keep_days`` after its delete.

Restores themselves go through ``store.save_ontology_text`` (D5).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.ontology import store

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

LOG_SUFFIX = ".history.jsonl"
CURRENT = "current"
KINDS = ("create", "import", "save", "statements", "restore", "delete")
# ``<id>.<ttl|trig>.bak-<%Y%m%dT%H%M%S%f>`` (store._backup); single segment, never a path.
BACKUP_RE = re.compile(r"^(ont_[0-9a-f]{12})\.(ttl|trig)\.bak-(\d{8}T\d{12})$")
_LOG_RE = re.compile(r"^(ont_[0-9a-f]{12})" + re.escape(LOG_SUFFIX) + r"$")
DIFF_DEFAULT_LIMIT = 200
DIFF_MAX_LIMIT = 1000


class VersionNotFound(KeyError):
    """A version name that is not a backup of this ontology (404)."""


@dataclass(frozen=True)
class Backup:
    name: str
    ontology_id: str
    stamp: datetime
    size: int

    @property
    def format(self) -> str:
        return "trig" if f"{store.TRIG_SUFFIX}.bak-" in self.name else "turtle"


# ---- Log (D1) ------------------------------------------------------------------


def _log_path(ontology_id: str) -> Path:
    return store.ontology_dir() / f"{ontology_id}{LOG_SUFFIX}"


def append_entry(ontology_id: str, entry: dict[str, Any]) -> None:
    """Append one log line. Never raises: the log must not block a write (D1)."""
    line = json.dumps({"at": store._now(), **entry}, ensure_ascii=False, separators=(",", ":"))
    try:
        with store.write_lock(ontology_id), _log_path(ontology_id).open("ab") as fh:
            fh.write(line.encode("utf-8") + b"\n")
    except OSError:
        logger.warning("ontology history log append failed: %s", ontology_id, exc_info=True)


def read_entries(ontology_id: str) -> list[dict[str, Any]]:
    """The log lines in file order; damaged lines are skipped."""
    path = _log_path(ontology_id)
    with store.write_lock(ontology_id):
        try:
            raw = path.read_bytes() if path.is_file() else b""
        except OSError:
            return []
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("kind") in KINDS:
            out.append(item)
    return out


# ---- Backups and versions (D2) ---------------------------------------------------


def _stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%dT%H%M%S%f").replace(tzinfo=UTC)


def list_backups(ontology_id: str) -> list[Backup]:
    """This ontology's backups, newest first."""
    out: list[Backup] = []
    for path in store.ontology_dir().iterdir():
        match = BACKUP_RE.match(path.name)
        if match is None or match.group(1) != ontology_id:
            continue
        try:
            out.append(Backup(path.name, ontology_id, _stamp(match.group(3)), path.stat().st_size))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda b: b.stamp, reverse=True)
    return out


def backup_path(ontology_id: str, version: str) -> Path:
    """The file of a backup version; ``VersionNotFound`` unless it is this ontology's backup."""
    match = BACKUP_RE.match(version or "")
    if match is None or match.group(1) != ontology_id:
        raise VersionNotFound(version)
    path = store.ontology_dir() / version
    if not path.is_file():
        raise VersionNotFound(version)
    return path


def read_version(ontology_id: str, version: str) -> bytes:
    """The bytes of ``current`` or of one backup."""
    if version == CURRENT:
        data = store.read_ontology_bytes(ontology_id)
        if data is None:
            raise VersionNotFound(version)
        return data
    path = backup_path(ontology_id, version)
    with store.write_lock(ontology_id):
        return path.read_bytes()


def _producer(entries: list[dict[str, Any]], revision: str | None, before: str | None) -> dict[str, Any] | None:
    """The latest log entry before ``before`` whose write produced ``revision``."""
    if revision is None:
        return None
    for item in reversed(entries):
        if before is not None and str(item.get("at", "")) >= before:
            continue
        if item.get("revision_after") == revision:
            return item
    return None


def versions(ontology_id: str) -> list[dict[str, Any]]:
    """The current version and every backup, newest first, labelled from the log (D2)."""
    entries = read_entries(ontology_id)
    by_backup = {item["backup"]: item for item in entries if item.get("backup")}
    current = store.read_ontology_bytes(ontology_id) or b""
    out: list[dict[str, Any]] = []

    def row(version: str, revision: str | None, replaced: dict[str, Any] | None, size: int, fmt: str) -> dict:
        producer = _producer(entries, revision, replaced.get("at") if replaced else None)
        item: dict[str, Any] = {
            "version": version,
            "revision": revision,
            "kind": producer.get("kind") if producer else None,
            "created_at": producer.get("at") if producer else None,
            "replaced_at": None,
            "added": producer.get("added") if producer else None,
            "removed": producer.get("removed") if producer else None,
            "restored_from": producer.get("restored_from") if producer else None,
            "bytes": size,
            "format": fmt,
            "legacy": False,
        }
        return item

    entry = store.get_entry(ontology_id)
    current_format = "trig" if entry and store.is_dataset(entry) else "turtle"
    out.append(row(CURRENT, store.revision_of(current), None, len(current), current_format))
    for backup in list_backups(ontology_id):
        replaced = by_backup.get(backup.name)
        revision = replaced.get("revision_before") if replaced else None
        item = row(backup.name, revision, replaced, backup.size, backup.format)
        item["replaced_at"] = backup.stamp.isoformat()
        item["legacy"] = replaced is None
        out.append(item)
    return out


# ---- Retention (D3) ---------------------------------------------------------------


def _retention() -> tuple[int, int, int]:
    return (
        max(1, int(settings.ontology_history_keep_recent)),
        max(0, int(settings.ontology_history_keep_days)),
        max(0, int(settings.ontology_history_max_mb)) * 2**20,
    )


def prune(ontology_id: str, *, now: datetime | None = None) -> list[str]:
    """Apply the retention rules to this ontology's logged backups; returns the removed names.

    Backups not named in the log (made before v0.179.0, or a stray target backup) are
    never touched (PRP-0202 Q1). The newest logged backup is always kept.
    """
    keep_recent, keep_days, max_bytes = _retention()
    now = now or datetime.now(UTC)
    with store.write_lock(ontology_id):
        logged = {item["backup"] for item in read_entries(ontology_id) if item.get("backup")}
        backups = [b for b in list_backups(ontology_id) if b.name in logged]
        if not backups:
            return []
        keep = {b.name for b in backups[:keep_recent]}
        cutoff = now - timedelta(days=keep_days)
        days: set[Any] = set()
        for backup in backups:  # newest first: the first one seen per day is that day's newest
            day = backup.stamp.date()
            if keep_days and backup.stamp >= cutoff and day not in days:
                keep.add(backup.name)
                days.add(day)
        if max_bytes:
            kept = [b for b in backups if b.name in keep]
            total = sum(b.size for b in kept)
            for backup in reversed(kept):  # oldest first
                if total <= max_bytes:
                    break
                if backup is backups[0]:
                    continue  # the newest backup is never dropped
                keep.discard(backup.name)
                total -= backup.size
        removed: list[str] = []
        for backup in backups:
            if backup.name in keep:
                continue
            try:
                (store.ontology_dir() / backup.name).unlink()
                removed.append(backup.name)
            except OSError:
                logger.warning("could not remove ontology backup %s", backup.name, exc_info=True)
    if removed:
        logger.info("ontology history pruned: %s (%d backups)", ontology_id, len(removed))
    return removed


# ---- Trash (D6) -----------------------------------------------------------------------


def _ids_on_disk() -> set[str]:
    ids: set[str] = set()
    for path in store.ontology_dir().iterdir():
        match = BACKUP_RE.match(path.name) or _LOG_RE.match(path.name)
        if match is not None:
            ids.add(match.group(1))
    return ids


def _last_delete(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(entries):
        if item.get("kind") == "delete":
            return item
    return None


def _expire(ontology_id: str, deleted: dict[str, Any], now: datetime) -> bool:
    """Remove a deleted ontology's logged backups and log once ``keep_days`` have passed."""
    _, keep_days, _ = _retention()
    try:
        at = datetime.fromisoformat(str(deleted.get("at")))
    except ValueError:
        return False
    if now - at < timedelta(days=keep_days):
        return False
    with store.write_lock(ontology_id):
        if store.get_entry(ontology_id) is not None:
            return False  # restored meanwhile
        logged = {item["backup"] for item in read_entries(ontology_id) if item.get("backup")}
        for backup in list_backups(ontology_id):
            if backup.name in logged:
                try:
                    (store.ontology_dir() / backup.name).unlink()
                except OSError:
                    logger.warning("could not remove ontology backup %s", backup.name, exc_info=True)
        with contextlib.suppress(OSError):
            _log_path(ontology_id).unlink()
    logger.info("deleted ontology %s expired from the trash", ontology_id)
    return True


def trash(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """Deleted ontologies (backups, no catalog entry), newest delete first; expired ones are removed."""
    now = now or datetime.now(UTC)
    _, keep_days, _ = _retention()
    catalog = {e["id"] for e in store.read_catalog()}
    out: list[dict[str, Any]] = []
    for ontology_id in sorted(_ids_on_disk() - catalog):
        entries = read_entries(ontology_id)
        deleted = _last_delete(entries)
        if deleted is not None and _expire(ontology_id, deleted, now):
            continue
        backups = list_backups(ontology_id)
        if not backups:
            continue
        newest = backups[0]
        deleted_at = deleted.get("at") if deleted else newest.stamp.isoformat()
        expires_at = None
        if deleted is not None:
            try:
                expires_at = (datetime.fromisoformat(str(deleted["at"])) + timedelta(days=keep_days)).isoformat()
            except ValueError:
                expires_at = None
        out.append(
            {
                "id": ontology_id,
                "name": (deleted or {}).get("name") or ontology_id,
                "description": (deleted or {}).get("description") or "",
                "deleted_at": deleted_at,
                "expires_at": expires_at,
                "version": newest.name,
                "bytes": newest.size,
                "format": newest.format,
                "legacy": deleted is None,
            }
        )
    out.sort(key=lambda item: str(item["deleted_at"]), reverse=True)
    return out


def expire_trash() -> None:
    """Run the trash expiry (on every delete and trash listing)."""
    trash()


# ---- Diff (D4) ----------------------------------------------------------------------------


def _statement_json(quad: Any) -> dict[str, Any]:
    from app.ontology.vocabulary import is_named, term_to_json

    out = {"s": term_to_json(quad.subject), "p": quad.predicate.value, "o": term_to_json(quad.object)}
    if is_named(quad):
        out["g"] = term_to_json(quad.graph_name)
    return out


def _parse(data: bytes) -> tuple[list[Any], dict[str, Any]]:
    from app.ontology.vocabulary import read_dataset

    if not data.strip():
        return [], {"prefixes": [], "base": None, "version": None}
    return read_dataset(data)


def diff(target: bytes, base: bytes, *, offset: int = 0, limit: int = DIFF_DEFAULT_LIMIT) -> dict[str, Any]:
    """``added`` = statements in ``target`` not in ``base``; ``removed`` = the reverse (D4)."""
    limit = max(1, min(int(limit), DIFF_MAX_LIMIT))
    offset = max(0, int(offset))
    target_quads, target_doc = _parse(target)
    base_quads, base_doc = _parse(base)
    target_set, base_set = set(target_quads), set(base_quads)
    added = sorted(target_set - base_set, key=str)
    removed = sorted(base_set - target_set, key=str)
    target_prefixes = {(p["prefix"], p["iri"]) for p in target_doc.get("prefixes") or []}
    base_prefixes = {(p["prefix"], p["iri"]) for p in base_doc.get("prefixes") or []}
    document: dict[str, Any] = {
        "prefixes_added": [{"prefix": p, "iri": i} for p, i in sorted(target_prefixes - base_prefixes)],
        "prefixes_removed": [{"prefix": p, "iri": i} for p, i in sorted(base_prefixes - target_prefixes)],
    }
    for key in ("base", "version"):
        if (target_doc.get(key) or None) != (base_doc.get(key) or None):
            document[key] = [base_doc.get(key), target_doc.get(key)]
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "added": [_statement_json(q) for q in added[offset : offset + limit]],
        "removed": [_statement_json(q) for q in removed[offset : offset + limit]],
        "document": document,
        "offset": offset,
        "limit": limit,
    }


def previous_version(ontology_id: str, version: str) -> str | None:
    """The next older version in the list (None for the oldest)."""
    names = [item["version"] for item in versions(ontology_id)]
    if version not in names:
        raise VersionNotFound(version)
    index = names.index(version)
    return names[index + 1] if index + 1 < len(names) else None


def version_diff(
    ontology_id: str, version: str, against: str = "previous", *, offset: int = 0, limit: int = DIFF_DEFAULT_LIMIT
) -> dict[str, Any]:
    """The diff of ``version`` against ``previous`` (what produced it), ``current`` or a version."""
    target = read_version(ontology_id, version)
    if against == "previous":
        older = previous_version(ontology_id, version)
        base = read_version(ontology_id, older) if older else b""
        resolved = older
    else:
        base = read_version(ontology_id, against)
        resolved = against
    return {"version": version, "against": resolved, **diff(target, base, offset=offset, limit=limit)}


__all__ = [
    "BACKUP_RE",
    "CURRENT",
    "KINDS",
    "LOG_SUFFIX",
    "Backup",
    "VersionNotFound",
    "append_entry",
    "backup_path",
    "diff",
    "expire_trash",
    "list_backups",
    "previous_version",
    "prune",
    "read_entries",
    "read_version",
    "trash",
    "version_diff",
    "versions",
]
