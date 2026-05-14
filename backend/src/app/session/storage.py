"""Session and folder storage helpers for file-based persistence."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
import uuid

from app.core.config import settings

FOLDER_NAME_MAX_LENGTH = 100


def sessions_dir() -> Path:
    """Return the configured session directory."""
    return Path(settings.sessions_dir)


def session_path(thread_id: str) -> Path:
    """Return the JSON file path for a session."""
    return sessions_dir() / f"{thread_id}.json"


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


def read_folder_index() -> list[dict[str, Any]]:
    """Read folder registry entries."""
    path = folder_index_path()
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        msg = "Folder registry is malformed"
        raise ValueError(msg)
    folders: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        folders.append(
            {
                "id": str(item.get("id", "")),
                "name": str(item.get("name", "")),
                "created_at": str(item.get("created_at", "")),
                "updated_at": str(item.get("updated_at", "")),
            }
        )
    return folders


def write_folder_index(folders: list[dict[str, Any]]) -> None:
    """Persist folder registry entries atomically."""
    write_json_atomic(folder_index_path(), folders)


def list_folder_ids() -> set[str]:
    """Return the set of currently registered folder IDs."""
    return {folder["id"] for folder in read_folder_index() if folder.get("id")}


def create_folder_record(name: str) -> dict[str, Any]:
    """Create and persist a new folder record."""
    now = datetime.now(UTC).isoformat()
    folder = {
        "id": str(uuid.uuid4()),
        "name": name,
        "created_at": now,
        "updated_at": now,
    }
    folders = read_folder_index()
    folders.append(folder)
    write_folder_index(folders)
    return folder


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
