"""Agent harness helpers (PRP-0067, CTR-0098, CTR-0099; PRP-0073, CTR-0104).

Thin resolver / helper modules:

- ``app.agent.compaction`` -- maps ``COMPACTION_STRATEGY`` to a MAF
  ``CompactionStrategy`` instance (or ``None``).
- ``app.agent.approval`` -- maps ``TOOL_APPROVAL_MODE`` /
  ``TOOL_APPROVAL_REQUIRE_LIST`` to a require-set and wraps individual
  tool callables with ``@tool(approval_mode="always_require")`` at
  registration time. Also owns the in-process approval store consumed
  by the AG-UI parked-stream resolver.
- ``app.agent.identity`` -- loads the Global Agent Identity from the fixed
  ``.agent/IDENTITY.md`` file (built-in default fallback) and assembles the
  system prompt with Identity as slot #1 (CTR-0104, UDR-0049).

All three are pure Settings / file -> object mappings -- no new Protocol
seam is introduced (UDR-0042 D3 / UDR-0043 D1 / UDR-0049 D9).
"""

from app.agent.approval import (
    DEFAULT_REQUIRE_LIST,
    ApprovalRecord,
    ApprovalResolution,
    approval_store,
    resolve_require_set,
    wrap_with_approval,
)
from app.agent.compaction import resolve_compaction_strategy
from app.agent.identity import (
    DEFAULT_IDENTITY,
    IDENTITY_PATH,
    build_system_prompt,
    load_identity,
)

__all__ = [
    "DEFAULT_IDENTITY",
    "DEFAULT_REQUIRE_LIST",
    "IDENTITY_PATH",
    "ApprovalRecord",
    "ApprovalResolution",
    "approval_store",
    "build_system_prompt",
    "load_identity",
    "resolve_compaction_strategy",
    "resolve_require_set",
    "wrap_with_approval",
]
