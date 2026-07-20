"""Declarative agent mapped spec + error types (CTR-0142, PRP-0094, UDR-0072).

A ``DeclarativeAgentSpec`` is the ChatWalaʻau-side projection of a declarative
agent YAML: only the fields ChatWalaʻau can MAP onto its own construction path
(CTR-0070 / CTR-0102 / the prompt-assembly slots). Everything ChatWalaʻau owns --
credentials, prompt cache, web search, tools, compaction -- is NOT taken from the
YAML (UDR-0072 D1/D2/D3). A field equal to ``None`` means "inherit the current
ChatWalaʻau default", so the bundled CORE spec (all ``None``) reproduces
pre-PRP-0094 behavior byte-for-byte (UDR-0072 D13).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Sentinel for the YAML ``instructions`` field meaning "use the runtime Global
# Agent Identity (.agent/IDENTITY.md) unchanged" (UDR-0072 D6). The bundled CORE
# YAML uses it so an operator-edited IDENTITY.md still applies when CORE is active.
IDENTITY_SENTINEL = "=Identity"

CORE_AGENT_ID = "core"


class DeclarativeAgentError(Exception):
    """Raised when a declarative YAML cannot be parsed or mapped (UDR-0072 D9).

    The message is human-readable and surfaced both in the management inventory
    (per-agent ``error``) and the API 400 detail.
    """


@dataclass
class DeclarativeAgentSpec:
    """ChatWalaʻau projection of a declarative agent (CTR-0142).

    ``None`` on a mapping field == inherit the current ChatWalaʻau default. The
    CORE spec is all-``None`` so the registry build is identical to the imperative
    path (UDR-0072 D2/D13).
    """

    id: str
    name: str
    description: str = ""
    source: str = "custom"  # "core" | "custom"
    # Nested folders relative to DECLARATIVE_AGENTS_DIR (UDR-0072 D11); () for CORE
    # and for a top-level custom YAML.
    group_path: tuple[str, ...] = ()

    # ---- mapping results (None => inherit current ChatWalaʻau behavior) ----
    # slot #1 (Identity) override; None => load_identity() (UDR-0072 D6).
    instructions_override: str | None = None
    # model.id from the YAML: the PREFERRED DEFAULT model(s). The first that is
    # configured becomes the registry default; the other configured models stay
    # selectable (NOT removed) so the model picker never vanishes. None => deployment
    # default (UDR-0072 D4, amended per operator feedback).
    model_filter: list[str] | None = None
    # per-message option shape ({"effort": ..., "verbosity": ...}) passed as the
    # ``selected`` arg of providers.build_model_options; None => provider defaults.
    model_options_override: dict | None = None
    # {"schema": <json schema | None>, "mode": "json_schema"|"json_object"} default
    # structured output; None => no default (per-message control unchanged, D5).
    structured_output: dict | None = None
    # Per-agent tool surface allow-list (PRP-0117, UDR-0100 D1). A list of stable
    # tool identifiers (see app.agent.declarative.tool_ids) that SUBSETS the globally
    # available tool surface; None => inherit the full shared surface (so CORE and
    # every agent without a ``tools:`` block stays byte-for-byte, UDR-0072 D13). It
    # can only narrow, never introduce a tool. Applied at the single tool-assembly
    # chokepoint (app.agui.agent_factory) on the activate/reload rebuild (UDR-0100 D2).
    tool_allowlist: list[str] | None = None

    # Non-fatal mapping notes (ignored fields) surfaced in the inventory (D3/D5).
    warnings: list[str] = field(default_factory=list)


__all__ = [
    "CORE_AGENT_ID",
    "IDENTITY_SENTINEL",
    "DeclarativeAgentError",
    "DeclarativeAgentSpec",
]
