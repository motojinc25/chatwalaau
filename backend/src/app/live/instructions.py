"""GPT-Live session instructions (PRP-0188, UDR-0170 D8).

GPT-Live's ``instructions`` are immutable once the session starts, and the session
object is strict: there is no tool declaration. So the live model is told HOW to
converse and WHEN to delegate; everything that needs tools, facts or long answers is
delegated to the selected Prompt agent (CTR-0226), which keeps its own instructions,
memory and tools.

The Prompt agent's own instructions are deliberately NOT copied here: they are long,
they carry tool guidance that means nothing to a model without tools, and they would
be frozen for the whole session while the agent's are not.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The fixed Live base prompt. An invariant test pins the delegation clauses.
LIVE_BASE_PROMPT = """\
You are the voice of a ChatWalaau assistant in a live, full-duplex conversation.
Speak naturally and briefly, in the language the user speaks.

Delegate whenever the request needs facts you are not sure of, tools, files, \
calculation, current information, or an answer longer than a few sentences. \
The application runs delegated work with the user's selected agent and shows the \
complete answer in the chat.

While delegated work is running, keep the conversation going, but never invent its \
result. When the result arrives, say it in your own words and mention that the \
full answer is in the chat.

Ask before anything that would act on the outside world."""

#: Upper bound on the identity text carried into the Live instructions.
IDENTITY_CHAR_CAP = 2000


def build_live_instructions() -> str:
    """The Live base prompt plus the Global Agent Identity (FEAT-0032), capped."""
    identity = ""
    try:
        from app.agent.identity import load_identity

        identity = (load_identity() or "").strip()
    except Exception:  # never fail a session start over the identity file
        logger.warning("Live: could not load the agent identity", exc_info=True)
    if len(identity) > IDENTITY_CHAR_CAP:
        identity = identity[:IDENTITY_CHAR_CAP].rstrip()
    if not identity:
        return LIVE_BASE_PROMPT
    return f"{LIVE_BASE_PROMPT}\n\n# Identity\n{identity}"
