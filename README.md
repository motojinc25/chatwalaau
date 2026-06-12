# ChatWalaʻau

**The localhost AI Agent Runtime** -- Chat UI, Tools, RAG, and MCP in one `pip install`

[![PyPI](https://img.shields.io/pypi/v/chatwalaau)](https://pypi.org/project/chatwalaau/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE.md)
[![Python](https://img.shields.io/pypi/pyversions/chatwalaau)](https://pypi.org/project/chatwalaau/)

ChatWalaʻau is a **full-stack AI agent runtime** that runs entirely on localhost. It connects a modern chat UI to AI agents via the AG-UI protocol, with built-in tools, a RAG pipeline, MCP integration, and an OpenAI-compatible API -- all from a single `pip install`.

> Hawaii-built, powered by [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)

📖 **Full documentation & guides: [chatwalaau.com](https://www.chatwalaau.com)**

---

## Quick Start

```bash
pip install chatwalaau
chatwalaau init
# Edit .env and set AZURE_OPENAI_ENDPOINT
az login
chatwalaau
```

Open: [http://localhost:8000/chat](http://localhost:8000/chat)

> Behind a corporate TLS-intercepting proxy? Install with `pip install "chatwalaau[corp]"`
> so Python trusts your OS certificate store. See the
> [Installation guide](https://www.chatwalaau.com/docs/getting-started/installation).

---

## Highlights

- **Modern chat UI** -- Markdown, code, math (KaTeX), Mermaid, reasoning blocks, web search with citations, voice in/out, image analysis, and Temporary Chat
- **Agent tools** -- image generation + mask editor, weather, coding tools with an approval workflow, prompt templates, and Agent Skills
- **Models** -- switch between **Azure OpenAI** and **Anthropic (Claude)** mid-conversation, with per-message reasoning effort and provider-agnostic **prompt caching** that cuts input-token cost on long/coding turns (on by default, output-transparent)
- **Knowledge** -- RAG over your PDFs (ChromaDB) and background batch jobs with a live dashboard
- **MCP native** -- connect any MCP server (Claude Desktop-compatible config); MCP Apps render interactive UI in chat
- **Memory** -- a configurable Agent Identity and a self-maintaining User Preference Memory that the agent curates inline and (opt-in) reconciles in the background, superseding stale or conflicting preferences instead of just piling them up
- **OpenAI-compatible API** -- expose the agent as `/v1/responses` for any OpenAI-SDK app
- **Yours, local-first** -- file-based sessions, vectors, and uploads stay on your machine; unified API-key auth and an optional web sign-in for LAN/cloud

See the [Features documentation](https://www.chatwalaau.com/docs/features/chat-and-ui) for the full list and configuration.

---

## UI Preview

<p align="center">
<img src="assets/images/screenshot1.png">
<img src="assets/images/screenshot2.png">
<img src="assets/images/screenshot3.png">
</p>
<p align="center">
<sub>Weather Tools | Mermaid Diagrams | Image Analysis</sub>
</p>
<p align="center">
<img src="assets/images/screenshot4.png">
<img src="assets/images/screenshot5.png">
<img src="assets/images/screenshot6.png">
</p>
<p align="center">
<sub>DevUI | Search Session | Image Generation</sub>
</p>

---

## About the Name

"Walaʻau" (wah-la-OW) is a Hawaiian word meaning "to chat, talk, or converse." We chose it because it captures what the agent does, in the language of the place where the project is built. Hawaiian (ʻōlelo Hawaiʻi) is an indigenous language now in active revitalization; we use this word with respect and gratitude.

---

## Documentation

Everything -- installation, configuration, every feature, the API, the CLI, and deployment -- lives on the documentation site:

- **Getting started:** [Installation](https://www.chatwalaau.com/docs/getting-started/installation) · [Configuration](https://www.chatwalaau.com/docs/getting-started/configuration)
- **Features:** [Chat & UI](https://www.chatwalaau.com/docs/features/chat-and-ui) · [Agent Tools](https://www.chatwalaau.com/docs/features/agents-and-tools) · [Models & Reasoning](https://www.chatwalaau.com/docs/features/models-and-reasoning) · [Voice](https://www.chatwalaau.com/docs/features/voice-and-speech) · [Knowledge & MCP](https://www.chatwalaau.com/docs/features/knowledge-and-mcp) · [Memory & Sessions](https://www.chatwalaau.com/docs/features/memory-and-sessions)
- **API & CLI:** [OpenAI-compatible API](https://www.chatwalaau.com/docs/api-and-cli/openai-compatible-api) · [Authentication](https://www.chatwalaau.com/docs/api-and-cli/authentication) · [CLI](https://www.chatwalaau.com/docs/api-and-cli/cli)
- **Deployment & Ops:** [Development setup](https://www.chatwalaau.com/docs/deployment/development) · [Networking & Ops](https://www.chatwalaau.com/docs/deployment/operations)

Documentation is available in English and 日本語.

---

## Development

```bash
# Backend
cd backend && cp .env.sample .env   # set AZURE_OPENAI_ENDPOINT
uv sync --prerelease=allow
uv run uvicorn app.main:app --reload --app-dir src   # http://localhost:8000

# Frontend (separate terminal)
cd frontend && pnpm install && pnpm dev               # http://localhost:5173
```

Full prerequisites, Azure credential lanes, and the production build are in the
[Development setup guide](https://www.chatwalaau.com/docs/deployment/development).

---

## Supported Platforms

Windows 10/11 · macOS (Intel / Apple Silicon) · Linux (Ubuntu, Debian, etc.)

## License

[Apache-2.0](LICENSE.md)
