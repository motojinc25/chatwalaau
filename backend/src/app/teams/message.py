"""TeamsMessage normalized model (CTR-0139, PRP-0092, UDR-0070 D4).

The single boundary DTO between Microsoft Teams and the ChatWalaʻau core. The
adapter (CTR-0140) builds a ``TeamsMessage`` from an inbound Bot Framework
Activity; the core consumes ONLY ``TeamsMessage`` and never sees a Teams SDK type.

The ``conversation_ref`` field is an OPAQUE handle used by the adapter to send a
proactive reply / typing indicator. It is declared ``Any`` so this module -- and
therefore the core -- carries no static dependency on the Teams SDK. Nothing here
is persisted; a ``TeamsMessage`` is an in-memory per-turn value object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ConversationType = Literal["personal", "group", "channel"]


@dataclass(frozen=True)
class TeamsMessage:
    """A normalized, platform-neutral inbound Teams message (CTR-0139)."""

    thread_id: str
    """ChatWalaʻau thread id: ``teams_<stable-hash(conversationId)>`` (UDR-0070 D5)."""

    conversation_type: ConversationType
    """personal (1:1) / group / channel."""

    text: str
    """User text with the bot's own mention stripped (UDR-0070 D7)."""

    sender_object_id: str
    """Sender Microsoft Entra ID Object ID (aadObjectId); authorization key."""

    sender_id: str
    """Sender Bot Framework channel account id (from.id); used to @mention the
    user in a group/channel reply (UDR-0070 D6). Distinct from sender_object_id."""

    sender_name: str
    """Sender display name."""

    message_id: str
    """Source Bot Framework activity id; the dedup key (UDR-0070 D4 step 2)."""

    images: list[str] = field(default_factory=list)
    """Session-scoped cached image references (multimodal input)."""

    conversation_ref: Any = None
    """OPAQUE reply/typing handle, used only by the adapter (never by the core)."""
