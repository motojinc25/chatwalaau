"""Per-sender Teams authorization (CTR-0138/CTR-0140, PRP-0092, UDR-0070 D5).

Application-layer authorization that runs AFTER Bot Framework JWT validation: if
``TEAMS_ALLOWED_USERS`` is non-empty, only listed Microsoft Entra ID Object IDs
(``aadObjectId``) are served. Empty (default) = everyone in the bot's reach.

Because a group/channel maps to a SHARED thread, this check runs on EVERY inbound
message (per sender), not once per thread. It fails CLOSED: a message whose sender
Object ID is missing is refused when an allow-list is configured.
"""

from __future__ import annotations

from app.core.config import settings


def is_sender_allowed(sender_object_id: str | None) -> bool:
    """Return True if the sender may use the bot (UDR-0070 D5).

    - Empty allow-list (``TEAMS_ALLOWED_USERS`` unset) -> everyone allowed.
    - Non-empty allow-list -> only listed Entra Object IDs; a missing/blank sender
      id is refused (fail closed).
    """
    allowed = settings.teams_allowed_users_set
    if not allowed:
        return True
    sid = (sender_object_id or "").strip()
    if not sid:
        return False
    return sid in allowed
