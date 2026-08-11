"""Harness Agents (FEAT-0064, PRP-0135, UDR-0119).

A ``kind: Harness`` YAML is a ChatWalaʻau-OWNED schema (no MAF declarative
counterpart, UDR-0119 D1) mapped to a :class:`HarnessAgentSpec` (CTR-0192) and
converted by the Harness Factory (CTR-0193) into ONE MAF
``create_harness_agent()`` call. MAF owns ASSEMBLY; ChatWalaʻau owns every
INPUT (UDR-0119 D2): the client (CTR-0102), the tool allow-list (CTR-0178
identifier space), workspace stores / shell (CODING_WORKSPACE_DIR), and skills
(SKILLS_DIR). A harness agent is a per-conversation RUN-TARGET selected via
AG-UI ``state.harness_id`` -- never a persona (UDR-0119 D3).
"""

from app.agent.harness.spec import HarnessAgentError, HarnessAgentSpec

__all__ = ["HarnessAgentError", "HarnessAgentSpec"]
