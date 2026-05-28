"""Demo Mode subpackage (PRP-0066, UDR-0041).

A single chokepoint `is_demo_mode()` for every factory in the codebase.
The factories route to demo implementations when this returns True,
and route to live implementations when it returns False. The decision
is made off the Settings singleton (UDR-0041 D2) so a test fixture
can flip the toggle without mutating os.environ.

Imports in this subpackage are intentionally cheap so the predicate
is import-safe from anywhere; the actual demo implementations
(`chat_client`, `stt`, `tts`, `image_gen`, `embedder`) are imported
lazily by their respective factories ONLY when this predicate
returns True. A production install that never enables demo mode
therefore never pays the import cost of `app.demo.*`.
"""

from __future__ import annotations


def is_demo_mode() -> bool:
    """Return True when DEMO_MODE is enabled on the active Settings.

    Single chokepoint per UDR-0041 D1; scattered `if settings.demo_mode:`
    blocks are forbidden -- callers MUST go through this function so the
    decision can be relocated or instrumented in one place.
    """
    from app.core.config import settings

    return bool(settings.demo_mode)


def resolve_demo_models() -> list[str]:
    """Return the effective demo model list (>= 1 entry).

    Falls back to a single ``chatwalaau-demo`` entry when DEMO_MODELS is
    empty; otherwise returns the parsed comma-separated list.
    """
    from app.core.config import settings

    return settings.demo_model_list


__all__ = ["is_demo_mode", "resolve_demo_models"]
