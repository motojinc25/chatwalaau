"""Agent harness helpers (PRP-0067, CTR-0098, CTR-0099).

Two thin resolver modules:

- ``app.agent.compaction`` -- maps ``COMPACTION_STRATEGY`` to a MAF
  ``CompactionStrategy`` instance (or ``None``).
- ``app.agent.approval`` -- maps ``TOOL_APPROVAL_MODE`` /
  ``TOOL_APPROVAL_REQUIRE_LIST`` to a require-set and wraps individual
  tool callables with ``@tool(approval_mode="always_require")`` at
  registration time. Also owns the in-process approval store consumed
  by the AG-UI parked-stream resolver.

Both modules are pure Settings -> object mappings -- no new Protocol
seam is introduced (UDR-0042 D3 / UDR-0043 D1).
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

__all__ = [
    "DEFAULT_REQUIRE_LIST",
    "ApprovalRecord",
    "ApprovalResolution",
    "approval_store",
    "resolve_compaction_strategy",
    "resolve_require_set",
    "wrap_with_approval",
]
