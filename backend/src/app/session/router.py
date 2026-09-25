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
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.auth import verify_api_key
from app.core.config import settings
from app.image_gen.names import IMAGE_RESULT_TOOL_NAMES
from app.session import index_store
from app.session.bundle import BundleValidationError, build_export_bundle, import_bundle
from app.session.storage import (
    DEFAULT_FOLDER_COLOR,
    FOLDER_COLORS,
    FOLDER_NAME_MAX_LENGTH,
    SessionCorruptError,
    SessionMutator,
    SessionUnavailableError,
    create_folder_record,
    create_session_json,
    iter_session_files,
    list_folder_ids,
    read_folder_index,
    read_session_file,
    read_session_json,
    reorder_folders,
    session_path,
    sessions_dir,
    touch_folder_record,
    update_folder_record,
    update_session_file,
    update_session_json,
    write_folder_index,
)
from app.session.upload_refs import copy_referenced_uploads, rewrite_upload_refs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


def _sessions_dir() -> Path:
    return sessions_dir()


# Current AND legacy names: stored history keeps the name it was written with
# (PRP-0187 / UDR-0169 D6).
_IMAGE_GEN_TOOLS = IMAGE_RESULT_TOOL_NAMES


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
    """Return the archive directory: a SIBLING of the configured session directory.

    v0.117.4: this was a hardcoded, CWD-relative ``Path(".archived")`` while the
    sessions themselves live under ``settings.sessions_dir`` (CTR-0006
    SESSIONS_DIR). With the DEFAULT ``SESSIONS_DIR=".sessions"`` the two resolve
    identically -- ``Path(".sessions").parent / ".archived"`` IS ``.archived`` --
    which is why archiving worked on a developer box and failed only on a
    deployment that sets SESSIONS_DIR. There it broke twice over:

    - the archive landed outside the session store entirely (relative to the
      process CWD), so archived chats were separated from the data they belong to
      and were not carried by a volume backup; and
    - when the session store is a MOUNTED volume, the destination is on a
      different filesystem than the source, so the ``os.rename`` that used to
      perform the move failed with ``EXDEV`` / ``[WinError 17]`` -> HTTP 500.

    Deriving it from ``sessions_dir()`` keeps the default layout byte-identical
    (so an existing ``.archived/`` is still found) while making the archive
    follow SESSIONS_DIR onto the same filesystem wherever it is configured.
    """
    return sessions_dir().parent / ".archived"


# The metadata projection moved to app.session.index_store (CTR-0014 v2), which
# owns both the parse and the mtime-reconciled cache in front of it.


class InitSessionRequest(BaseModel):
    title: str = ""


@router.post("/{thread_id}/init", dependencies=[Depends(verify_api_key)])
async def init_session(thread_id: str, body: InitSessionRequest) -> dict[str, Any]:
    """Initialize an empty session file before agent processing starts.

    Creates the session JSON so it appears in the sidebar immediately.
    Idempotent: returns existing session if already present.
    """
    # Route via session_path so a temp_ thread id (Temporary Chat, CTR-0106) lands
    # in the .temporary/ quarantine and never the user-listed .sessions/ dir. The
    # SPA does not call init for temporary chats, but route defensively.
    from app.agent.temporary import is_temporary

    path = session_path(thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
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
    # User Preference Memory (PRP-0075, CTR-0105, UDR-0051 D3): capture the
    # FROZEN User Profile snapshot at session start. It is reused for every turn
    # of this session (and on reload); the live .agent/USER.md may change later
    # but this session's Memory Block does not. Temporary Chat (CTR-0106,
    # UDR-0052 D7) is de-personalized -- no snapshot is captured.
    if settings.user_profile_enabled and not is_temporary(thread_id):
        from app.agent.user_memory import current_user_profile_block

        data["user_profile_snapshot"] = current_user_profile_block()
    # Auto Session Title (PRP-0077, CTR-0109, CTR-0110): when LLM titling is on,
    # mark the session pending so the sidebar shows a spinner until the
    # background task finalizes the title (cleared via the CTR-0110 push, or on
    # the next list refresh). Temporary chats are never auto-titled.
    if settings.session_title_mode == "llm" and not is_temporary(thread_id):
        data["auto_title_pending"] = True
    # Never replaces a record (UDR-0156 D1): a concurrent creator that won the race
    # keeps its file, and this request reports "exists" exactly as above.
    try:
        created = create_session_json(thread_id, data)
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to write session") from e
    if not created:
        return {"status": "exists", "thread_id": thread_id}
    logger.info("Initialized session %s", thread_id)
    return {"status": "created", "thread_id": thread_id}


class CreateFolderRequest(BaseModel):
    name: str
    color: str = DEFAULT_FOLDER_COLOR


class FolderUpdateRequest(BaseModel):
    name: str | None = None
    color: str | None = None


class FolderOrderRequest(BaseModel):
    folder_ids: list[str]


class AssignFolderRequest(BaseModel):
    folder_id: str | None = None


def _read_folder_records() -> list[dict[str, Any]]:
    """Read the folder registry.

    The reader self-heals recoverable corruption (UDR-0046 D5), so a malformed
    registry no longer raises -- it returns the normalized (and repaired) list.
    """
    return read_folder_index()


def _unavailable(exc: SessionUnavailableError) -> HTTPException:
    """Map an unreadable session to an HTTP error (PRP-0174, UDR-0156 D2).

    A corrupt file is a 500 the operator must look at; anything else is a transient
    conflict the client may retry. The file is left untouched in both cases.
    """
    if isinstance(exc, SessionCorruptError):
        return HTTPException(status_code=500, detail="Failed to read session")
    return HTTPException(
        status_code=503,
        detail="Session is temporarily unavailable; retry.",
        headers={"Retry-After": "1"},
    )


def _read_session_or_404(thread_id: str) -> dict[str, Any]:
    """Read session JSON or raise HTTP errors."""
    try:
        data = read_session_json(thread_id)
    except SessionUnavailableError as e:
        raise _unavailable(e) from e
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to read session") from e
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


def _update_session_or_error(thread_id: str, mutate: SessionMutator) -> dict[str, Any]:
    """Serialised read-modify-write of an EXISTING session (UDR-0156 D1/D2).

    404 when absent, 503 / 500 when unreadable (nothing written), 500 when the write
    itself fails.
    """
    try:
        data = update_session_json(thread_id, mutate)
    except SessionUnavailableError as e:
        raise _unavailable(e) from e
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to write session") from e
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


@router.get("/folders")
async def list_folders() -> list[dict[str, Any]]:
    """List folder records, sorted by manual order ascending (CTR-0015 v1.12).

    Each record carries an additive ``session_count`` (PRP-0112 / v0.106.2): how many
    chats the folder holds, ACROSS THE WHOLE STORE.

    The SPA cannot compute this itself any more. Since PRP-0112 a folder's sessions are
    fetched only when it is expanded (UDR-0091 D4), so a client-side
    ``sessions.filter(s => s.folder_id === folder.id)`` counts only what happens to be
    loaded -- zero for every collapsed folder. The count therefore has to come from the
    server, which is the only participant that sees every session.

    It is cheap: the CTR-0014 v2 metadata index already carries ``folder_id`` for every
    session, so this is a pass over cached metadata and opens no session file.
    """
    folders = _read_folder_records()
    folders.sort(key=lambda folder: folder.get("order", 0))
    return await _with_session_counts(folders)


async def _with_session_counts(folders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the derived ``session_count`` to folder records, in place.

    EVERY endpoint that returns a folder must go through this, not just the list
    (v0.118.3 fix). ``session_count`` is derived state the folder record itself does
    not carry, so an endpoint that returns the raw record omits it -- and the SPA
    normalizes an absent count to 0. The result was a folder whose chat count fell to
    zero the moment it was renamed, recolored, or dragged into a new position, while
    the chats inside it were untouched. Nothing errored; the number was simply wrong
    until the next full list refresh.
    """
    counts: dict[str, int] = {}
    for meta in await index_store.list_session_metadata():
        folder_id = meta.get("folder_id")
        if folder_id:
            counts[folder_id] = counts.get(folder_id, 0) + 1
    for folder in folders:
        folder["session_count"] = counts.get(folder["id"], 0)
    return folders


def _validate_folder_name(name: str) -> str:
    """Trim and validate a folder name, raising HTTP 400 on violations."""
    trimmed = name.strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty")
    if len(trimmed) > FOLDER_NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Folder name must be {FOLDER_NAME_MAX_LENGTH} characters or fewer",
        )
    return trimmed


def _validate_folder_color(color: str) -> None:
    """Reject colors outside the preset palette (UDR-0046 D2)."""
    if color not in FOLDER_COLORS:
        raise HTTPException(
            status_code=400,
            detail=f"Folder color must be one of: {', '.join(FOLDER_COLORS)}",
        )


@router.post("/folders", dependencies=[Depends(verify_api_key)])
async def create_folder(body: CreateFolderRequest) -> dict[str, Any]:
    """Create a new folder record."""
    name = _validate_folder_name(body.name)
    _validate_folder_color(body.color)

    try:
        folder = create_folder_record(name, body.color)
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to create folder") from e
    logger.info("Created folder %s", folder["id"])
    return (await _with_session_counts([folder]))[0]


@router.put("/folders/order", dependencies=[Depends(verify_api_key)])
async def reorder_folder_records(body: FolderOrderRequest) -> list[dict[str, Any]]:
    """Reassign folder order from an explicit id sequence (CTR-0015 v1.12)."""
    try:
        folders = reorder_folders(body.folder_ids)
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to reorder folders") from e
    logger.info("Reordered %d folders", len(folders))
    return await _with_session_counts(folders)


@router.patch("/folders/{folder_id}", dependencies=[Depends(verify_api_key)])
async def update_folder(folder_id: str, body: FolderUpdateRequest) -> dict[str, Any]:
    """Update a folder's name and/or color (CTR-0015 v1.12)."""
    name = _validate_folder_name(body.name) if body.name is not None else None
    if body.color is not None:
        _validate_folder_color(body.color)

    try:
        folder = update_folder_record(folder_id, name=name, color=body.color)
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to update folder") from e
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    logger.info("Updated folder %s", folder_id)
    return (await _with_session_counts([folder]))[0]


@router.delete("/folders/{folder_id}", dependencies=[Depends(verify_api_key)])
async def delete_folder(folder_id: str) -> dict[str, Any]:
    """Delete a folder and unassign all linked sessions."""
    folders = _read_folder_records()
    if folder_id not in {folder["id"] for folder in folders}:
        raise HTTPException(status_code=404, detail="Folder not found")

    def unassign(data: dict[str, Any]) -> bool:
        if data.get("folder_id") != folder_id:
            return False
        data["folder_id"] = None
        data["updated_at"] = datetime.now(UTC).isoformat()
        return True

    for base_dir in (_sessions_dir(), _archived_dir()):
        # iter_session_files excludes the metadata index (UDR-0091 D6).
        for file in iter_session_files(base_dir):
            try:
                update_session_file(file, unassign)
            except SessionUnavailableError as e:
                raise _unavailable(e) from e
            except OSError as e:
                raise HTTPException(status_code=500, detail="Failed to write session") from e

    try:
        write_folder_index([folder for folder in folders if folder.get("id") != folder_id])
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to delete folder") from e

    logger.info("Deleted folder %s", folder_id)
    return {"status": "deleted", "folder_id": folder_id}


#: ``folder_id`` value selecting the sessions that belong to NO folder. A bare
#: ``folder_id=`` cannot express this (it is indistinguishable from "not supplied"),
#: hence an explicit sentinel.
ROOT_FOLDER_SENTINEL = "__root__"


def sort_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order sessions: pinned first (newest pin first), then by updated_at desc.

    This order is a CONTRACT (CTR-0015, UDR-0091 D1), not an implementation detail.
    It used to be applied client-side, which only worked because the client held
    every session; a paginated client cannot sort a page it does not have, so the
    responsibility moved to the server -- the only participant that sees them all.
    """
    pinned = sorted(
        (s for s in sessions if s.get("pinned_at")),
        key=lambda s: s.get("pinned_at") or "",
        reverse=True,
    )
    unpinned = sorted(
        (s for s in sessions if not s.get("pinned_at")),
        key=lambda s: s.get("updated_at") or "",
        reverse=True,
    )
    return [*pinned, *unpinned]


@router.get("")
async def list_sessions(
    response: Response,
    limit: int | None = None,
    offset: int = 0,
    folder_id: str | None = None,
) -> list[dict[str, Any]]:
    """List sessions, pinned first then newest first (UDR-0091 D1).

    ``limit`` / ``offset`` / ``folder_id`` are all OPTIONAL and ADDITIVE (UDR-0091
    D3): with none supplied this returns every session as a bare JSON array, byte
    -for-byte the pre-PRP-0112 shape, so the CLI (CTR-0082) and any operator script
    keep working. The response stays a bare array -- the total count travels in the
    ``X-Total-Count`` header rather than in an envelope that would break every
    existing consumer.

    ``folder_id=__root__`` selects the sessions that belong to no folder (the
    sidebar's paginated "Chats" section); ``folder_id=<id>`` selects one folder's
    sessions, which are fetched complete and never paginated (UDR-0091 D4).
    """
    sessions = await index_store.list_session_metadata()

    if folder_id == ROOT_FOLDER_SENTINEL:
        known = list_folder_ids()
        sessions = [s for s in sessions if not s.get("folder_id") or s.get("folder_id") not in known]
    elif folder_id is not None:
        sessions = [s for s in sessions if s.get("folder_id") == folder_id]

    sessions = sort_sessions(sessions)

    # Total BEFORE slicing, so the client knows whether more pages exist.
    response.headers["X-Total-Count"] = str(len(sessions))

    if limit is None:
        return sessions

    start = max(0, offset)
    return sessions[start : start + max(0, limit)]


# PRP-0173 (UDR-0155 D6): the Usage Dashboard names the chats its ledger rows point at,
# and checks they still exist before opening one. Bounded so one request cannot ask
# about the whole ledger.
LOOKUP_MAX_IDS = 200


@router.get("/lookup", dependencies=[Depends(verify_api_key)])
async def lookup_sessions(ids: str = "") -> dict[str, Any]:
    """Resolve thread ids to titles, and say which no longer exist (CTR-0015, PRP-0173).

    Reads the session INDEX only -- no session file and no message is opened. Declared
    before ``/{thread_id}`` so ``lookup`` is never taken for a thread id. A Temporary
    Chat is not in the index, so its id is reported as missing (the ledger records
    those with a null id anyway).
    """
    wanted = list(dict.fromkeys(part.strip() for part in ids.split(",") if part.strip()))
    if len(wanted) > LOOKUP_MAX_IDS:
        raise HTTPException(status_code=400, detail=f"at most {LOOKUP_MAX_IDS} ids per request")
    if not wanted:
        return {"found": {}, "missing": []}

    index = {meta.get("thread_id"): meta for meta in await index_store.list_session_metadata()}
    found: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for thread_id in wanted:
        meta = index.get(thread_id)
        if meta is None:
            missing.append(thread_id)
            continue
        found[thread_id] = {
            "title": meta.get("title", ""),
            "updated_at": meta.get("updated_at", ""),
            "folder_id": meta.get("folder_id"),
        }
    return {"found": found, "missing": missing}


@router.get("/search")
async def search_sessions(q: str = "") -> list[dict[str, Any]]:
    """Search sessions by message content (full-text) and title.

    Returns matching sessions with a snippet of the first matching content.
    Must be registered before /{thread_id} to avoid path parameter capture.
    """
    if not q.strip():
        return []

    lower_q = q.strip().lower()
    results: list[dict[str, Any]] = []

    # Search stays a full scan (UDR-0091 D7): it must read message bodies by
    # definition, so the metadata index cannot serve it. It only needs the shared
    # iterator so the index file itself is not searched as if it were a chat.
    for file in iter_session_files(_sessions_dir()):
        try:
            data = read_session_file(file)
        except OSError:
            continue
        if data is None:
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


@router.post("/import", dependencies=[Depends(verify_api_key)])
async def import_session(file: UploadFile) -> dict[str, Any]:
    """Import a chat from a ZIP bundle as a brand-new session (CTR-0015 v1.15).

    The bundle is untrusted input (UDR-0062 D5): size / entry caps, zip-slip
    rejection, an entry allowlist, and manifest + session schema validation all
    run before anything is written. Structural / security violations raise a 400
    with a human-readable ``detail``; per-upload issues are non-fatal -- an
    unsupported / oversized / malformed attachment is skipped and reported in the
    response ``warnings`` list rather than failing the whole import (CTR-0015
    v1.16). A NEW thread id is always allocated (non-destructive, UDR-0062 D3)
    and the chat is de-personalized (UDR-0062 D4). Must be registered before
    /{thread_id} so "import" is never captured as a path parameter.
    """
    zip_bytes = await file.read()
    try:
        return import_bundle(zip_bytes)
    except BundleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{thread_id}")
async def get_session(thread_id: str) -> dict[str, Any]:
    """Get a session with its messages."""
    return _read_session_or_404(thread_id)


@router.get("/{thread_id}/export")
async def export_session(thread_id: str) -> Response:
    """Export a chat as a self-contained ZIP bundle (CTR-0015 v1.15).

    Read-only GET following the existing GET-session convention (loopback
    friendly, not behind the write gate). The bundle carries the session JSON
    plus its whole upload directory so images round-trip across instances.
    """
    try:
        zip_bytes, filename, filename_utf8 = build_export_bundle(thread_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except SessionUnavailableError as exc:
        raise _unavailable(exc) from exc
    # `filename` is latin-1-safe (header-encodable); `filename*` carries the full
    # possibly-non-ASCII name per RFC 5987 so browsers show the real title.
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename_utf8)}"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )


@router.patch("/{thread_id}/folder", dependencies=[Depends(verify_api_key)])
async def assign_session_folder(thread_id: str, body: AssignFolderRequest) -> dict[str, Any]:
    """Assign or unassign a session to a folder."""
    _read_session_or_404(thread_id)

    if body.folder_id is not None and body.folder_id not in {folder["id"] for folder in _read_folder_records()}:
        raise HTTPException(status_code=400, detail="Folder not found")

    def assign(data: dict[str, Any]) -> None:
        data["folder_id"] = body.folder_id
        data["updated_at"] = datetime.now(UTC).isoformat()

    data = _update_session_or_error(thread_id, assign)

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
    # Persisted so the assistant action bar restores them on reload (PRP-0071,
    # CTR-0030 / CTR-0014). Without these, model_dump dropped the model and
    # reasoning effort and past chats showed no model / reasoning label.
    max_context_tokens: int | None = None
    model: str | None = None
    reasoning: str | None = None
    # Text verbosity (PRP-0081) and structured-output state (PRP-0082, CTR-0014
    # v1.12, UDR-0058 D9). Persisted so a reloaded chat restores the verbosity
    # label and re-renders a structured (JSON) answer as a code block instead of
    # plain Markdown. Absent on legacy messages.
    verbosity: str | None = None
    structured: bool | None = None
    output_status: dict[str, Any] | None = None
    # Run-target that produced the turn -- the Built-in / Prompt agent name, or the
    # workflow name (v0.112.2). Persisted for the same reason as `model` above: without
    # it, model_dump dropped the label and a reloaded chat could not show WHICH agent or
    # workflow answered. Absent on legacy messages.
    run_target: str | None = None
    # Declarative workflow run state (v0.117.1; the completion marker dates to v0.115.1).
    # These MUST be declared: UsageItem is a strict model, so an undeclared key is dropped
    # at parse time -- which is exactly why the v0.115.1 `workflow_completed` marker never
    # actually reached disk and a silent workflow turn reloaded as an empty bubble.
    # `workflow_completed` is the compact legacy marker; `workflow_run` is the full run
    # (identity + every step's final state) the indicator and the diagram are rebuilt from.
    # Per-action payload logs are deliberately NOT persisted -- unbounded in aggregate.
    workflow_completed: dict[str, Any] | None = None
    workflow_run: dict[str, Any] | None = None
    # Two-axis token detail (PRP-0157) and the Declarative Workflow breakdown (PRP-0170).
    # Declared for the same reason as the keys above: without them a reload dropped
    # `turn` (the detail dialog stopped opening), `context_base_tokens` (the indicator fell
    # back to input + output) and `workflow_nodes` (the indicator could no longer add a
    # workflow reply to the session estimate, so a reloaded chat showed only one turn).
    context_base_tokens: int | None = None
    turn: dict[str, Any] | None = None
    workflow_nodes: list[dict[str, Any]] | None = None
    # End-of-turn harness progress RECORD (PRP-0181, CTR-0219, UDR-0163 D5): the last
    # harness_progress value of the turn -- mode, iteration n of N, end state and the
    # bounded Todo list -- so a reloaded chat still shows the indicator and the dialog.
    # Declared for the same reason as workflow_run: UsageItem is strict and would drop
    # it. It is a record of that turn only; nothing feeds it back into the agent.
    harness_run: dict[str, Any] | None = None


class SaveMessageItem(BaseModel):
    # The frontend message id (crypto.randomUUID). Persisted as `message_id` so it
    # survives reload -- the loader (useSession) restores it as the ChatMessage id,
    # keeping per-message identity STABLE across reloads. Required for any feature
    # keyed by message id across sessions, e.g. the Agent Memory per-turn like state
    # (CTR-0165 / CTR-0164 `memory_liked`, keyed by the assistant message id).
    id: str | None = None
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
    # Imported lazily, NOT at module scope. `app.session.provider` pulls in
    # agent_framework, and importing it as a side effect of importing this router
    # changed module import ORDER across the app -- which is enough to shift which
    # settings singleton other modules bind and made an unrelated session test
    # intermittently fail. The router is imported at app startup; this function is
    # not on a hot path.
    from app.session.provider import MESSAGE_TYPE_ID

    result: dict[str, Any] = {
        # DERIVED from the installed MAF, never a literal (PRP-0126): the id was
        # renamed chat_message -> message between 1.10 and 1.13, and the literal
        # here wrote sessions that Message.from_dict() rejected on the next turn.
        "type": MESSAGE_TYPE_ID,
        "role": msg.role,
        "contents": contents,
    }
    # Persist the frontend message id so the ChatMessage id is STABLE across reload
    # (the loader restores it via `message_id`). Without it every reload minted a new
    # random id, breaking id-keyed state such as the Agent Memory per-turn like
    # (CTR-0165 `memory_liked`, keyed by the assistant message id).
    if msg.id:
        result["message_id"] = msg.id
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

    PRP-0174 / UDR-0156:
    - D2: an existing file that cannot be read is NEVER replaced. It used to be
      treated as "no session", and the fresh record written in its place erased the
      whole history and moved the chat out of its folder. Now: 503, nothing written.
    - D4: idempotent by ``message_id``. A message whose id is already stored is
      skipped, so the SPA can retry a save without duplicating the turn.
    """
    new_message_dicts = [_to_maf_message_dict(m) for m in body.messages]
    now = datetime.now(UTC).isoformat()

    def create() -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "title": "",
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
            "image_count": 0,
            "folder_id": None,
            "messages": [],
        }

    def append(data: dict[str, Any]) -> bool:
        existing = data.get("messages")
        if not isinstance(existing, list):
            existing = []
        stored_ids = {m.get("message_id") for m in existing if isinstance(m, dict) and m.get("message_id")}
        fresh = [m for m in new_message_dicts if not m.get("message_id") or m["message_id"] not in stored_ids]
        if not fresh and existing:
            return False
        existing.extend(fresh)
        data["messages"] = existing
        data["updated_at"] = now
        data["message_count"] = len(existing)
        data["image_count"] = _count_images(existing)
        if not data.get("title"):
            for m in fresh:
                if m["role"] == "user":
                    data["title"] = _message_text(m)[:100]
                    break
        return True

    _sessions_dir().mkdir(parents=True, exist_ok=True)
    try:
        data = update_session_json(thread_id, append, create=create)
    except SessionUnavailableError as e:
        raise _unavailable(e) from e
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to write session") from e
    assert data is not None  # create= guarantees a record
    logger.info("Saved %d messages to session %s via API", len(new_message_dicts), thread_id)
    return {"status": "saved", "thread_id": thread_id, "message_count": data["message_count"]}


def _message_text(message: dict[str, Any]) -> str:
    """First text content of a stored message ("" when it has none)."""
    for content in message.get("contents", []):
        if isinstance(content, dict) and content.get("type") in ("text", "text_content"):
            text = content.get("text")
            if isinstance(text, str):
                return text
    return ""


class TruncateRequest(BaseModel):
    after_index: int
    delete_from: int


async def _reset_harness_conversation(thread_id: str, *, reason: str) -> None:
    """Discard the conversation's cached harness runtime session (UDR-0119 D11).

    A harness agent's history, todo list and mode live in a per-conversation MAF
    session cached in-process -- state the operator cannot see. Rewriting the
    session FILE leaves it untouched, so a rewound conversation kept the debris of
    every earlier run: RES-0003 recorded a conversation truncated to zero messages
    whose next request still carried eight orphaned tool calls from two runs that
    had already died, and was rejected because of them.

    Best-effort by design. A cleanup failure must never fail the user's edit, and a
    non-harness conversation simply has nothing cached (drops 0).
    """
    try:
        from app.agent.harness.runtime import drop_thread

        dropped = await drop_thread(thread_id, reason=reason)
        if dropped:
            logger.info(
                "Reset %d harness conversation(s) for session %s (reason=%s)",
                dropped,
                thread_id,
                reason,
            )
    except Exception:  # never fail the caller's operation
        logger.exception("Failed to reset harness conversation for session %s", thread_id)


@router.post("/{thread_id}/truncate", dependencies=[Depends(verify_api_key)])
async def truncate_session(thread_id: str, body: TruncateRequest) -> dict[str, Any]:
    """Truncate session messages from a given index onward.

    Used for message edit/regenerate: removes messages from
    delete_from onward so the frontend can re-request.
    """

    def truncate(data: dict[str, Any]) -> bool:
        messages = data.get("messages", [])
        if body.delete_from >= len(messages):
            return False
        data["messages"] = messages[: body.delete_from]
        data["message_count"] = len(data["messages"])
        data["image_count"] = _count_images(data["messages"])
        data["updated_at"] = datetime.now(UTC).isoformat()
        logger.info("Truncated session %s from index %d", thread_id, body.delete_from)
        return True

    data = _update_session_or_error(thread_id, truncate)

    # UDR-0119 D11: the visible conversation was rewound, so the invisible one must
    # agree. Unconditional -- a truncate request that changed nothing still means the
    # operator asked to rewind, and the stored messages are not index-aligned with the
    # visible ones, so there is no partial correspondence to preserve.
    await _reset_harness_conversation(thread_id, reason="truncate")

    return {"status": "truncated", "thread_id": thread_id, "message_count": data.get("message_count", 0)}


@router.delete("/{thread_id}/messages/{index}", dependencies=[Depends(verify_api_key)])
async def delete_message(thread_id: str, index: int) -> dict[str, Any]:
    """Delete a single message at the given index from a session."""
    out_of_range = False

    def remove(data: dict[str, Any]) -> bool:
        nonlocal out_of_range
        messages = data.get("messages", [])
        if index < 0 or index >= len(messages):
            out_of_range = True
            return False
        messages.pop(index)
        data["messages"] = messages
        data["message_count"] = len(messages)
        data["image_count"] = _count_images(messages)
        data["updated_at"] = datetime.now(UTC).isoformat()
        return True

    data = _update_session_or_error(thread_id, remove)
    if out_of_range:
        raise HTTPException(status_code=400, detail="Index out of range")
    logger.info("Deleted message at index %d from session %s", index, thread_id)

    # UDR-0119 D11 -- same divergence as truncate.
    await _reset_harness_conversation(thread_id, reason="message_deleted")

    return {"status": "deleted", "thread_id": thread_id, "message_count": data["message_count"]}


class ForkRequest(BaseModel):
    up_to_index: int


# Conversation state a branch carries over from its source (PRP-0174, UDR-0156 D6).
# The frozen memory snapshots belong to the conversation (UDR-0051 D3, UDR-0079 D6),
# and a branch continues that conversation. Everything else that is not listed here
# or set explicitly below -- pinned_at, auto_title_pending, memory_liked, source,
# runtime / retired fields -- belongs to the source ITEM and is not copied.
_FORK_INHERITED_FIELDS = ("user_profile_snapshot", "agent_memory_snapshot")


@router.post("/{thread_id}/fork", dependencies=[Depends(verify_api_key)])
async def fork_session(thread_id: str, body: ForkRequest) -> dict[str, Any]:
    """Fork a session up to a given message index ("Branch in new chat").

    Creates a COMPLETE, independent session holding messages[0:up_to_index+1]
    (PRP-0174 / UDR-0156 D6): it keeps the source's title and folder, inherits the
    frozen memory snapshots, claims the title slot so automatic titling never
    renames it, records ``forked_from``, and owns copies of the uploads the slice
    references -- so deleting the source no longer breaks the branch's images.
    """
    data = _read_session_or_404(thread_id)

    messages = data.get("messages", [])
    if body.up_to_index < 0 or body.up_to_index >= len(messages):
        raise HTTPException(status_code=400, detail="Index out of range")
    forked_messages = messages[: body.up_to_index + 1]

    new_thread_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    title = data.get("title") or ""
    if not title:
        title = next((_message_text(m)[:100] for m in forked_messages if m.get("role") == "user"), "")

    new_data: dict[str, Any] = {
        "thread_id": new_thread_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "message_count": len(forked_messages),
        "image_count": _count_images(forked_messages),
        "folder_id": data.get("folder_id"),
        "messages": forked_messages,
        "forked_from": {"thread_id": thread_id, "up_to_index": body.up_to_index},
    }
    for field in _FORK_INHERITED_FIELDS:
        if field in data:
            new_data[field] = data[field]
    if title:
        new_data["auto_title_done"] = True
    cursor = data.get("memory_extracted_index")
    if isinstance(cursor, int) and not isinstance(cursor, bool):
        user_turns = sum(1 for m in forked_messages if m.get("role") == "user")
        new_data["memory_extracted_index"] = min(cursor, user_turns)

    upload_dir = Path(settings.upload_dir) / new_thread_id
    try:
        copied = copy_referenced_uploads(forked_messages, thread_id, new_thread_id)
        new_data["messages"] = rewrite_upload_refs(forked_messages, thread_id, new_thread_id)
        _sessions_dir().mkdir(parents=True, exist_ok=True)
        create_session_json(new_thread_id, new_data)
    except OSError as e:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Failed to write session") from e
    logger.info(
        "Forked session %s -> %s (up to index %d, %d uploads copied)",
        thread_id,
        new_thread_id,
        body.up_to_index,
        copied,
    )

    return {
        "status": "forked",
        "new_thread_id": new_thread_id,
        "message_count": len(forked_messages),
        # Which sidebar scope the new row lives in, so the SPA refreshes it (D7).
        "folder_id": new_data["folder_id"],
    }


class RenameRequest(BaseModel):
    title: str


@router.patch("/{thread_id}/rename", dependencies=[Depends(verify_api_key)])
async def rename_session(thread_id: str, body: RenameRequest) -> dict[str, Any]:
    """Rename a session title."""

    def rename(data: dict[str, Any]) -> None:
        data["title"] = body.title.strip()[:100]
        # Auto Session Title (PRP-0077, CTR-0109): a manual rename claims the title
        # slot so a late-completing background title task never overwrites the
        # user-chosen title (UDR-0053 D9), and clears any pending spinner.
        data["auto_title_done"] = True
        data["auto_title_pending"] = False
        data["updated_at"] = datetime.now(UTC).isoformat()

    data = _update_session_or_error(thread_id, rename)
    logger.info("Renamed session %s to '%s'", thread_id, data["title"])

    return {"status": "renamed", "thread_id": thread_id, "title": data["title"]}


def _session_model(data: dict[str, Any]) -> str | None:
    """Model of the session's most recent assistant turn, if it recorded one.

    Used as the regeneration hint for ``resolve_task_model("session_title", ...)``
    (PRP-0115 / UDR-0096), mirroring what the AG-UI endpoint passes on the
    automatic path. ``None`` is fine -- the catalog default is then used.
    """
    for message in reversed(data.get("messages") or []):
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if isinstance(usage, dict) and usage.get("model"):
            return str(usage["model"])
    return None


@router.post("/{thread_id}/title/regenerate", dependencies=[Depends(verify_api_key)])
async def regenerate_session_title(thread_id: str) -> dict[str, Any]:
    """Regenerate a chat's title on explicit operator request (CTR-0109 v2).

    PRP-0143 / UDR-0124 D4. This is the ONLY path allowed to overwrite a title
    that has already claimed the slot (including a manual rename); automatic
    titling stays once-only and is untouched (D3).

    One endpoint serves both modes -- the mode is a server concern and the SPA
    must not branch on it (D6):

    - ``truncate``: no model to call, so the work is done inline and the response
      carries the final title. The source is the MOST RECENT user message with
      text (D5), deliberately asymmetric with automatic truncation, which uses
      the first: re-deriving from the first message would reproduce the very
      title the operator just rejected.
    - ``llm``: dispatches the CTR-0108 background task, whose per-thread dedup is
      also what bounds click-spam. ``auto_title_pending`` is set ONLY when
      dispatch actually scheduled (D7) -- otherwise the sidebar spinner would
      run forever -- and the finished title arrives over CTR-0110.
    """
    from app.agent.temporary import is_temporary

    if is_temporary(thread_id):
        raise HTTPException(status_code=409, detail="temporary chats are not titled")

    data = _read_session_or_404(thread_id)
    messages = data.get("messages") or []

    if settings.session_title_mode == "llm":
        from app.background import dispatch as dispatch_background
        from app.background.session_title import build_regeneration_window

        if not build_regeneration_window(messages):
            raise HTTPException(status_code=422, detail="no message to title")
        scheduled = dispatch_background(
            "session-title-regenerate",
            dedup_key=thread_id,
            ctx={"thread_id": thread_id, "model": _session_model(data)},
        )
        if not scheduled:
            # Unregistered task, no running loop, or a regeneration for this
            # thread is already in flight. Nothing was started, so nothing may
            # claim the spinner.
            return {"status": "unchanged", "thread_id": thread_id, "title": data.get("title", "")}

        def mark_pending(record: dict[str, Any]) -> None:
            record["auto_title_pending"] = True
            record["updated_at"] = datetime.now(UTC).isoformat()

        data = _update_session_or_error(thread_id, mark_pending)
        logger.info("Dispatched title regeneration for session %s", thread_id)
        return {"status": "pending", "thread_id": thread_id, "title": data.get("title", "")}

    from app.background.session_title import latest_user_text

    source = latest_user_text(messages)
    if not source:
        raise HTTPException(status_code=422, detail="no message to title")

    def apply_title(record: dict[str, Any]) -> None:
        record["title"] = source[:100]
        # An explicit regeneration claims the slot exactly as a rename does, so the
        # automatic path stays locked out afterwards (UDR-0124 D3).
        record["auto_title_done"] = True
        record["auto_title_pending"] = False
        record["updated_at"] = datetime.now(UTC).isoformat()

    data = _update_session_or_error(thread_id, apply_title)
    logger.info("Regenerated title for session %s: %r", thread_id, data["title"])
    return {"status": "applied", "thread_id": thread_id, "title": data["title"]}


@router.post("/{thread_id}/archive", dependencies=[Depends(verify_api_key)])
async def archive_session(thread_id: str) -> dict[str, str]:
    """Archive a session by moving it out of the session directory into ``.archived/``.

    v0.117.4: the two ways this failed on a configured deployment are fixed, and
    the remaining failures report a reason the operator can act on instead of the
    bare "Failed to archive session" that reached the browser as an opaque 500.
    """
    path = session_path(thread_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Session not found")

    archived_path = _archived_dir()
    try:
        archived_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Read-only root, a non-writable mount, or a missing parent. Name the
        # resolved path AND the setting that determines it -- the directory is
        # derived from SESSIONS_DIR, which is not obvious from the UI.
        logger.exception("Cannot create the archive directory %s", archived_path)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Cannot create the archive directory '{archived_path}' ({type(e).__name__}). "
                "It is created next to the session directory, so it follows the SESSIONS_DIR "
                "setting. Make sure that location is writable by the server process (a "
                "read-only or unmounted volume is the usual cause), then try again."
            ),
        ) from e

    dest = archived_path / f"{thread_id}.json"
    try:
        # shutil.move, NOT Path.rename: rename cannot cross filesystems, and the
        # session directory is commonly a mounted volume. shutil.move falls back
        # to copy + delete when source and destination are on different devices.
        shutil.move(str(path), str(dest))
    except OSError as e:
        logger.exception("Failed to archive session %s (%s -> %s)", thread_id, path, dest)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not move the chat into the archive directory '{archived_path}' "
                f"({type(e).__name__}). The session file itself was left untouched. Check that "
                "the archive location is writable and has free space, then try again."
            ),
        ) from e

    # Uploads are best-effort: the chat is ALREADY archived at this point, so a
    # failure here must not fail the request -- doing so would report an error for
    # an action that in fact happened and leave the sidebar row in place.
    try:
        upload_dir = Path(settings.upload_dir) / thread_id
        if upload_dir.is_dir():
            archived_uploads = Path(settings.upload_dir).parent / ".archived_uploads" / thread_id
            archived_uploads.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(upload_dir), str(archived_uploads))
            logger.info("Archived upload directory: %s -> %s", upload_dir, archived_uploads)
    except OSError:
        logger.warning(
            "Archived session %s but could not move its uploads out of %s; the attachments were left in place.",
            thread_id,
            settings.upload_dir,
            exc_info=True,
        )

    logger.info("Archived session %s -> %s", thread_id, dest)
    return {"status": "archived", "thread_id": thread_id}


class PinRequest(BaseModel):
    pinned: bool


@router.patch("/{thread_id}/pin", dependencies=[Depends(verify_api_key)])
async def pin_session(thread_id: str, body: PinRequest) -> dict[str, Any]:
    """Pin or unpin a session."""

    def pin(data: dict[str, Any]) -> None:
        data["pinned_at"] = datetime.now(UTC).isoformat() if body.pinned else None

    data = _update_session_or_error(thread_id, pin)
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

        # UDR-0119 D11: the conversation is gone, so holding its harness agent state
        # is a leak -- today a deleted conversation kept a live runtime (and its shell
        # process) until LRU evicted it 32 conversations later.
        await _reset_harness_conversation(thread_id, reason="session_deleted")

        logger.info("Deleted session: %s", thread_id)
        return {"status": "deleted", "thread_id": thread_id}
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to delete session") from e
