"""SDK-independent Teams inbound pipeline (CTR-0140, PRP-0092, UDR-0070 D4/D5).

The 8-step normalization, expressed over an ``InboundView`` -- a small, SDK-neutral
projection of a Bot Framework Activity that the adapter's SDK glue fills in. Keeping
the pipeline here (no ``microsoft_teams`` import) makes it unit-testable without the
SDK installed.

Pipeline (UDR-0070 D4):

1. Ignore the bot's own messages (``is_bot_echo``).
2. De-duplicate by activity id (``store.dedup_store``).
3. Save the conversation reference (caller, after a build succeeds).
4. Extract text.
5. Classify personal / group / channel.
6. Resolve sender Entra Object ID + name.
7. Cache attached images (caller supplies already-cached URIs).
8. Build the TeamsMessage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.teams import normalize
from app.teams.message import TeamsMessage
from app.teams.store import dedup_store


@dataclass
class InboundView:
    """An SDK-neutral projection of a Bot Framework message Activity."""

    activity_id: str
    conversation_id: str
    conversation_type_raw: str | None
    text: str
    sender_object_id: str
    sender_name: str
    sender_id: str = ""
    is_bot_echo: bool = False
    bot_mention_texts: list[str] = field(default_factory=list)
    image_uris: list[str] = field(default_factory=list)
    conversation_ref: Any = None
    # Adaptive Card Action.Submit payload (Bot Framework delivers a card submit as a
    # message activity with `value` set); resolved by the adapter before any agent
    # turn (CTR-0141). None for an ordinary message.
    submit_value: dict[str, Any] | None = None


def build_teams_message(view: InboundView) -> TeamsMessage | None:
    """Run pipeline steps 1, 2, 4, 5, 6, 8 and return a TeamsMessage, or None.

    Returns None when the activity is the bot's own echo (step 1) or a duplicate
    redelivery within the dedup window (step 2) -- in both cases no agent run.
    Steps 3 (save conversation ref) and 7 (cache images) are the caller's job: the
    caller saves ``view.conversation_ref`` after a non-None build, and supplies
    already-cached image URIs in ``view.image_uris``.
    """
    # Step 1: ignore the bot's own echoes.
    if view.is_bot_echo:
        return None
    # Step 2: dedup by activity id.
    if dedup_store.seen_before(view.activity_id):
        return None
    # Step 4 + 7 (mention strip is step 4's normalization): user text only.
    text = normalize.strip_bot_mention(view.text, mention_texts=view.bot_mention_texts)
    # Step 5: classify.
    conversation_type = normalize.classify(view.conversation_type_raw)
    # Step 8: build the normalized message (step 6 sender fields carried through).
    return TeamsMessage(
        thread_id=normalize.thread_id_for(view.conversation_id),
        conversation_type=conversation_type,
        text=text,
        sender_object_id=view.sender_object_id,
        sender_id=view.sender_id,
        sender_name=view.sender_name,
        message_id=view.activity_id,
        images=list(view.image_uris),
        conversation_ref=view.conversation_ref,
    )
