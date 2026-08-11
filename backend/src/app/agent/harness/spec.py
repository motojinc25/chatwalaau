"""Harness agent mapped spec + error types (CTR-0192, PRP-0135, UDR-0119).

A ``HarnessAgentSpec`` is the ChatWalaʻau-side projection of a ``kind: Harness``
YAML -- a schema ChatWalaʻau OWNS end to end (UDR-0119 D1; MAF declarative has no
harness kind). Unlike the Prompt lane (UDR-0072 D1/D2), the runtime agent IS
MAF-built: the factory (CTR-0193) passes these fields to
``create_harness_agent()`` while ChatWalaʻau supplies every input -- the client
(CTR-0102), tools (CTR-0178 identifiers), stores, and skills (UDR-0119 D2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The ``instructions.agent`` sentinel meaning "use the runtime Global Agent
# Identity (.agent/IDENTITY.md)" -- the UDR-0072 D6 convention carried over.
IDENTITY_SENTINEL = "=Identity"

HARNESS_KIND = "Harness"

# Valid initial modes for the Agent Mode Provider (UDR-0119 D4).
ALLOWED_INITIAL_MODES = ("plan", "execute")


class HarnessAgentError(Exception):
    """Raised when a harness YAML cannot be parsed or mapped (UDR-0119 D8).

    The message is human-readable and surfaced both in the management inventory
    (per-agent ``error``) and the API 400 detail.
    """


@dataclass
class HarnessAgentSpec:
    """ChatWalaʻau projection of one ``kind: Harness`` YAML (CTR-0192).

    Field defaults mirror the phase-1 policy set (UDR-0119 D4): everything the
    YAML omits inherits the harness default; the factory (CTR-0193) supplies the
    FIXED policies (InMemory history, default Todo / Mode providers,
    ``todos_remaining()`` loop, readonly approvals off) that are never spec
    fields at all.
    """

    id: str
    name: str
    display_name: str = ""
    description: str = ""
    # Nested folders relative to DECLARATIVE_AGENTS_DIR (same jail as the other
    # kinds, UDR-0119 D1); () for a top-level YAML.
    group_path: tuple[str, ...] = ()

    # ---- required mapping (blocking warning when missing/unknown, D8) ----
    # Exactly ONE catalog offering id; the CTR-0102 client source (UDR-0119 D2).
    model_id: str = ""

    # ---- instructions ----
    # None => MAF DEFAULT_HARNESS_INSTRUCTIONS.
    harness_instructions: str | None = None
    # None => ChatWalaʻau DEFAULT_AGENT_INSTRUCTIONS (factory constant);
    # IDENTITY_SENTINEL => the Global Agent Identity (UDR-0072 D6 convention).
    agent_instructions: str | None = None

    # ---- tool allow-list (CTR-0178 identifier space; [] => no extra tools) ----
    tool_allowlist: list[str] = field(default_factory=list)

    # ---- compaction (UDR-0119 D9) ----
    compaction_disabled: bool = False
    # None => catalog_context_window(model_id) at build time.
    max_context_window_tokens: int | None = None
    max_output_tokens: int | None = None

    # ---- harness building blocks (UDR-0119 D4) ----
    todo_disabled: bool = False
    mode_disabled: bool = False
    mode_initial: str | None = None  # "plan" | "execute" | None (MAF default)
    file_memory_disabled: bool = False
    file_access_disable_write_tools: bool = False
    # True => write / delete / replace run WITHOUT host approval -- an explicit,
    # file-persisted operator opt-in (UDR-0119 D6).
    file_access_disable_write_tool_approval: bool = False
    web_search_disabled: bool = False
    # None => MAF harness DEFAULT_MAX_ITERATIONS; always clamped to it (D4).
    loop_max_iterations: int | None = None

    # Validation notes. ANY warning blocks the agent from becoming a selectable
    # run-target (UDR-0119 D8, the UDR-0072 D9 pattern).
    warnings: list[str] = field(default_factory=list)


__all__ = [
    "ALLOWED_INITIAL_MODES",
    "HARNESS_KIND",
    "IDENTITY_SENTINEL",
    "HarnessAgentError",
    "HarnessAgentSpec",
]
