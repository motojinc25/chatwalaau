"""Global Agent Identity and Prompt Assembly (PRP-0073, CTR-0104, UDR-0049).

The agent identity -- the instance-wide persona, tone, base posture, and
behaviors-to-avoid shared across every project and conversation -- is the
FIRST block of every agent's system prompt ("Prompt Assembly slot #1",
UDR-0049 D4).

It is loaded from a single FIXED file ``.agent/IDENTITY.md`` (resolved against
the process working directory; NOT environment-configurable, UDR-0049 D2). When
the file is absent the runtime best-effort writes it from ``DEFAULT_IDENTITY``;
when the file cannot be read or written (read-only filesystem, permissions,
empty / unreadable file) the in-memory ``DEFAULT_IDENTITY`` is used and a
WARNING is logged -- ``load_identity`` never raises and never blocks agent
construction (UDR-0049 D3).

No new Protocol seam and no new Capability are introduced; the MAF Agent
``instructions=`` parameter remains the boundary (UDR-0049 D9).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Fixed, CWD-relative identity file path (UDR-0049 D2 -- not env-configurable).
IDENTITY_PATH = Path(".agent") / "IDENTITY.md"

# Built-in default identity -- the single source of truth for the fallback
# content (UDR-0049 D3). Ships in code so it always exists in every install
# layout (PyPI wheel, container image, source checkout).
DEFAULT_IDENTITY = (
    "You are ChatWalaʻau Agent, an intelligent AI assistant created by "
    "Jingun Jung of WeDX Digital Twins Solutions.\n"
    "You are helpful, knowledgeable, practical, and direct.\n"
    "You assist users with a wide range of tasks, including answering "
    "questions, writing and editing code, analyzing information, creating "
    "content, solving problems, and executing tools when appropriate.\n"
    "You communicate clearly, honestly, and efficiently.\n"
    "When information is uncertain or incomplete, you acknowledge the "
    "uncertainty and explain what is known.\n"
    "You prioritize genuine usefulness, accuracy, and practical value over "
    "unnecessary verbosity.\n"
)


def load_identity() -> str:
    """Return the agent identity text (CTR-0104).

    Reads ``.agent/IDENTITY.md`` when it exists and is non-empty. When the file
    is absent, best-effort creates it from ``DEFAULT_IDENTITY`` so the operator
    has a discoverable, editable template. On any read or write failure
    (read-only filesystem, permissions, empty / unreadable file) logs a WARNING
    and returns the in-memory ``DEFAULT_IDENTITY``. Never raises (UDR-0049 D3).
    """
    try:
        if IDENTITY_PATH.is_file():
            content = IDENTITY_PATH.read_text(encoding="utf-8").strip()
            if content:
                return content
            logger.warning(
                "Agent identity file %s is empty; using the built-in default identity",
                IDENTITY_PATH,
            )
            return DEFAULT_IDENTITY.strip()
    except OSError:
        logger.warning(
            "Could not read agent identity file %s; using the built-in default identity",
            IDENTITY_PATH,
            exc_info=True,
        )
        return DEFAULT_IDENTITY.strip()

    # File absent -> best-effort materialize the default (UDR-0049 D3). A failure
    # here (e.g. read-only container filesystem) must not block startup.
    try:
        IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        IDENTITY_PATH.write_text(DEFAULT_IDENTITY, encoding="utf-8")
        logger.info("Created default agent identity file at %s", IDENTITY_PATH)
    except OSError:
        logger.warning(
            "Could not create agent identity file %s (read-only filesystem?); using the built-in default identity",
            IDENTITY_PATH,
            exc_info=True,
        )

    return DEFAULT_IDENTITY.strip()


def build_system_prompt(capability_instructions: str) -> str:
    """Assemble the full system prompt with Identity as slot #1 (CTR-0104).

    Prompt Assembly order (UDR-0049 D4)::

        [slot #1]   Identity (this block, from .agent/IDENTITY.md or default)
        [slot #2..] capability / tool guidance (``capability_instructions``)

    The per-model web-search fragment is appended by the caller AFTER the
    capability layer, so Identity always remains slot #1 of the final prompt.
    """
    identity = load_identity()
    capability_instructions = capability_instructions.strip()
    if not capability_instructions:
        return identity
    return f"{identity}\n\n{capability_instructions}"
