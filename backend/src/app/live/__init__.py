"""Live voice conversation with GPT-Live (FEAT-0070, PRP-0188, UDR-0170).

Full-duplex voice over WebRTC. The backend creates the session (no ephemeral client
keys exist), attaches a sideband WebSocket that is the only controller of the session
(CTR-0225), runs client delegations with the selected Prompt agent (CTR-0226), and
stores the conversation as ordinary chat messages marked ``source: "live"``
(CTR-0227). The SPA talks to CTR-0223 / CTR-0224 only.
"""

from app.live.router import register_live, shutdown

__all__ = ["register_live", "shutdown"]
