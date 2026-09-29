"""CodeAct instructions and tool description (PRP-0193, UDR-0175 D8).

MAF's own CodeAct text (``agent_framework_monty._instructions``) is written for an
agent whose tools ALL live in the sandbox: it opens with "You have one primary tool:
`execute_code`" and asks for "a single `execute_code` call per request". A
ChatWalaʻau agent keeps its weather / RAG / image / MCP / Skills tools as DIRECT
tools, so that text would steer it to route everything through the sandbox -- and
with zero sandbox tools it even tells the model to ask the operator for a
``workspace_root``. Neither text is ever sent; these are.

The guidance travels as a ``<tool-guide name="code_act">`` block rendered by the
shared renderer, so it has the form of every other capability block (UDR-0103 D1).
It is injected by the provider in the SAME ``before_run`` decision that injects the
tool (UDR-0175 D5, the UDR-0167 D6 one-value rule), never baked into slot #3.
"""

from __future__ import annotations

from app.agent.capability_guidance import ToolGuidance, render_capability_guidance

CODEACT_GUIDANCE_NAME = "code_act"

CODEACT_INSTRUCTION = """\
You can run Python code with the `execute_code` tool, in an isolated compute sandbox.

Use it whenever a wrong digit would matter: exact arithmetic, dates and durations,
unit conversion, counting, sorting and aggregating data you already have, reshaping
JSON, and regular expressions. Do not use it for anything that needs no computation.

- Put the data into the code as literals. The sandbox has no files, no network and no
  other tools: call your other tools directly, as usual, and pass their results into
  the code.
- It is a Python subset. `json`, `re`, `math`, `datetime`, `collections`, `itertools`,
  `base64` and `dataclasses` are available; `statistics` and `csv` are not.
- Return the answer with `print(...)` or by ending the code with an expression.
- Each call starts fresh: variables do not survive between calls. Execution time and
  memory are limited."""

EXECUTE_CODE_DESCRIPTION = (
    "Run a Python snippet in an isolated compute sandbox (no files, no network, no other "
    "tools) for exact calculation, dates, data shaping and regular expressions. Returns what "
    "the code printed and the value of its final expression."
)


def codeact_instructions() -> str:
    """The rendered ``<tool-guide name="code_act">`` block."""
    return render_capability_guidance([ToolGuidance(CODEACT_GUIDANCE_NAME, CODEACT_INSTRUCTION)])


__all__ = [
    "CODEACT_GUIDANCE_NAME",
    "CODEACT_INSTRUCTION",
    "EXECUTE_CODE_DESCRIPTION",
    "codeact_instructions",
]
