"""Session management REST API (CTR-0015).

Provides endpoints for listing, saving, forking, renaming, archiving,
pinning, and deleting sessions.
Session files are stored in the .sessions/ directory.
"""

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import shutil
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import verify_api_key
from app.core.config import settings
from app.session.storage import (
    FOLDER_NAME_MAX_LENGTH,
    create_folder_record,
    ensure_session_defaults,
    read_folder_index,
    read_session_json,
    session_path,
    sessions_dir,
    touch_folder_record,
    write_folder_index,
    write_json_atomic,
    write_session_json,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


def _sessions_dir() -> Path:
    return sessions_dir()


_IMAGE_GEN_TOOLS = frozenset({"generate_image", "edit_image"})


def _count_images(messages: list[dict[str, Any]]) -> int:
    """Count image_url content entries and generated images across all messages."""
    count = 0
    for msg in messages:
        for c in msg.get("contents", []):
            if isinstance(c, dict) and c.get("type") == "image_url":
                count += 1
        for tc in msg.get("tool_calls", []):
            if tc.get("name") not in _IMAGE_GEN_TOOLS:
                continue
            result = tc.get("result", "")
            if not isinstance(result, str):
                continue
            try:
                parsed = json.loads(result)
                count += len(parsed.get("images", []))
            except (json.JSONDecodeError, TypeError):
                pass
    return count


def _archived_dir() -> Path:
    return Path(".archived")


def _read_session_metadata(path: Path) -> dict[str, Any] | None:
    """Read session file and return metadata (without full messages)."""
    try:
        data = ensure_session_defaults(json.loads(path.read_text(encoding="utf-8")))
        return {
            "thread_id": data.get("thread_id", path.stem),
            "title": data.get("title", ""),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", ""),
            "message_count": data.get("message_count", 0),
            "image_count": data.get("image_count", 0),
            "pinned_at": data.get("pinned_at"),
            "folder_id": data.get("folder_id"),
            "source": data.get("source", "ag-ui"),
        }
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read session file: %s", path)
        return None


class InitSessionRequest(BaseModel):
    title: str = ""


@router.post("/{thread_id}/init", dependencies=[Depends(verify_api_key)])
async def init_session(thread_id: str, body: InitSessionRequest) -> dict[str, Any]:
    """Initialize an empty session file before agent processing starts.

    Creates the session JSON so it appears in the sidebar immediately.
    Idempotent: returns existing session if already present.
    """
    sessions_path = _sessions_dir()
    sessions_path.mkdir(parents=True, exist_ok=True)
    path = sessions_path / f"{thread_id}.json"

    if path.is_file():
        return {"status": "exists", "thread_id": thread_id}

    now = datetime.now(UTC).isoformat()
    data = {
        "thread_id": thread_id,
        "title": body.title[:100],
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "image_count": 0,
        "folder_id": None,
        "messages": [],
    }
    write_session_json(thread_id, data)
    logger.info("Initialized session %s", thread_id)
    return {"status": "created", "thread_id": thread_id}


class CreateFolderRequest(BaseModel):
    name: str


class AssignFolderRequest(BaseModel):
    folder_id: str | None = None


def _read_folder_records() -> list[dict[str, Any]]:
    """Read folder registry or raise a HTTP 500 on corruption."""
    try:
        return read_folder_index()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail="Failed to read folder registry") from e


def _read_session_or_404(thread_id: str) -> dict[str, Any]:
    """Read session JSON or raise HTTP errors."""
    try:
        data = read_session_json(thread_id)
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail="Failed to read session") from e
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


def _write_session_or_500(thread_id: str, data: dict[str, Any]) -> None:
    """Persist session JSON or raise HTTP errors."""
    try:
        write_session_json(thread_id, data)
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to write session") from e


@router.get("/folders")
async def list_folders() -> list[dict[str, Any]]:
    """List all folder records."""
    folders = _read_folder_records()
    folders.sort(key=lambda folder: folder.get("updated_at", ""), reverse=True)
    return folders


@router.post("/folders", dependencies=[Depends(verify_api_key)])
async def create_folder(body: CreateFolderRequest) -> dict[str, Any]:
    """Create a new folder record."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty")
    if len(name) > FOLDER_NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Folder name must be {FOLDER_NAME_MAX_LENGTH} characters or fewer",
        )

    try:
        folder = create_folder_record(name)
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to create folder") from e
    logger.info("Created folder %s", folder["id"])
    return folder


@router.delete("/folders/{folder_id}", dependencies=[Depends(verify_api_key)])
async def delete_folder(folder_id: str) -> dict[str, Any]:
    """Delete a folder and unassign all linked sessions."""
    folders = _read_folder_records()
    if folder_id not in {folder["id"] for folder in folders}:
        raise HTTPException(status_code=404, detail="Folder not found")

    for base_dir in (_sessions_dir(), _archived_dir()):
        if not base_dir.is_dir():
            continue
        for file in base_dir.glob("*.json"):
            try:
                data = ensure_session_defaults(json.loads(file.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                raise HTTPException(status_code=500, detail="Failed to read session") from e
            if data.get("folder_id") != folder_id:
                continue
            data["folder_id"] = None
            data["updated_at"] = datetime.now(UTC).isoformat()
            try:
                write_json_atomic(file, data)
            except OSError as e:
                raise HTTPException(status_code=500, detail="Failed to write session") from e

    try:
        write_folder_index([folder for folder in folders if folder.get("id") != folder_id])
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to delete folder") from e

    logger.info("Deleted folder %s", folder_id)
    return {"status": "deleted", "folder_id": folder_id}


@router.get("")
async def list_sessions() -> list[dict[str, Any]]:
    """List all sessions sorted by updated_at descending."""
    sessions_path = _sessions_dir()
    if not sessions_path.is_dir():
        return []

    sessions = []
    for file in sessions_path.glob("*.json"):
        meta = _read_session_metadata(file)
        if meta:
            sessions.append(meta)

    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions


@router.get("/search")
async def search_sessions(q: str = "") -> list[dict[str, Any]]:
    """Search sessions by message content (full-text) and title.

    Returns matching sessions with a snippet of the first matching content.
    Must be registered before /{thread_id} to avoid path parameter capture.
    """
    sessions_path = _sessions_dir()
    if not sessions_path.is_dir() or not q.strip():
        return []

    lower_q = q.strip().lower()
    results: list[dict[str, Any]] = []

    for file in sessions_path.glob("*.json"):
        try:
            data = ensure_session_defaults(json.loads(file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue

        snippet = ""
        # Search in title first
        title = data.get("title", "")
        if lower_q in title.lower():
            snippet = title[:120]

        # Search in message contents
        if not snippet:
            for msg in data.get("messages", []):
                for c in msg.get("contents", []):
                    if not isinstance(c, dict):
                        continue
                    text = c.get("text", "")
                    if not isinstance(text, str):
                        continue
                    lower_text = text.lower()
                    pos = lower_text.find(lower_q)
                    if pos != -1:
                        start = max(0, pos - 40)
                        end = min(len(text), pos + len(q) + 80)
                        snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
                        break
                if snippet:
                    break

        if snippet:
            results.append(
                {
                    "thread_id": data.get("thread_id", file.stem),
                    "title": title,
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "message_count": data.get("message_count", 0),
                    "image_count": data.get("image_count", 0),
                    "pinned_at": data.get("pinned_at"),
                    "folder_id": data.get("folder_id"),
                    "snippet": snippet,
                }
            )

    results.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return results


@router.get("/{thread_id}")
async def get_session(thread_id: str) -> dict[str, Any]:
    """Get a session with its messages."""
    return _read_session_or_404(thread_id)


@router.patch("/{thread_id}/folder", dependencies=[Depends(verify_api_key)])
async def assign_session_folder(thread_id: str, body: AssignFolderRequest) -> dict[str, Any]:
    """Assign or unassign a session to a folder."""
    data = _read_session_or_404(thread_id)

    if body.folder_id is not None and body.folder_id not in {folder["id"] for folder in _read_folder_records()}:
        raise HTTPException(status_code=400, detail="Folder not found")

    data["folder_id"] = body.folder_id
    data["updated_at"] = datetime.now(UTC).isoformat()
    _write_session_or_500(thread_id, data)

    if body.folder_id:
        try:
            touch_folder_record(body.folder_id)
        except OSError as e:
            raise HTTPException(status_code=500, detail="Failed to update folder") from e

    logger.info("Assigned session %s to folder %s", thread_id, body.folder_id)
    return {"status": "updated", "thread_id": thread_id, "folder_id": data["folder_id"]}


class ReasoningItem(BaseModel):
    id: str | None = None
    content: str


class ActivityLogItem(BaseModel):
    type: str
    id: str


class ImageItem(BaseModel):
    uri: str
    media_type: str


class ToolCallItem(BaseModel):
    id: str
    name: str
    status: str
    args: str | None = None
    result: str | None = None


class UsageItem(BaseModel):
    input_token_count: int | None = None
    output_token_count: int | None = None
    total_token_count: int | None = None


class SaveMessageItem(BaseModel):
    role: str
    content: str
    reasoning: list[ReasoningItem] | None = None
    images: list[ImageItem] | None = None
    tool_calls: list[ToolCallItem] | None = None
    activity_log: list[ActivityLogItem] | None = None
    usage: UsageItem | None = None


class SaveMessagesRequest(BaseModel):
    messages: list[SaveMessageItem]


def _to_maf_message_dict(msg: SaveMessageItem) -> dict[str, Any]:
    """Convert a simple role/content pair to MAF Message dict format.

    Content types use MAF's ContentType literals: ``text`` for text
    content and ``text_reasoning`` for reasoning blocks, so that
    ``Message.from_dict()`` can restore them correctly when the session
    is loaded back by the history provider.
    """
    contents: list[dict[str, Any]] = []
    if msg.reasoning:
        contents.extend(
            {"type": "text_reasoning", "text": r.content, **({"id": r.id} if r.id else {})} for r in msg.reasoning
        )
    contents.append({"type": "text", "text": msg.content})
    if msg.images:
        contents.extend({"type": "image_url", "uri": img.uri, "media_type": img.media_type} for img in msg.images)
    result: dict[str, Any] = {
        "type": "chat_message",
        "role": msg.role,
        "contents": contents,
    }
    if msg.tool_calls:
        result["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
    if msg.activity_log:
        result["activity_log"] = [al.model_dump() for al in msg.activity_log]
    if msg.usage:
        result["usage"] = msg.usage.model_dump(exclude_none=True)
    return result


@router.post("/{thread_id}/messages", dependencies=[Depends(verify_api_key)])
async def save_messages(thread_id: str, body: SaveMessagesRequest) -> dict[str, Any]:
    """Save new messages to a session file.

    Called by the frontend after an AG-UI stream completes.
    AG-UI bypasses MAF's ResponseStream finalizers, so after_run
    on context providers is never called. This endpoint provides
    an alternative persistence path.
    """
    sessions_path = _sessions_dir()
    sessions_path.mkdir(parents=True, exist_ok=True)
    path = session_path(thread_id)

    new_message_dicts = [_to_maf_message_dict(m) for m in body.messages]
    now = datetime.now(UTC).isoformat()

    if path.is_file():
        try:
            data = ensure_session_defaults(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            data = None
    else:
        data = None

    if data:
        existing = data.get("messages", [])
        existing.extend(new_message_dicts)
        data["messages"] = existing
        data["updated_at"] = now
        data["message_count"] = len(existing)
        data["image_count"] = _count_images(existing)
    else:
        data = {
            "thread_id": thread_id,
            "title": body.messages[0].content[:100] if body.messages else "",
            "created_at": now,
            "updated_at": now,
            "message_count": len(new_message_dicts),
            "image_count": _count_images(new_message_dicts),
            "folder_id": None,
            "messages": new_message_dicts,
        }

    if not data.get("title") and new_message_dicts:
        for m in new_message_dicts:
            if m["role"] == "user":
                data["title"] = m["contents"][0]["text"][:100]
                break

    _write_session_or_500(thread_id, data)
    logger.info("Saved %d messages to session %s via API", len(new_message_dicts), thread_id)
    return {"status": "saved", "thread_id": thread_id, "message_count": data["message_count"]}


class TruncateRequest(BaseModel):
    after_index: int
    delete_from: int


@router.post("/{thread_id}/truncate", dependencies=[Depends(verify_api_key)])
async def truncate_session(thread_id: str, body: TruncateRequest) -> dict[str, Any]:
    """Truncate session messages from a given index onward.

    Used for message edit/regenerate: removes messages from
    delete_from onward so the frontend can re-request.
    """
    data = _read_session_or_404(thread_id)

    messages = data.get("messages", [])
    if body.delete_from < len(messages):
        data["messages"] = messages[: body.delete_from]
        data["message_count"] = len(data["messages"])
        data["image_count"] = _count_images(data["messages"])
        data["updated_at"] = datetime.now(UTC).isoformat()
        _write_session_or_500(thread_id, data)
        logger.info("Truncated session %s from index %d", thread_id, body.delete_from)

    return {"status": "truncated", "thread_id": thread_id, "message_count": data.get("message_count", 0)}


@router.delete("/{thread_id}/messages/{index}", dependencies=[Depends(verify_api_key)])
async def delete_message(thread_id: str, index: int) -> dict[str, Any]:
    """Delete a single message at the given index from a session."""
    data = _read_session_or_404(thread_id)

    messages = data.get("messages", [])
    if index < 0 or index >= len(messages):
        raise HTTPException(status_code=400, detail="Index out of range")

    messages.pop(index)
    data["messages"] = messages
    data["message_count"] = len(messages)
    data["image_count"] = _count_images(messages)
    data["updated_at"] = datetime.now(UTC).isoformat()
    _write_session_or_500(thread_id, data)
    logger.info("Deleted message at index %d from session %s", index, thread_id)

    return {"status": "deleted", "thread_id": thread_id, "message_count": len(messages)}


class ForkRequest(BaseModel):
    up_to_index: int


@router.post("/{thread_id}/fork", dependencies=[Depends(verify_api_key)])
async def fork_session(thread_id: str, body: ForkRequest) -> dict[str, Any]:
    """Fork a session up to a given message index.

    Creates a new session file containing messages[0:up_to_index+1]
    from the source session. Used by "Branch in new chat" feature.
    """
    data = _read_session_or_404(thread_id)

    messages = data.get("messages", [])
    forked_messages = messages[: body.up_to_index + 1]

    new_thread_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    # Derive title from first user message in forked messages
    title = ""
    for m in forked_messages:
        if m.get("role") == "user":
            contents = m.get("contents", [])
            if contents and isinstance(contents[0], dict):
                title = contents[0].get("text", "")[:100]
            break

    new_data = {
        "thread_id": new_thread_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "message_count": len(forked_messages),
        "image_count": _count_images(forked_messages),
        "folder_id": data.get("folder_id"),
        "messages": forked_messages,
    }

    sessions_path = _sessions_dir()
    sessions_path.mkdir(parents=True, exist_ok=True)
    _write_session_or_500(new_thread_id, new_data)
    logger.info("Forked session %s -> %s (up to index %d)", thread_id, new_thread_id, body.up_to_index)

    return {
        "status": "forked",
        "new_thread_id": new_thread_id,
        "message_count": len(forked_messages),
    }


class RenameRequest(BaseModel):
    title: str


@router.patch("/{thread_id}/rename", dependencies=[Depends(verify_api_key)])
async def rename_session(thread_id: str, body: RenameRequest) -> dict[str, Any]:
    """Rename a session title."""
    data = _read_session_or_404(thread_id)

    data["title"] = body.title.strip()[:100]
    data["updated_at"] = datetime.now(UTC).isoformat()
    _write_session_or_500(thread_id, data)
    logger.info("Renamed session %s to '%s'", thread_id, data["title"])

    return {"status": "renamed", "thread_id": thread_id, "title": data["title"]}


@router.post("/{thread_id}/archive", dependencies=[Depends(verify_api_key)])
async def archive_session(thread_id: str) -> dict[str, str]:
    """Archive a session by moving it from .sessions/ to .archived/."""
    path = session_path(thread_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        archived_path = _archived_dir()
        archived_path.mkdir(parents=True, exist_ok=True)
        dest = archived_path / f"{thread_id}.json"
        path.rename(dest)

        # Move uploaded files to .archived_uploads/ if they exist
        upload_dir = Path(settings.upload_dir) / thread_id
        if upload_dir.is_dir():
            archived_uploads = Path(settings.upload_dir).parent / ".archived_uploads" / thread_id
            archived_uploads.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(upload_dir), str(archived_uploads))
            logger.info("Archived upload directory: %s -> %s", upload_dir, archived_uploads)

        logger.info("Archived session: %s", thread_id)
        return {"status": "archived", "thread_id": thread_id}
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to archive session") from e


class ContinuationTokenRequest(BaseModel):
    continuation_token: dict[str, Any] | None = None


@router.patch("/{thread_id}/continuation-token", dependencies=[Depends(verify_api_key)])
async def update_continuation_token(thread_id: str, body: ContinuationTokenRequest) -> dict[str, Any]:
    """Update continuation_token for background response resumption (CTR-0045, PRP-0025)."""
    data = _read_session_or_404(thread_id)

    data["continuation_token"] = body.continuation_token
    data["updated_at"] = datetime.now(UTC).isoformat()
    _write_session_or_500(thread_id, data)
    logger.info(
        "Updated continuation_token for session %s: %s", thread_id, "set" if body.continuation_token else "cleared"
    )

    return {"status": "updated", "thread_id": thread_id}


class PinRequest(BaseModel):
    pinned: bool


@router.patch("/{thread_id}/pin", dependencies=[Depends(verify_api_key)])
async def pin_session(thread_id: str, body: PinRequest) -> dict[str, Any]:
    """Pin or unpin a session."""
    data = _read_session_or_404(thread_id)

    if body.pinned:
        data["pinned_at"] = datetime.now(UTC).isoformat()
    else:
        data["pinned_at"] = None

    _write_session_or_500(thread_id, data)
    logger.info("Pin session %s: pinned=%s", thread_id, body.pinned)

    return {"status": "pinned" if body.pinned else "unpinned", "thread_id": thread_id, "pinned_at": data["pinned_at"]}


@router.delete("/{thread_id}", dependencies=[Depends(verify_api_key)])
async def delete_session(thread_id: str) -> dict[str, str]:
    """Delete a session file and its uploaded files."""
    path = session_path(thread_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        path.unlink()

        # Cascade delete uploaded files for this session
        upload_dir = Path(settings.upload_dir) / thread_id
        if upload_dir.is_dir():
            shutil.rmtree(upload_dir, ignore_errors=True)
            logger.info("Deleted upload directory: %s", upload_dir)

        logger.info("Deleted session: %s", thread_id)
        return {"status": "deleted", "thread_id": thread_id}
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to delete session") from e
