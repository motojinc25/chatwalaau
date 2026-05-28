"""Keyword-routed demo scenarios (PRP-0066, UDR-0041).

`DemoChatClient` (see chat_client.py) reads the latest user message,
classifies it, and replays a scripted scenario from this table. The
scripts cover every UI surface the demo needs to exercise:

- ``greeting`` / ``default``: a markdown showcase covering headings,
  lists, bold/italic, links, blockquote, table, KaTeX math, Mermaid,
  and a syntax-highlighted code block.
- ``reasoning``: a text_reasoning preamble + text body so the
  reasoning-block UI (CTR-0017) is exercised.
- ``weather``: triggers the live Weather tool (Open-Meteo, free) by
  emitting a ``get_coords_by_city`` function_call. After the Agent
  fans out to the tool loop, the model is invoked again -- the demo
  client then emits a short text summary instead of a second tool
  call.
- ``image``: triggers ``generate_image`` (which routes to the demo
  image provider when DEMO_MODE=true).
- ``rag`` / ``search documents``: triggers ``rag_search`` which queries
  ChromaDB with the bundled demo corpus.
- ``model``: when the user asks "which model are you" the client
  echoes back the model name from ``DemoChatClient(model=...)``.

The scripts are intentionally deterministic so two browser windows
side by side render the same output (modulo the per-token jitter from
DEMO_LATENCY_MS).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ScenarioKind = Literal[
    "greeting",
    "markdown",
    "mermaid",
    "code",
    "math",
    "reasoning",
    "weather",
    "image",
    "rag",
    "model",
    "default",
]


@dataclass(frozen=True)
class FunctionCallStep:
    """Emit a function_call event so the Agent re-enters its tool loop."""

    name: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ReasoningStep:
    """Emit a text_reasoning span before the body."""

    text: str


@dataclass(frozen=True)
class TextStep:
    """Emit a body text response."""

    text: str


Step = TextStep | ReasoningStep | FunctionCallStep


def classify(message: str, *, has_prior_tool_result: bool = False) -> ScenarioKind:
    """Classify the most recent user message into a scenario kind.

    ``has_prior_tool_result`` is true when the Agent re-invokes the
    client after a tool call returned, so the second pass replies in
    plain text instead of looping into another function_call.
    """
    text = (message or "").lower()

    if has_prior_tool_result:
        # Second pass after a tool result -- always answer in prose.
        return "default"

    if not text.strip():
        return "greeting"

    # Order matters: more-specific keywords win.
    if any(k in text for k in ("weather", "天気", "기온", "tokyo", "honolulu", "osaka")):
        return "weather"
    if any(k in text for k in ("image", "picture", "draw", "generate a", "画像", "그림")):
        return "image"
    if any(
        k in text
        for k in (
            "rag",
            "search documents",
            "search the docs",
            "search the document",
            "documents",
            "lookup",
            "knowledge base",
            "ドキュメント",
        )
    ):
        return "rag"
    if any(k in text for k in ("think step", "reason", "explain why", "なぜ", "理由")):
        return "reasoning"
    if any(k in text for k in ("which model", "what model", "model are you", "どのモデル")):
        return "model"
    if any(k in text for k in ("mermaid", "diagram", "sequence", "flowchart")):
        return "mermaid"
    if any(k in text for k in ("code", "python", "javascript", "syntax")):
        return "code"
    if any(k in text for k in ("math", "formula", "equation", "katex", "latex")):
        return "math"
    if any(k in text for k in ("hello", "hi", "greet", "こんにちは", "안녕")):
        return "greeting"
    if "markdown" in text:
        return "markdown"
    return "default"


def _extract_city(message: str) -> str:
    """Heuristic: pull a city name out of a weather query.

    Demo-only; production code never reads this.
    """
    text = (message or "").strip()
    for city in (
        "Tokyo",
        "Osaka",
        "Kyoto",
        "Honolulu",
        "San Francisco",
        "New York",
        "London",
        "Paris",
        "Berlin",
        "Seoul",
    ):
        if city.lower() in text.lower():
            return city
    return "Tokyo"


# ---- Scripts ----

_GREETING_TEXT = (
    "Hello! I'm a **ChatWalaʻau demo agent**. I'm running entirely offline "
    "with no LLM API calls, so every reply you see is scripted rather than "
    "generated.\n\n"
    "Try these prompts to exercise the UI:\n\n"
    '- **Markdown**: "show me a markdown sample"\n'
    '- **Mermaid diagram**: "draw a sequence diagram"\n'
    '- **KaTeX math**: "show a math formula"\n'
    '- **Code blocks**: "write some Python code"\n'
    '- **Reasoning blocks**: "reason about this problem"\n'
    '- **Weather tool** (live, Open-Meteo): "what\'s the weather in Tokyo?"\n'
    '- **Image generation** (demo placeholder): "generate an image of a cat"\n'
    '- **RAG search** (demo corpus): "search the documents for ChatWalaʻau"\n'
)

_MARKDOWN_TEXT = (
    "# Markdown showcase\n\n"
    "## Headings and emphasis\n\n"
    "**Bold**, *italic*, `inline code`, and a [link](https://example.com).\n\n"
    "## Lists\n\n"
    "- Apples\n- Oranges\n- Mangoes\n\n"
    "1. First\n2. Second\n3. Third\n\n"
    "## Blockquote\n\n"
    '> ChatWalaʻau means "to chat" in Hawaiian.\n\n'
    "## Table\n\n"
    "| Feature | Live | Demo |\n"
    "|---|---|---|\n"
    "| Chat | Azure OpenAI | DemoChatClient |\n"
    "| Weather | Open-Meteo | Open-Meteo |\n"
    "| Image | gpt-image-1.5 | placeholder PNG |\n"
    "| TTS | ElevenLabs | bundled MP3 |\n\n"
    "## Horizontal rule\n\n"
    "---\n\n"
    "That's it!"
)

_MERMAID_TEXT = (
    "Here is a sequence diagram of the AG-UI streaming flow:\n\n"
    "```mermaid\n"
    "sequenceDiagram\n"
    "    participant Browser\n"
    "    participant FastAPI\n"
    "    participant Agent\n"
    "    participant DemoClient\n"
    "    Browser->>FastAPI: POST /ag-ui/\n"
    "    FastAPI->>Agent: agent.run_stream(messages)\n"
    "    Agent->>DemoClient: get_response(stream=True)\n"
    "    DemoClient-->>Agent: ChatResponseUpdate(text delta)\n"
    "    Agent-->>FastAPI: TEXT_MESSAGE_CONTENT (SSE)\n"
    "    FastAPI-->>Browser: SSE event stream\n"
    "```\n\n"
    "And a flowchart of the demo dispatch:\n\n"
    "```mermaid\n"
    "flowchart LR\n"
    "    A[Request] --> B{DEMO_MODE?}\n"
    "    B -->|true| C[DemoChatClient]\n"
    "    B -->|false| D[OpenAIChatClient]\n"
    "    C --> E[Scripted response]\n"
    "    D --> F[Azure OpenAI]\n"
    "```"
)

_CODE_TEXT = (
    "Here is a small Python sample that uses ChatWalaʻau's OpenAI-compatible "
    "REST API:\n\n"
    "```python\n"
    "from openai import OpenAI\n\n"
    "client = OpenAI(\n"
    '    base_url="http://localhost:8000/v1",\n'
    '    api_key="sk-chatwalaau-your-secret",\n'
    ")\n\n"
    "response = client.responses.create(\n"
    '    model="chatwalaau-demo",\n'
    '    input="Hello from the demo!",\n'
    ")\n"
    "print(response.output_text)\n"
    "```\n\n"
    "Equivalent JavaScript:\n\n"
    "```javascript\n"
    "const res = await fetch('http://localhost:8000/v1/responses', {\n"
    "  method: 'POST',\n"
    "  headers: {\n"
    "    'Content-Type': 'application/json',\n"
    "    Authorization: 'Bearer sk-chatwalaau-your-secret',\n"
    "  },\n"
    "  body: JSON.stringify({ model: 'chatwalaau-demo', input: 'Hello' }),\n"
    "});\n"
    "console.log(await res.json());\n"
    "```"
)

_MATH_TEXT = (
    "Here are some KaTeX-rendered formulas:\n\n"
    "Inline math: the Pythagorean theorem $a^2 + b^2 = c^2$.\n\n"
    "Block math:\n\n"
    "$$\n"
    "e^{i\\pi} + 1 = 0\n"
    "$$\n\n"
    "And a matrix:\n\n"
    "$$\n"
    "A = \\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}\n"
    "$$\n"
)

_REASONING_PREAMBLE = (
    "Let me think about this step by step.\n\n"
    "First, I'll consider what kind of question this is. The user appears "
    "to want a worked explanation, so I'll structure the response with "
    "an explicit chain of reasoning followed by a concise conclusion. "
    "This pattern works well for analytical questions where the user "
    "wants to follow the thinking, not just the final answer."
)

_REASONING_BODY = (
    "Here's the conclusion:\n\n"
    "When you ask a reasoning model a question, the SPA renders the "
    "intermediate thinking in a **collapsible block above the body**, "
    "and the actual answer appears below. In this demo, both halves are "
    "scripted -- no LLM was consulted -- but the UI surface is exactly "
    "the same as production."
)

_RAG_INTRO = "I'll search the bundled demo corpus for relevant passages."

_RAG_SECOND_PASS = (
    "Based on the demo RAG corpus, ChatWalaʻau is an open-source "
    "AI agent runtime distributed via PyPI. Key points from the citations:\n\n"
    "- It uses Microsoft Agent Framework for the agent layer.\n"
    "- AG-UI protocol drives the frontend over SSE.\n"
    "- Demo Mode (this mode!) replaces metered providers with offline "
    "deterministic dummies for cost-zero cloud demos.\n\n"
    "Citations appear inline above with `[source: filename, page N]` formatting."
)

_WEATHER_INTRO = (
    "I'll look that up using the Open-Meteo weather tool (this is the "
    "ONE live external API in demo mode -- Open-Meteo is free and "
    "key-less)."
)

_WEATHER_SECOND_PASS_TEMPLATE = (
    "The Open-Meteo result is shown above as a card widget. Notice that "
    "in demo mode the weather data is **real** -- everything else "
    "(LLM, TTS, STT, image gen, embeddings) is scripted or hash-based, "
    "but Open-Meteo stays live because it's free and requires no key. "
    'This is the demo\'s one "real" anchor (UDR-0041 D7).'
)

_IMAGE_INTRO = (
    "I'll generate that image for you. (In demo mode, the result is a "
    "bundled placeholder PNG -- no Azure OpenAI Images API call is made.)"
)

_IMAGE_SECOND_PASS = (
    "Above is the generated image. In demo mode, `generate_image` returns "
    "one of two bundled 1024x1024 placeholder PNGs, rotating "
    "deterministically per call. The full UI surface (inline render, "
    "click-to-open full-size, ImageGenerationResult component) is "
    "exercised end-to-end with zero outbound traffic."
)


def _model_text(model: str) -> str:
    return (
        f"I'm the **ChatWalaʻau demo agent** running on model `{model}`.\n\n"
        "In demo mode the model name only affects which entry of "
        "`DEMO_MODELS` is selected from the dropdown -- there is no real "
        "LLM behind any of these names. Switch model from the selector "
        "to see the registry routing in action."
    )


_DEFAULT_TEXT = (
    "I'm running in **demo mode**, so my replies are scripted rather than "
    "generated by an LLM. Your message has been received -- try one of the "
    'shortcut prompts ("weather in Tokyo", "generate an image of a cat", '
    '"search the documents", "reason about this", "show markdown", or '
    '"draw a diagram") to see the corresponding UI surface in action.\n\n'
    "Every external paid API in this demo is replaced by an in-process "
    "deterministic dummy. The only live external call is Open-Meteo for "
    "the Weather tool (it's free and requires no API key)."
)


def build_script(
    kind: ScenarioKind,
    *,
    model: str,
    user_text: str,
) -> list[Step]:
    """Return the ordered list of steps for a scenario."""
    if kind == "greeting":
        return [TextStep(_GREETING_TEXT)]
    if kind == "markdown":
        return [TextStep(_MARKDOWN_TEXT)]
    if kind == "mermaid":
        return [TextStep(_MERMAID_TEXT)]
    if kind == "code":
        return [TextStep(_CODE_TEXT)]
    if kind == "math":
        return [TextStep(_MATH_TEXT)]
    if kind == "reasoning":
        return [ReasoningStep(_REASONING_PREAMBLE), TextStep(_REASONING_BODY)]
    if kind == "model":
        return [TextStep(_model_text(model))]
    if kind == "weather":
        city = _extract_city(user_text)
        return [
            TextStep(_WEATHER_INTRO),
            FunctionCallStep(name="get_coords_by_city", arguments={"city": city}),
        ]
    if kind == "image":
        return [
            TextStep(_IMAGE_INTRO),
            FunctionCallStep(
                name="generate_image",
                arguments={"prompt": user_text or "a cute cat in pixel art style"},
            ),
        ]
    if kind == "rag":
        return [
            TextStep(_RAG_INTRO),
            FunctionCallStep(name="rag_search", arguments={"query": user_text or "ChatWalaʻau"}),
        ]
    return [TextStep(_DEFAULT_TEXT)]


def second_pass_text(kind: ScenarioKind) -> str | None:
    """Return the prose follow-up after a tool result (or None for plain scenarios)."""
    if kind == "weather":
        return _WEATHER_SECOND_PASS_TEMPLATE
    if kind == "image":
        return _IMAGE_SECOND_PASS
    if kind == "rag":
        return _RAG_SECOND_PASS
    return None


__all__ = [
    "FunctionCallStep",
    "ReasoningStep",
    "ScenarioKind",
    "Step",
    "TextStep",
    "build_script",
    "classify",
    "second_pass_text",
]
