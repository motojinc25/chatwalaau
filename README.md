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
chatwalaau init        # writes a .env for you to edit (and can set up your first model)
```

> **Setting up models (v0.107.0+):** chat models are configured **exclusively** through the
> Model Offering Catalog (`model_offerings.jsonc`). Run `chatwalaau init` for a guided
> first-model step, `chatwalaau models add` any time to author the file, or use the in-app
> **Model Settings** screen (changes apply without a restart). The legacy per-provider model
> environment variables (`AZURE_OPENAI_MODELS`, `ANTHROPIC_MODELS`, `OPENAI_MODELS`,
> `FOUNDRY_MODELS`, `MODEL_MAX_CONTEXT_TOKENS`, `ANTHROPIC_HOSTING`, ...) have been
> **removed**. As of **v0.108.0**, image generation and RAG embeddings are configured the same
> way -- add an offering with `operations: ["image"]` or `["embeddings"]` (the former
> `IMAGE_DEPLOYMENT_NAME` / `EMBEDDING_DEPLOYMENT_NAME` / `IMAGE_*` variables are removed). See the
> [model configuration docs](https://chatwalaau.com/docs/features/models-and-reasoning).

Set your Azure endpoint / credentials in `.env` (shared with image, RAG, and speech):

```ini
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<your-key>      # or authenticate with Entra ID instead (see below)
# ANTHROPIC_API_KEY / OPENAI_API_KEY as needed -- referenced by NAME from the catalog
```

Then author **at least one chat model** in `model_offerings.jsonc` (or run
`chatwalaau models add`). Example spanning several providers:

```jsonc
{
  "offerings": [
    { "id": "gpt-5.5", "provider": "azure-openai", "model_ref": "gpt-5.5",
      "endpoint": "${AZURE_OPENAI_ENDPOINT}", "default": true, "context_window": 1050000 },
    { "id": "claude-fable-5", "provider": "anthropic", "hosting": "direct",
      "model_ref": "claude-fable-5", "api_key_env": "ANTHROPIC_API_KEY" },
    { "id": "gpt-5.1", "provider": "openai", "model_ref": "gpt-5.1", "api_key_env": "OPENAI_API_KEY" },
    { "id": "deepseek-v4-pro", "provider": "foundry", "model_ref": "deepseek-v4-pro",
      "endpoint": "https://<resource>.services.ai.azure.com/api/projects/<project>" }
  ]
}
```

Each offering self-describes its provider, `model_ref` (the connector's real
model/deployment name), optional `endpoint` / `base_url` / `hosting` / `context_window`,
and references any API key by env-var NAME via `api_key_env` (secrets stay in `.env`). An
azure-openai offering may omit `endpoint`/`api_key_env` to reuse the shared Azure lanes above.

Then start the server:

```bash
chatwalaau
```

Open: [http://localhost:8000/chat](http://localhost:8000/chat)

> **Azure authentication options.** An API key (`AZURE_OPENAI_API_KEY`) is the
> quickest and always takes precedence -- set it and you do **not** need `az login`.
> To use Microsoft Entra ID instead, leave the key unset and pick a credential lane
> with `AZURE_CREDENTIAL_MODE`: `cli` (default -- `az login` for local dev),
> `managed-identity` (Azure App Service / Container Apps / AKS / Functions / VM), or
> `default` (auto-discovery). Anthropic uses `ANTHROPIC_API_KEY` for `direct`
> hosting, or Entra ID for `foundry` hosting. Microsoft Foundry is Entra ID only
> (it reuses the same credential lanes; no API key exists). See the
> [Authentication guide](https://www.chatwalaau.com/docs/api-and-cli/authentication).

> Behind a corporate TLS-intercepting proxy? Install with `pip install "chatwalaau[corp]"`
> so Python trusts your OS certificate store. See the
> [Installation guide](https://www.chatwalaau.com/docs/getting-started/installation).

---

## Highlights

- **Modern chat UI** -- Markdown, code, math (KaTeX), Mermaid, reasoning blocks, web search with citations, voice in/out, image analysis, **attach a PDF and the assistant reads it directly** (given to the model as context, like an image -- natively on Azure OpenAI / OpenAI / Foundry, or as extracted text on other providers), a built-in **paint canvas** (draw, paste, or load an image from your device **or the coding workspace**, attach, and re-edit), Temporary Chat, **message-by-message navigation** (previous/next step buttons that walk the conversation one message at a time), **slash commands** (`/help`, `/prompt`, `/skill`, `/model`) with completion and dynamic arguments, and a **compact chat sidebar** that collapses by section and loads hundreds of conversations as you scroll
- **Agent tools** -- image generation + mask editor, weather, coding tools with an approval workflow (a per-turn round counter, a configurable round budget, and "approve for this session" that stops counting against the budget and clears the other pending cards of that tool), prompt templates, and Agent Skills (enable/disable or hot-reload from disk at runtime)
- **Models** -- switch between **Azure OpenAI**, **Anthropic (Claude)**, **OpenAI**, and **Microsoft Foundry** mid-conversation, with per-message generation options (reasoning effort and, on gpt-5.x, verbosity), **structured output** (constrain the answer to JSON / a JSON Schema), and provider-agnostic **prompt caching** that cuts input-token cost on long/coding turns (on by default, output-transparent); as of **v0.107.0** models are configured **exclusively** through the **Model Offering Catalog** (`model_offerings.jsonc`) -- **compose the served models** (multi-provider and gateway offerings) from the CLI (`chatwalaau models add`) or the in-app **Model Settings** screen, **drag to set the order they appear in the selector**, and changes apply live without a restart (the legacy per-provider `*_MODELS` env vars have been removed); as of **v0.108.0** the **image generation** and **RAG embedding** models are configured the same way -- add an offering with `operations: ["image"]` (with optional `image_defaults`) or `["embeddings"]`, and the former `IMAGE_DEPLOYMENT_NAME` / `EMBEDDING_DEPLOYMENT_NAME` / `IMAGE_*` env vars are removed; as of **v0.109.0** the per-task **helper models** (chat title, user-/agent-memory, Teams meeting summary, ontology NL-to-SPARQL) are assigned in the catalog too under **Task model assignments** (a `roles` block, the Model Settings screen, or `chatwalaau models role set`) -- each points at one of your chat offerings so it routes to the right provider, and the former `SESSION_TITLE_MODEL` / `USER_MEMORY_EXTRACTION_MODEL` / `AGENT_MEMORY_CURATION_MODEL` / `TEAMS_MEETING_SUMMARY_MODEL` / `ONTOLOGY_NL_MODEL` env vars are removed
- **Knowledge** -- RAG over your PDFs (ChromaDB), ingested by the built-in **Pipeline Jobs** engine: submit/monitor/cancel jobs from a portal, the API, or the agent (reference an uploaded PDF by its **filename**), with live progress and run history (on by default)
- **Ontology** -- design **concept models as RDF knowledge graphs** on a visual node canvas: circular entities (emoji, colors, typed properties with **key attributes**) connect from **anywhere on the node's ring (360°)** with directional, cardinality-labeled relationships that **fan out when parallel** so each is individually selectable, and clicking a node or edge lights up its whole in/out neighborhood; **rename** ontologies in place, search with **SPARQL or natural language** with on-canvas highlighting, import/export standard RDF with automatic backups, and let the agent **answer from your ontologies in any chat** (opt-in via `ONTOLOGY_ENABLED`)
- **MCP native** -- connect any MCP server (Claude Desktop-compatible config); enable/disable servers and individual tools at runtime to control token usage, or hot-reload the config (reconnect) without a restart; MCP Apps render interactive UI in chat
- **Memory** -- a configurable Agent Identity, a self-maintaining User Preference Memory (about you), and an Agent Memory (about the work -- project conventions, tool quirks, operating rules) that the agent curates inline and you can grow by giving any chat turn a thumbs-up to "remember this turn"; a built-in **Memory editor** lets you view and edit all three files (`IDENTITY.md` / `USER.md` / `MEMORY.md`) in a Markdown editor with automatic timestamped backups
- **Scheduled execution** -- a built-in **Cron Scheduler** runs workspace scripts on a cron expression, an interval, or once after a delay; manage jobs from a portal, the API, or the agent (opt-in via `CRON_ENABLED`)
- **File Explorer** -- a built-in VSCode-style **file tree + monaco editor** to browse and hand-edit files in your coding workspace, with tabs, create/rename/delete, drag-to-move, **upload files & folders (multiple, with an overall-progress bar)**, **file/folder download (ZIP)**, **PDF & image preview with zoom** (with **Attach to chat** to hand an open image/PDF to the composer), and a **split editor** (drag tabs between panes) (opt-in via `FILE_EXPLORER_ENABLED`)
- **Microsoft Teams** -- talk to the agent from a Teams personal chat, group chat, or channel (Bot Framework JWT auth, typing indicator, Adaptive Card tool approval, Entra Object-ID allow-list; opt-in via `TEAMS_ENABLED`)
- **Declarative agents** -- define an agent (persona, model selection, output policy) in a YAML file and switch the active agent at runtime from the web app; the built-in CORE agent reproduces the default behavior (opt-in custom agents via `DECLARATIVE_AGENTS_DIR`)
- **Inbound webhooks** -- drive the agent from external events via a **Webhook Gateway** with a management portal; the first source is **Microsoft Graph**, which auto-summarizes **Teams meeting transcripts** into the workspace (opt-in via `WEBHOOK_ENABLED`), or summarize a meeting you organized on demand by signing in yourself (device-code, no service principal or admin policy); the portal shows **live** Graph subscriptions, offers **delete-and-re-subscribe** when a subscription already exists, and surfaces the **auto-renewal schedule**
- **OpenAI-compatible API** -- expose the agent as `/v1/responses` for any OpenAI-SDK app
- **Survives a restart** -- deploying or restarting the backend no longer signs anyone out, and a message you were typing is never thrown away: if the server is unreachable your text comes back to the input box with a **Retry** button, and an expired sign-in opens a dialog **over your intact chat** instead of reloading the page. If the API server is not up yet, the app **says so** and waits for it, instead of rendering a chat page where nothing works. Sessions persist as SHA-256 digests only -- never the token itself (`AUTH_SESSION_PERSIST`, on by default)
- **Yours, local-first** -- file-based sessions, vectors, and uploads stay on your machine; unified API-key auth and an optional web sign-in for LAN/cloud; **uploaded and generated images are served behind your session** (a raw image URL no longer opens for anyone who is not signed in -- when web login is on, even on `localhost` and behind a dev/reverse proxy); an opt-in **Prompt Dump** (`PROMPT_DUMP_ENABLED`) writes the exact flowing prompt per run to a folder for debugging

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
- **Features:** [Chat & UI](https://www.chatwalaau.com/docs/features/chat-and-ui) · [Slash Commands](https://www.chatwalaau.com/docs/features/slash-commands) · [Agent Tools](https://www.chatwalaau.com/docs/features/agents-and-tools) · [Models & Reasoning](https://www.chatwalaau.com/docs/features/models-and-reasoning) · [Voice](https://www.chatwalaau.com/docs/features/voice-and-speech) · [Knowledge & MCP](https://www.chatwalaau.com/docs/features/knowledge-and-mcp) · [Memory & Sessions](https://www.chatwalaau.com/docs/features/memory-and-sessions) · [Declarative Agents](https://www.chatwalaau.com/docs/features/declarative-agents)
- **API & CLI:** [OpenAI-compatible API](https://www.chatwalaau.com/docs/api-and-cli/openai-compatible-api) · [Authentication](https://www.chatwalaau.com/docs/api-and-cli/authentication) · [CLI](https://www.chatwalaau.com/docs/api-and-cli/cli)
- **Deployment & Ops:** [Development setup](https://www.chatwalaau.com/docs/deployment/development) · [Networking & Ops](https://www.chatwalaau.com/docs/deployment/operations)

Documentation is available in English and 日本語, with **full-text search** (including
Japanese) built into the site.

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
