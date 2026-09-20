"""Agent harness helpers (PRP-0067, CTR-0098; PRP-0073, CTR-0104).

Thin resolver / helper modules:

- ``app.agent.compaction`` -- builds the fixed two-stage MAF
  ``CompactionStrategy`` instance (or ``None``).
- ``app.agent.identity`` -- loads the Global Agent Identity from the fixed
  ``.agent/IDENTITY.md`` file (built-in default fallback) and assembles the
  system prompt with Identity as slot #1 (CTR-0104, UDR-0049).
- ``app.agent.wire_trace`` -- ids-and-shapes tracing of the provider request
  seam (CTR-0102, UDR-0126).

The tool-approval policy module that used to live here was removed with the
approval flow (PRP-0179, UDR-0161): no tool asks for approval on any lane.

All are pure Settings / file -> object mappings -- no new Protocol seam is
introduced (UDR-0042 D3 / UDR-0049 D9).
"""

from app.agent.compaction import resolve_compaction_strategy
from app.agent.identity import (
    DEFAULT_IDENTITY,
    IDENTITY_PATH,
    build_system_prompt,
    load_identity,
)

__all__ = [
    "DEFAULT_IDENTITY",
    "IDENTITY_PATH",
    "build_system_prompt",
    "load_identity",
    "resolve_compaction_strategy",
]
