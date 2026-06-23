"""Cron Scheduler subpackage (FEAT-0048, PRP-0089, UDR-0067).

A self-contained, file-backed scheduler owned by CAP-002. It runs an in-process
tick loop (CTR-0130) that fires due jobs persisted as per-file JSON records
(CTR-0131), executes their scripts inside the coding-workspace realpath jail
(CTR-0132), and is managed via a REST API (CTR-0133) and a ``manage_cron`` agent
Function Tool (CTR-0134). The frontend portal is CTR-0135.

Everything here is inert unless ``CRON_ENABLED`` is true (UDR-0067 D10).
"""

from __future__ import annotations

__all__: list[str] = []
