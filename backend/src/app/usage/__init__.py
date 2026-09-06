"""Token Usage Ledger (CTR-0200 / CTR-0201, FEAT-0066, PRP-0158, UDR-0136).

The durable record of what the system spent, and the read side that aggregates it.

PRP-0157 made a turn's token numbers correct at the seam that measures them. They
still had nowhere to live: the only place a ``usage`` object is persisted is the
session file the SPA writes, and that store loses billing history through five
mechanisms that are each CORRECT for a conversation store -- delete unlinks it, the
Temporary Chat sweep unlinks it on a timer, an interrupted turn is never saved, an
imported chat is re-identified under a new id, and the writer is the browser.

So the ledger lives in its OWN directory (``USAGE_DIR``), never under
``SESSIONS_DIR`` (UDR-0136 D1).

Modules:

* ``ledger``    -- the record shape and the append seam (write side)
* ``aggregate`` -- month reading and grouping (read side)
* ``router``    -- GET /api/usage/summary (CTR-0201)

Two properties this package is built around, both from UDR-0136:

* **An append can never fail a turn** (D6). Every write is best-effort; a failure
  logs a warning and returns. Statistics are not worth failing a user's work.
* **Token counts only** (D3). No message content, no titles, no user identifiers,
  and no price -- a record that stores money goes stale with no signal, while one
  that stores tokens stays true.

Coverage is deliberately incomplete as of v0.145.0 (D11): the Declarative Workflow
lane and MAF's internal compaction calls are not recorded. What this ledger holds
is "what the observable work consumed", not "what the account was charged", and
every surface that presents its numbers has to say so.
"""

from app.usage.ledger import (
    LEDGER_FIELDS,
    TOKEN_FIELDS,
    append_helper_usage,
    append_record,
    append_turn_usage,
    ledger_dir,
    month_path,
)

__all__ = [
    "LEDGER_FIELDS",
    "TOKEN_FIELDS",
    "append_helper_usage",
    "append_record",
    "append_turn_usage",
    "ledger_dir",
    "month_path",
]
