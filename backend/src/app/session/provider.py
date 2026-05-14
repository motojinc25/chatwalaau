"""File-based session history provider (CTR-0014).

Extends MAF's HistoryProvider to persist conversation history
as JSON files in the .sessions/ directory. Each session is keyed
by the AG-UI thread_id (accessed via session.metadata["ag_ui_thread_id"]).
"""

from collections.abc import Sequence
import json
import logging
from pathlib import Path
from typing import Any

from agent_framework import (
    AgentSession,
    Content,
    HistoryProvider,
    Message,
    SessionContext,
    SupportsAgentRun,
)

from app.core.config import settings

logger = logging.getLogger(__name__)


class FileHistoryProvider(HistoryProvider):
    """Persists conversation history to JSON files."""

    def __init__(self, sessions_dir: Path) -> None:
        super().__init__(source_id="file_history")
        self._sessions_dir = sessions_dir
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

    def _get_thread_id(self, session: AgentSession) -> str | None:
        """Extract thread_id from AG-UI metadata injected by the protocol layer."""
        metadata = getattr(session, "metadata", None)
        if isinstance(metadata, dict):
            return metadata.get("ag_ui_thread_id")
        return None

    def _session_path(self, thread_id: str) -> Path:
        return self._sessions_dir / f"{thread_id}.json"

    def _read_session_data(self, thread_id: str) -> dict[str, Any] | None:
        path = self._session_path(thread_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_session_data(self, thread_id: str, data: dict[str, Any]) -> None:
        path = self._session_path(thread_id)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _extract_title(messages: list[dict[str, Any]]) -> str:
        """Extract title from first user message content."""
        for msg in messages:
            if msg.get("role") != "user":
                continue
            contents = msg.get("contents", [])
            if contents and isinstance(contents[0], dict):
                text = contents[0].get("text", "")
                return text[:100]
        return ""

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        """Not called directly; before_run is overridden."""
        return []

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Not called directly; after_run is overridden."""

    @staticmethod
    def _normalize_content_types(raw_messages: list[dict[str, Any]]) -> None:
        """Fix legacy content type names in-place.

        v0.11.0 stored ``text_content`` and ``reasoning_content`` but MAF's
        ContentType expects ``text`` and ``text_reasoning``.  Normalizing here
        ensures ``Message.from_dict()`` produces Content objects that the
        Azure OpenAI Responses client can serialise correctly.
        """
        _TYPE_MAP = {"text_content": "text", "reasoning_content": "text_reasoning"}
        for msg in raw_messages:
            for content in msg.get("contents", []):
                if isinstance(content, dict) and content.get("type") in _TYPE_MAP:
                    content["type"] = _TYPE_MAP[content["type"]]

    async def before_run(
        self,
        *,
        agent: SupportsAgentRun,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Load conversation history from file and inject into context."""
        thread_id = self._get_thread_id(session)
        if not thread_id or not self.load_messages:
            return

        data = self._read_session_data(thread_id)
        if not data:
            return

        raw_messages = data.get("messages", [])
        if not raw_messages:
            return

        self._normalize_content_types(raw_messages)
        # Strip frontend-only fields before Message.from_dict(); MAF Message doesn't accept them
        _frontend_keys = {"tool_calls", "usage", "activity_log"}
        messages = [Message.from_dict({k: v for k, v in m.items() if k not in _frontend_keys}) for m in raw_messages]
        self._resolve_image_contents(messages, raw_messages)
        context.extend_messages(self, messages)
        logger.info("Loaded %d messages from session %s", len(messages), thread_id)

    @staticmethod
    def _resolve_image_contents(messages: list[Message], raw_messages: list[dict[str, Any]]) -> None:
        """Convert image_url content entries to MAF Content objects.

        Session JSON stores images as ``{"type": "image_url", "uri": "...", "media_type": "..."}``.
        ``Message.from_dict()`` does not recognise this custom type, so we resolve
        them here: local uploads become ``Content.from_data()`` (reads file bytes),
        external URLs become ``Content.from_uri()``.
        """
        upload_dir = Path(settings.upload_dir)
        for maf_msg, raw_msg in zip(messages, raw_messages, strict=False):
            for content_dict in raw_msg.get("contents", []):
                if not isinstance(content_dict, dict) or content_dict.get("type") != "image_url":
                    continue
                uri = content_dict.get("uri", "")
                media_type = content_dict.get("media_type", "")
                # Skip non-image files (e.g., PDFs uploaded for RAG ingestion).
                # Azure OpenAI Responses API only accepts image/* content types.
                if not media_type.startswith("image/"):
                    continue
                if uri.startswith("/api/uploads/"):
                    # Local upload: /api/uploads/{thread_id}/{filename}
                    parts = uri.split("/")  # ["", "api", "uploads", thread_id, filename]
                    if len(parts) >= 5:
                        file_path = upload_dir / parts[3] / parts[4]
                        if file_path.is_file():
                            maf_msg.contents.append(
                                Content.from_data(data=file_path.read_bytes(), media_type=media_type)
                            )
                elif uri.startswith(("http://", "https://")):
                    maf_msg.contents.append(Content.from_uri(uri=uri, media_type=media_type))

    async def after_run(
        self,
        *,
        agent: SupportsAgentRun,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """No-op: saving is handled by POST /api/sessions/{thread_id}/messages.

        AG-UI's _normalize_response_stream() bypasses ResponseStream finalizers,
        so this method may not be called reliably. The frontend saves messages
        via the session API after stream completion instead.
        """
