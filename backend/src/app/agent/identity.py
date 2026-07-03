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


def build_capability_block(
    capability_instructions: str,
    user_profile_block: str | None = None,
    agent_memory_block: str | None = None,
) -> str:
    """Assemble the post-Identity prompt blocks (CTR-0104 v3, CTR-0105, CTR-0162).

    Returns everything that follows the Identity block, in fixed order::

        [slot #2a]  Memory Block (User Profile,  when ``user_profile_block`` given)
        [slot #2b]  Memory Block (Agent Memory,  when ``agent_memory_block`` given)
        [slot #3..] capability / tool guidance (``capability_instructions``)

    This is used for the per-run prompt remainder when the agent's baked
    instructions carry only the Identity block (the memory path, PRP-0075 /
    PRP-0100). MAF concatenates the run-level instructions AFTER the agent-level
    Identity, so the final prompt is Identity -> User Profile -> Agent Memory ->
    capability guidance.
    """
    parts: list[str] = []
    if user_profile_block:
        block = user_profile_block.strip()
        if block:
            parts.append(block)
    if agent_memory_block:
        block = agent_memory_block.strip()
        if block:
            parts.append(block)
    capability_instructions = capability_instructions.strip()
    if capability_instructions:
        parts.append(capability_instructions)
    return "\n\n".join(parts)


def build_system_prompt(
    capability_instructions: str,
    user_profile_block: str | None = None,
    agent_memory_block: str | None = None,
) -> str:
    """Assemble the full system prompt with Identity as slot #1 (CTR-0104 v3).

    Prompt Assembly order (UDR-0049 D4, UDR-0051 D4, UDR-0079 D6)::

        [slot #1]   Identity (this block, from .agent/IDENTITY.md or default)
        [slot #2a]  Memory Block (User Profile, when ``user_profile_block`` given)
        [slot #2b]  Memory Block (Agent Memory, when ``agent_memory_block`` given)
        [slot #3..] capability / tool guidance (``capability_instructions``)

    The per-model web-search fragment is appended by the caller AFTER the
    capability layer, so Identity always remains slot #1 of the final prompt.

    With both memory blocks None the output is byte-for-byte identical to the
    CTR-0104 v1 behavior (Identity + blank line + capability guidance), so the
    construction-time / DevUI / headless path is unchanged (UDR-0051 D4 /
    UDR-0079 D6).
    """
    identity = load_identity()
    remainder = build_capability_block(capability_instructions, user_profile_block, agent_memory_block)
    if not remainder:
        return identity
    return f"{identity}\n\n{remainder}"
