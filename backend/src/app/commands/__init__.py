"""Slash Commands backend (FEAT-0047, PRP-0088).

Serves the SPA a merged inventory of the effective slash commands -- the static
built-ins (CTR-0125) plus the dynamic commands derived from Prompt Templates
(CTR-0047) and Agent Skills (CTR-0043) -- via GET /api/commands (CTR-0126).

Dispatch itself is client-side (UDR-0066 D1): this package only PUBLISHES the
inventory; the AG-UI contract (CTR-0009) is unchanged.
"""
