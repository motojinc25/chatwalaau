"""Microsoft Teams channel adapter (CAP-009, FEAT-0050, PRP-0092, UDR-0070).

A Ports-and-Adapters channel adapter that lets a user converse with the
ChatWalaʻau agent from a Microsoft Teams personal chat, group chat, or channel.

Design (UDR-0070):

- The agent core (CAP-002) is REUSED unchanged and stays Teams-agnostic. Only the
  normalized ``TeamsMessage`` (CTR-0139) crosses inward; nothing in the core imports
  the Teams SDK or a Bot Framework Activity (D4).
- ``app.main`` owns the FastAPI lifecycle and MOUNTS the inbound router (CTR-0138);
  the ``microsoft-teams-apps`` SDK is hosted IN-PROCESS and owns Teams protocol
  handling (JWT validation, activity dispatch). The SDK never runs its own server (D2).
- The inbound POST is ACKed promptly; the agent turn runs on the Background Task
  Runner (CTR-0108) and the reply is sent PROACTIVELY (typing + chunking + image) (D6).
- Tool approval renders an Adaptive Card (Allow Once / Allow Session / Deny) mapped to
  the CTR-0099 approval store; Teams never auto-approves (D8).
- The whole package is INERT unless ``TEAMS_ENABLED`` (D10): ``register_teams`` is a
  no-op when disabled, so the router is not mounted and the SDK is not imported.

Module layout (SDK-independent core is unit-testable without the SDK installed):

- ``message``   -- the TeamsMessage normalized model (CTR-0139)
- ``normalize`` -- thread-id mapping, conversation classification, mention stripping
- ``authz``     -- per-sender TEAMS_ALLOWED_USERS authorization
- ``store``     -- process-local dedup + conversation-reference stores (D11)
- ``reply``     -- outbound chunking at TEAMS_MAX_REPLY_CHARS
- ``approval``  -- Adaptive Card payload + Action.Submit -> approval decision (CTR-0141)
- ``agent_run`` -- run one agent turn through the registry chokepoint
- ``adapter``   -- the SDK host + 8-step inbound pipeline (CTR-0140; lazy SDK import)
- ``router``    -- the inbound FastAPI router + ``register_teams(app, ...)`` (CTR-0138)
"""

from __future__ import annotations

from app.teams.router import initialize_teams, register_teams

__all__ = ["initialize_teams", "register_teams"]
