"""Memory Files Editing API (CTR-0166, PRP-0101 / UDR-0080).

A small, FIXED-ENUM human-facing editing surface over the three built-in file
memories under ``.agent/``:

- ``identity`` -> ``.agent/IDENTITY.md`` (CTR-0104, Prompt Assembly slot #1)
- ``user``     -> ``.agent/USER.md``     (CTR-0105, Memory Block slot #2a)
- ``agent``    -> ``.agent/MEMORY.md``   (CTR-0162, Agent Memory slot #2b)

``GET /api/memory/files`` returns all three with metadata (title, role summary,
enabled flag, raw content, char count, char limit, applies-when note). ``PUT
/api/memory/files/{key}`` validates the per-file character cap, writes a
timestamped backup, then atomically replaces the file (temp file + ``os.replace``;
a failed backup aborts the write).

The operator write guard is BACKUP + CHAR-LIMIT ONLY (UDR-0080 D2). The
deterministic secret/PII filter that guards the AGENT write paths (CTR-0105 /
CTR-0162) is NOT run here -- it exists to stop the LLM, not the trusted operator,
from persisting a secret (the File Explorer PUT /file precedent). The paths and
caps are reused from CTR-0104 / CTR-0105 / CTR-0162; a human edit changes the LIVE
file only, so it takes effect from the next session (USER / MEMORY) or the next
agent build (IDENTITY) -- the running session's frozen snapshot is untouched.

CTR-0083-gated: the PUT mutation and the GET read both depend on ``verify_api_key``
(loopback bypass). No new dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.agent_memory import AGENT_MEMORY_PATH, load_agent_memory
from app.agent.identity import IDENTITY_PATH, load_identity
from app.agent.user_memory import USER_PROFILE_PATH, load_user_profile
from app.auth import verify_api_key
from app.core.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["Memory Editor"])


@dataclass(frozen=True)
class _FileSpec:
    """Static description of one editable memory file (CTR-0166)."""

    key: str
    path: Path
    title: str
    role: str
    applies_when: str
    loader: Callable[[], str]
    cap: Callable[[], int]
    enabled: Callable[[], bool]


# Fixed enum of the three built-in file memories. The cap / enabled callables read
# ``settings`` at request time so a reloaded config is honored.
_FILES: dict[str, _FileSpec] = {
    "identity": _FileSpec(
        key="identity",
        path=IDENTITY_PATH,
        title="Agent Identity",
        role=(
            "The agent's persona, tone, and base posture. Injected as the first block of "
            "every system prompt (Prompt Assembly slot #1)."
        ),
        applies_when="Applies on the next agent build (restart).",
        loader=load_identity,
        cap=lambda: settings.identity_char_limit,
        enabled=lambda: True,
    ),
    "user": _FileSpec(
        key="user",
        path=USER_PROFILE_PATH,
        title="User Preference Memory",
        role=(
            "What the assistant remembers about you -- preferences, communication style, and "
            "workflow habits (Memory Block slot #2a)."
        ),
        applies_when="Applies from your next chat (the current session is frozen).",
        loader=load_user_profile,
        cap=lambda: settings.user_char_limit,
        enabled=lambda: settings.user_profile_enabled,
    ),
    "agent": _FileSpec(
        key="agent",
        path=AGENT_MEMORY_PATH,
        title="Agent Curated Memory",
        role=(
            "The assistant's own notes about the work -- environment facts, project "
            "conventions, and tool quirks (Agent Memory slot #2b)."
        ),
        applies_when="Applies from your next chat (the current session is frozen).",
        loader=load_agent_memory,
        cap=lambda: settings.memory_char_limit,
        enabled=lambda: settings.agent_memory_enabled,
    ),
}


class MemoryFile(BaseModel):
    key: str
    title: str
    role: str
    enabled: bool
    content: str
    char_count: int
    char_limit: int
    applies_when: str


class MemoryFilesResponse(BaseModel):
    files: list[MemoryFile]


class SaveMemoryRequest(BaseModel):
    # The cap is enforced explicitly below (with a friendly 422 message); the
    # Field bound is a coarse abuse guard well above any per-file cap.
    content: str = Field(default="", max_length=200000)


class SaveMemoryResponse(BaseModel):
    key: str
    char_count: int
    char_limit: int
    backup: str | None


def _describe(spec: _FileSpec) -> MemoryFile:
    """Build the GET payload for one file (raw content via its loader)."""
    try:
        content = spec.loader()
    except Exception:
        logger.warning("Could not load memory file %s; returning empty", spec.path, exc_info=True)
        content = ""
    return MemoryFile(
        key=spec.key,
        title=spec.title,
        role=spec.role,
        enabled=spec.enabled(),
        content=content,
        char_count=len(content),
        char_limit=spec.cap(),
        applies_when=spec.applies_when,
    )


def _backup_and_atomic_write(path: Path, content: str) -> str | None:
    """Write ``content`` to ``path`` durably (UDR-0080 D3).

    Backs up the prior file to ``<name>.bak-<timestamp>`` BEFORE writing, then
    writes a temp file and atomically ``os.replace``-s it (never ``open("w")`` in
    place). A backup failure raises before the target is touched, so the existing
    file is never corrupted. Returns the backup filename (or None when the file did
    not previously exist).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_name: str | None = None
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        backup = path.parent / f"{path.name}.bak-{stamp}"
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        backup_name = backup.name
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)  # atomic same-volume replace (Path.replace -> os.replace)
    return backup_name


@router.get("/files", response_model=MemoryFilesResponse, dependencies=[Depends(verify_api_key)])
async def list_memory_files() -> MemoryFilesResponse:
    """List the three built-in memory files with role summaries and caps (CTR-0166)."""
    return MemoryFilesResponse(files=[_describe(_FILES[k]) for k in ("identity", "user", "agent")])


@router.put("/files/{key}", response_model=SaveMemoryResponse, dependencies=[Depends(verify_api_key)])
async def save_memory_file(key: str, body: SaveMemoryRequest) -> SaveMemoryResponse:
    """Save one memory file with a backup + cap check (CTR-0166).

    Trusted operator write: backup + char-limit only (no secret/PII filter,
    UDR-0080 D2). Over-cap -> 422; unknown key -> 404. The write does not alter the
    running session's frozen snapshot (UDR-0080 D6).
    """
    spec = _FILES.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown memory file key: {key!r}")

    content = body.content
    cap = spec.cap()
    if len(content) > cap:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Content is {len(content)} characters but the limit is {cap}. Remove or shorten text before saving."
            ),
        )

    try:
        backup_name = _backup_and_atomic_write(spec.path, content)
    except OSError as exc:
        logger.warning("Could not save memory file %s: %s", spec.path, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {spec.title}: {exc}") from exc

    logger.info("memory file saved: key=%s path=%s chars=%d backup=%s", key, spec.path, len(content), backup_name)
    return SaveMemoryResponse(key=key, char_count=len(content), char_limit=cap, backup=backup_name)


__all__ = ["router"]
