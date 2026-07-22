"""Declarative workflow mapped spec + error type (CTR-0180, PRP-0118, UDR-0101).

A ``WorkflowSpec`` is the ChatWalaʻau-side projection of a ``kind: Workflow`` YAML:
the identity + the referenced ``kind: Prompt`` agent names + the mapped-action /
resolution warnings. The compiled MAF ``Workflow`` itself is built on demand by the
loader (never cached on the spec) so activation reflects live agent state. The YAML
is a SPECIFICATION; ChatWalaʻau owns construction of every node agent
(UDR-0101 D1/D4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

WORKFLOW_KIND = "Workflow"


class WorkflowError(Exception):
    """Raised when a workflow YAML cannot be parsed, mapped, or compiled (UDR-0101 D6).

    The message is human-readable and surfaced both in the management inventory
    (per-workflow ``error``) and the API 400 detail.
    """


@dataclass
class WorkflowSpec:
    """ChatWalaʻau projection of a declarative workflow (CTR-0180)."""

    id: str
    name: str
    # Optional human-friendly label from the YAML ``displayName`` (v0.112.1), mirroring
    # the declarative agent spec; surfaced in the management UI's secondary line.
    display_name: str = ""
    description: str = ""
    source: str = "custom"  # always "custom" (there is no bundled CORE workflow)
    group_path: tuple[str, ...] = ()

    # Names the workflow's InvokeAzureAgent actions reference (literal agentName
    # values). A dynamic ``=expression`` reference is recorded separately because it
    # cannot be statically resolved to a Prompt agent (UDR-0101 D4).
    referenced_agents: list[str] = field(default_factory=list)
    dynamic_agent_refs: list[str] = field(default_factory=list)

    # Action kinds present in the graph (for the node/edge summary CTR-0182 exposes).
    action_kinds: list[str] = field(default_factory=list)

    # Non-fatal mapping / resolution notes. ANY warning blocks activation
    # (UDR-0101 D4/D6, the UDR-0072 D9 pattern); the loader appends resolution
    # warnings (unknown agent, non-Prompt reference, unmapped action).
    warnings: list[str] = field(default_factory=list)


__all__ = ["WORKFLOW_KIND", "WorkflowError", "WorkflowSpec"]
