"""AI Discussion: a human-led, semi-automatic multi-agent discussion in a chat (PRP-0232).

The discussion is driven by a ChatWalaʻau state machine over a run record kept in the chat's
session file (UDR-0215 D1); no MAF Workflow, GroupChat or checkpoint is involved.

- ``schema``       -- the run record and the message field (CTR-0271)
- ``participants`` -- role presets, seat resolution, context building, the moderator (CTR-0275)
- ``speakers``     -- one model turn: the provider client, or scripted replies (CTR-0275)
- ``controller``   -- transitions, lease, budget, persistence, the phase runner (CTR-0274)
- ``router``       -- ``/api/discussions`` and the phase stream (CTR-0272 / CTR-0273)
"""
