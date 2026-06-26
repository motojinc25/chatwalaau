"""Process-local, non-persisted Teams adapter state (CTR-0140, UDR-0070 D11).

Two short-lived working stores, consistent with the MCP override store (CTR-0122)
and the approval store (CTR-0099): nothing is persisted, a restart clears them.

- ``DedupStore``       -- remembers recently-seen activity ids so a Bot Framework
  redelivery does not re-run the agent within the dedup window (UDR-0070 D4 step 2).
  Bounded (FIFO eviction) so it cannot grow without limit.
- ``ConversationRefStore`` -- maps a thread id to the latest opaque conversation
  reference, so a proactive reply / typing indicator can be sent after the inbound
  POST has been ACKed (UDR-0070 D3/D6). A restart loses any in-flight reference
  (bounded, accepted for Phase 1).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class DedupStore:
    """Bounded set of recently-seen activity ids (FIFO eviction)."""

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = max(1, capacity)
        self._seen: OrderedDict[str, None] = OrderedDict()

    def seen_before(self, activity_id: str) -> bool:
        """Record ``activity_id`` and return True if it was already present.

        A blank id is never deduped (always returns False) so a malformed activity
        is not silently dropped by collision with the empty key.
        """
        if not activity_id:
            return False
        if activity_id in self._seen:
            self._seen.move_to_end(activity_id)
            return True
        self._seen[activity_id] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._seen)


class ConversationRefStore:
    """Thread id -> latest opaque conversation reference (bounded)."""

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = max(1, capacity)
        self._refs: OrderedDict[str, Any] = OrderedDict()

    def save(self, thread_id: str, ref: Any) -> None:
        if not thread_id:
            return
        self._refs[thread_id] = ref
        self._refs.move_to_end(thread_id)
        if len(self._refs) > self._capacity:
            self._refs.popitem(last=False)

    def get(self, thread_id: str) -> Any | None:
        return self._refs.get(thread_id)

    def __len__(self) -> int:
        return len(self._refs)


# Module-level singletons. Process-local and not persisted (UDR-0070 D11).
dedup_store = DedupStore()
conversation_refs = ConversationRefStore()
