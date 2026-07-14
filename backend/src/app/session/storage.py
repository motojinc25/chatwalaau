"""Session and folder storage helpers for file-based persistence."""

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)

FOLDER_NAME_MAX_LENGTH = 100

# Preset folder colors (UDR-0046 D2). The record stores the token KEY, never a
# raw hex value, so the rendered color stays under theme / dark-mode control on
# the frontend. "neutral" renders as the uncolored (pre-PRP-0070) look.
FOLDER_COLORS: tuple[str, ...] = (
    "neutral",
    "red",
    "orange",
    "amber",
    "green",
    "blue",
    "violet",
    "pink",
)
DEFAULT_FOLDER_COLOR = "neutral"


def normalize_folder_color(value: Any) -> str:
    """Coerce an arbitrary value to a known palette token (UDR-0046 D2/D5)."""
    if isinstance(value, str) and value in FOLDER_COLORS:
        return value
    return DEFAULT_FOLDER_COLOR


def sessions_dir() -> Path:
    """Return the configured session directory."""
    return Path(settings.sessions_dir)


def session_path(thread_id: str) -> Path:
    """Return the JSON file path for a session.

    Temporary Chat threads (``temp_`` prefix) are routed to the ``.temporary/``
    quarantine directory instead of ``.sessions/`` (CTR-0106, UDR-0052 D2/D3), so
    they never appear in the sidebar list or full-text search (both scan
    ``.sessions/`` only) and are swept by the retention policy.
    """
    # Lazy import avoids an import cycle (app.agent.temporary imports config only).
    from app.agent.temporary import is_temporary, temporary_path

    if is_temporary(thread_id):
        return temporary_path(thread_id)
    return sessions_dir() / f"{thread_id}.json"


# The session metadata index (CTR-0014 v2, UDR-0091 D2) lives INSIDE the session
# directory, so a naive ``glob("*.json")`` would pick it up and try to parse it as
# a chat. UDR-0091 D6 requires every scan of the session directory to exclude it by
# exact name -- hence the single shared iterator below, which is the only sanctioned
# way to enumerate session files.
SESSION_INDEX_FILENAME = "index.json"


def iter_session_files(base_dir: Path) -> list[Path]:
    """Enumerate the session JSON files in ``base_dir``.

    Excludes the metadata index (UDR-0091 D6). Every caller that scans a session
    directory MUST go through this helper; a bare ``glob("*.json")`` would surface
    the index as a broken chat.
    """
    if not base_dir.is_dir():
        return []
    return [path for path in base_dir.glob("*.json") if path.name != SESSION_INDEX_FILENAME]


def folders_dir() -> Path:
    """Return the folder registry directory under the session root."""
    return sessions_dir() / "folders"


def folder_index_path() -> Path:
    """Return the JSON index file path for folder records."""
    return folders_dir() / "index.json"


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON atomically using a temp file and replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def read_session_json(thread_id: str) -> dict[str, Any] | None:
    """Read a session JSON file if present."""
    path = session_path(thread_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return ensure_session_defaults(data)


def write_session_json(thread_id: str, data: dict[str, Any]) -> None:
    """Persist a session JSON file with default fields populated."""
    write_json_atomic(session_path(thread_id), ensure_session_defaults(data))


def ensure_session_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Backfill additive session fields for older files."""
    data.setdefault("folder_id", None)
    return data


def _backup_corrupt_folder_index(path: Path) -> None:
    """Move an unparseable folder index aside so it is never silently lost.

    Mirrors the PRP-0064 .env-sync backup rule (UDR-0046 D5): a whole-file
    failure is preserved as index.corrupt-<timestamp>.json before the registry
    restarts empty.
    """
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        backup = path.parent / f"index.corrupt-{stamp}.json"
        path.replace(backup)
        logger.warning("Folder registry was unparseable; backed up to %s and reset to empty", backup)
    except OSError:
        logger.warning("Folder registry was unparseable and could not be backed up: %s", path)


def _normalize_folder_records(data: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Normalize raw folder entries per field (UDR-0046 D5).

    Returns the cleaned records (sorted by ascending order) plus a flag
    indicating whether anything changed, so the caller can write the repaired
    list back once (self-heal).

    - An entry missing an ``id`` is skipped.
    - An invalid / unknown ``color`` falls back to the default palette token.
    - A missing / duplicate / non-integer ``order`` is reassigned
      deterministically (stable sort by existing order then ``updated_at`` desc,
      reindexed 0..n-1).
    """
    changed = False
    cleaned: list[dict[str, Any]] = []
    seen_orders: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            changed = True
            continue
        folder_id = str(item.get("id", ""))
        if not folder_id:
            changed = True
            continue

        color = normalize_folder_color(item.get("color"))
        if color != item.get("color"):
            changed = True

        raw_order = item.get("order")
        order: int | None = raw_order if isinstance(raw_order, int) and not isinstance(raw_order, bool) else None
        if order is None or order in seen_orders:
            order = None  # defer to deterministic reindex below
            changed = True
        else:
            seen_orders.add(order)

        cleaned.append(
            {
                "id": folder_id,
                "name": str(item.get("name", "")),
                "color": color,
                "order": order,
                "created_at": str(item.get("created_at", "")),
                "updated_at": str(item.get("updated_at", "")),
            }
        )

    # Deterministic order: entries with a valid explicit order first (ascending),
    # then entries whose order had to be dropped, by recency (updated_at desc).
    with_order = sorted((f for f in cleaned if f["order"] is not None), key=lambda f: f["order"])
    without_order = sorted((f for f in cleaned if f["order"] is None), key=lambda f: f["updated_at"], reverse=True)
    cleaned = [*with_order, *without_order]
    for index, folder in enumerate(cleaned):
        if folder["order"] != index:
            changed = True
        folder["order"] = index
    return cleaned, changed


def read_folder_index() -> list[dict[str, Any]]:
    """Read folder registry entries, normalizing and self-healing on the fly.

    Tolerant and self-healing (UDR-0046 D5): a recoverable per-field problem is
    repaired and written back once; a whole-file parse failure is backed up and
    the registry restarts empty. Never raises for a recoverable malformed file.
    """
    path = folder_index_path()
    if not path.is_file():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        _backup_corrupt_folder_index(path)
        return []

    if not isinstance(data, list):
        _backup_corrupt_folder_index(path)
        return []

    folders, changed = _normalize_folder_records(data)
    if changed:
        try:
            write_folder_index(folders)
        except OSError:
            logger.warning("Failed to write back self-healed folder registry: %s", path)
    return folders


def write_folder_index(folders: list[dict[str, Any]]) -> None:
    """Persist folder registry entries atomically."""
    write_json_atomic(folder_index_path(), folders)


def list_folder_ids() -> set[str]:
    """Return the set of currently registered folder IDs."""
    return {folder["id"] for folder in read_folder_index() if folder.get("id")}


def create_folder_record(name: str, color: str = DEFAULT_FOLDER_COLOR) -> dict[str, Any]:
    """Create and persist a new folder record appended to the end of the order."""
    now = datetime.now(UTC).isoformat()
    folders = read_folder_index()
    next_order = max((f["order"] for f in folders), default=-1) + 1
    folder = {
        "id": str(uuid.uuid4()),
        "name": name,
        "color": normalize_folder_color(color),
        "order": next_order,
        "created_at": now,
        "updated_at": now,
    }
    folders.append(folder)
    write_folder_index(folders)
    return folder


def update_folder_record(
    folder_id: str,
    name: str | None = None,
    color: str | None = None,
) -> dict[str, Any] | None:
    """Update a folder's name and/or color. Returns the record or None if absent."""
    folders = read_folder_index()
    updated: dict[str, Any] | None = None
    for folder in folders:
        if folder.get("id") != folder_id:
            continue
        if name is not None:
            folder["name"] = name
        if color is not None:
            folder["color"] = normalize_folder_color(color)
        folder["updated_at"] = datetime.now(UTC).isoformat()
        updated = folder
        break
    if updated is not None:
        write_folder_index(folders)
    return updated


def reorder_folders(folder_ids: list[str]) -> list[dict[str, Any]]:
    """Reassign folder order from an explicit id sequence (UDR-0046 D6).

    Idempotent bulk set: unknown ids are ignored; registered ids absent from
    ``folder_ids`` are appended preserving their prior relative order.
    """
    folders = read_folder_index()
    by_id = {folder["id"]: folder for folder in folders}

    ordered: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for folder_id in folder_ids:
        folder = by_id.get(folder_id)
        if folder is not None and folder_id not in consumed:
            ordered.append(folder)
            consumed.add(folder_id)
    # Append registered ids missing from the request, preserving prior order.
    ordered.extend(folder for folder in folders if folder["id"] not in consumed)

    for index, folder in enumerate(ordered):
        folder["order"] = index
    write_folder_index(ordered)
    return ordered


def touch_folder_record(folder_id: str) -> None:
    """Update a folder's updated_at timestamp when it is actively used."""
    folders = read_folder_index()
    did_change = False
    for folder in folders:
        if folder.get("id") != folder_id:
            continue
        folder["updated_at"] = datetime.now(UTC).isoformat()
        did_change = True
        break
    if did_change:
        write_folder_index(folders)
