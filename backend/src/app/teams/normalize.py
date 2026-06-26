"""Teams inbound normalization helpers (CTR-0140, PRP-0092, UDR-0070 D4/D5/D7).

Pure, SDK-independent functions used by the adapter's 8-step pipeline:

- ``thread_id_for(conversation_id)``   -- deterministic ``teams_`` thread mapping (D5)
- ``classify(conversation_type_raw)``  -- personal / group / channel (D4 step 5)
- ``strip_bot_mention(text, bot_id)``  -- remove the bot's OWN mention (D7)

These are deliberately free of any ``microsoft_teams`` import so they can be unit
tested without the SDK installed.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.teams.message import ConversationType

# Bot mention markup fallback (UDR-0070 D7). The primary path removes only the
# bot's own recipient mention via the SDK's mention entities; this regex is the
# fallback for a leading/standalone ``<at>...</at>`` span.
_AT_TAG_RE = re.compile(r"<at>[^<]*</at>\s*")

# Teams thread-id prefix, consistent with resp_ (Responses API) and temp_
# (Temporary Chat) so Teams threads ride the existing session storage unchanged.
THREAD_PREFIX = "teams_"


def thread_id_for(conversation_id: str) -> str:
    """Map a Teams conversation id to one ChatWalaʻau thread id (UDR-0070 D5).

    Deterministic and stable: the same Teams conversation always yields the same
    thread id, so a personal/group/channel conversation maps to a single (for
    group/channel, SHARED) thread. A short blake2b hash keeps the id filesystem-
    and URL-safe regardless of the raw conversation id's characters.
    """
    digest = hashlib.blake2b(conversation_id.encode("utf-8"), digest_size=16).hexdigest()
    return f"{THREAD_PREFIX}{digest}"


def is_teams_thread(thread_id: str) -> bool:
    """True if ``thread_id`` belongs to the Teams channel (teams_ prefix)."""
    return thread_id.startswith(THREAD_PREFIX)


def classify(conversation_type_raw: str | None) -> ConversationType:
    """Classify a Bot Framework conversation type into our enum (UDR-0070 D4 step 5).

    Bot Framework ``conversation.conversationType`` is ``personal`` for 1:1,
    ``groupChat`` for a group chat, and ``channel`` for a channel. Anything
    unrecognized is treated as ``personal`` (the safest, most-restrictive default:
    a single participant, no @mention requirement).
    """
    raw = (conversation_type_raw or "").strip().lower()
    if raw == "channel":
        return "channel"
    if raw in {"groupchat", "group"}:
        return "group"
    return "personal"


def strip_bot_mention(text: str, *, mention_texts: list[str] | None = None) -> str:
    """Remove the bot's OWN @mention from inbound text (UDR-0070 D7).

    ``mention_texts`` are the rendered mention strings the SDK resolved for the
    BOT recipient (e.g. ``"<at>ChatWalaau</at>"`` or the display name). Each is
    removed first so user-to-user mentions are preserved. The ``<at>...</at>``
    regex is then applied as a fallback only for a bot-addressing span the SDK did
    not enumerate. Leading/trailing whitespace is collapsed.
    """
    out = text or ""
    for mention in mention_texts or []:
        if mention:
            out = out.replace(mention, " ")
    # Fallback: a single leading bot-mention tag the SDK did not resolve. We strip
    # at most the leading run so an embedded user-to-user <at> is left intact.
    out = _AT_TAG_RE.sub("", out, count=1) if out.lstrip().startswith("<at>") else out
    return out.strip()
