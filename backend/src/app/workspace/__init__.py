"""Workspace file completion (CTR-0127, PRP-0088).

A workspace-jailed file/directory listing for the SPA's ``@file`` completion. It
reuses the EXISTING coding workspace jail (CTR-0031 ``resolve_safe_path``) so every
completable path resolves INSIDE CODING_WORKSPACE_DIR, and is gated by CODING_ENABLED
(CTR-0032): when coding is disabled the surface is inert (UDR-0066 D6).
"""
