# ChatWalaʻau

**The localhost AI Agent Runtime** -- Chat UI, Tools, RAG, and MCP in one `pip install`

[![PyPI](https://img.shields.io/pypi/v/chatwalaau)](https://pypi.org/project/chatwalaau/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE.md)
[![Python](https://img.shields.io/pypi/pyversions/chatwalaau)](https://pypi.org/project/chatwalaau/)

ChatWalaʻau is a **full-stack AI agent runtime** that runs entirely on localhost. It connects a modern chat UI to AI agents via the AG-UI protocol, with built-in tools, RAG pipeline, MCP integration, and an OpenAI-compatible API -- all from a single `pip install`.

## About the Name

"Walaʻau" (wah-la-OW) is a Hawaiian word meaning "to chat, talk, or
converse." We chose it because it captures what the agent does, in the
language of the place where the project is built.

Hawaiian (ʻōlelo Hawaiʻi) is an indigenous language of the Hawaiian
Islands. After a long period of suppression, it is now in active
revitalization. We use this word with respect and gratitude.

### Why ChatWalaʻau?

- **One command, full stack** -- `pip install chatwalaau` gives you Chat UI + Agent Runtime + Tools + RAG. No Docker, no cloud setup.
- **MCP native** -- Zero-config first run via bundled defaults. Connect any MCP server. MCP Apps render interactive UI in chat.
- **Your data stays local** -- File-based sessions, ChromaDB vectors, and uploads never leave your machine.
- **OpenAI-compatible API** -- Expose your agent as `/v1/responses` for any app using the OpenAI SDK.

> Hawaii-built, powered by [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)

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

## Quick Start

```bash
pip install chatwalaau
chatwalaau init
# Edit .env and set AZURE_OPENAI_ENDPOINT
az login
chatwalaau
```

Open: [http://localhost:8000/chat](http://localhost:8000/chat)

> **On a corporate network (TLS-intercepting proxy / in-house root CA)?**
> Install with the `corp` extras so Python honours your OS certificate
> store instead of the bundled `certifi`:
>
> ```bash
> pip install "chatwalaau[corp]"
> ```
>
> Adds `pip-system-certs`. Default installs are unaffected.

---

## Features

### Chat & UI

- Chat with AI agents via AG-UI protocol (SSE streaming)
- Rich message rendering: Markdown, code blocks, math (KaTeX), Mermaid diagrams
- LLM reasoning visualization with collapsible thinking blocks
- Web search with inline citation links (OpenAI hosted web search for OpenAI-family models; Anthropic hosted web search for Claude models)
- Voice input via microphone with Whisper transcription (supports `whisper`, `gpt-4o-transcribe`, and `gpt-realtime-whisper` via Realtime API)
- Text-to-Speech playback and download, with a selectable provider: ElevenLabs or Azure OpenAI Realtime voice models (e.g. `gpt-realtime-2`)
- Multimodal image analysis (file attachment, drag-and-drop, URL)
- Temporary Chat: an "incognito-style" throwaway conversation started from the top-right of the chat screen. It does not appear in your history or search, cannot be resumed (closing/leaving/reloading discards it), and runs de-personalized -- the agent uses only its base Identity, never reads or writes your saved preferences (the built-in learning loop is untouched), while all other tools stay available. The input turns dark while active. For safety it is briefly retained server-side (`TEMPORARY_CHAT_RETENTION_DAYS`, default 30) then auto-deleted -- "not in your history", not "never stored". Also available on the API via an opt-in `temporary` flag (default off)
- Session management: save, search, pin, archive, fork, rename
- Conversation navigator: a floating right-edge rail of your questions in long chats; click to open a shortcut list and jump straight to any of your messages (shown on the full-page chat when the window is wide enough)
- Context window consumption display with warning levels
- Per-turn token usage display
- Three layout scenarios: Chat, Popup, Sidebar
- Multilingual chat with browser auto-translation suppressed
- Smooth large sessions: opening a long conversation and continuing to chat stays responsive -- only the streaming message re-renders, so scrolling and typing no longer slow down as the message count grows

### Agent Tools

- Image generation, editing, and Canvas mask editor via Azure OpenAI gpt-image-1.5
- Weather tools with rich card widgets (Open-Meteo, no API key)
- Coding tools (file read/write, shell execution, file search)
- Tool approval workflow: destructive coding tools (`bash_execute`, `file_write`) pause for an inline Approve / Reject decision by default; one env var (`TOOL_APPROVAL_MODE=skip`) restores autonomous execution
- Prompt Templates: save, manage, and insert reusable prompts from "+" menu and message actions
- Agent Skills: portable domain knowledge packages with progressive disclosure

### Platform

- Global Agent Identity: a single `.agent/IDENTITY.md` file defines the agent's persona, tone, and base posture for every project and conversation -- it is the first block of the system prompt. Edit it to rebrand the assistant; delete it and the runtime regenerates a sensible default on next start. No env var, fixed path, and read-only hosts (containers) fall back to the built-in default instead of failing to boot
- User Preference Memory: the agent keeps a small, durable profile of how you like to be helped (preferences, communication style, expectations, workflow habits) in a single `.agent/USER.md` file, rendered into the system prompt right after the Identity. It maintains the file itself during chats via an inline memory tool (add / replace / remove / consolidate); a deterministic filter blocks secrets and sensitive personal data from ever being stored, and the file is kept small (`USER_CHAR_LIMIT`, default 1375). Each session reads a frozen snapshot taken at session start -- so prompts stay cache-stable and consistent across reloads, and anything the agent learns mid-session takes effect from your next session. On by default; set `USER_PROFILE_ENABLED=false` to turn it off (fixed path, read-only hosts fall back to a built-in default)
- MCP Integration: connect external tools via Model Context Protocol (Claude Desktop-compatible config)
- MCP Apps: interactive UI rendered in sandboxed iframes for MCP tools with `_meta.ui` resources
- RAG Pipeline: PDF ingestion with ChromaDB vector search, Azure OpenAI embedding, and source citations
- Batch Processing: async job queue via Core MCP Server with real-time MCP Apps dashboard
- Multi-provider, multi-model switching: switch between **Azure OpenAI** and **Anthropic (Claude)** models mid-conversation. Add Claude models with `ANTHROPIC_MODELS` and they appear in the existing model selector. Both Anthropic hostings supported: **Direct** (Anthropic public API) and **Foundry** (Anthropic on Azure AI Foundry)
- Per-message reasoning effort: a selector next to the model picker sets how hard the model reasons, per message. Allowed levels and the default follow the selected model and are served by the backend -- Azure OpenAI (`low`/`medium`/`high`/`xhigh`, default `medium`); Anthropic (`low`/`medium`/`high`/`xhigh`/`max`, default `xhigh`, via adaptive thinking). The chosen effort is shown next to the model name and per-turn token usage, and persists with the session
- Session management: save, search, organize into folders, pin, archive, fork, rename
- Sidebar folders: assign a color from a preset palette (set on create or change later via the folder menu), reorder folders by drag-and-drop, and collapse folders by default (open/closed state remembered per device); the folder list self-heals if its saved color/order values are ever corrupted
- Background Responses: long-running agent timeout prevention with stream resumption
- Conversation compaction: long sessions are auto-compacted before each model call (sliding-window by default) so the agent keeps responding instead of failing at the context-window limit; disable with `COMPACTION_STRATEGY=none`
- Context window consumption display with warning levels
- Per-turn token usage display
- OpenAI-compatible API: expose agent as `/v1/responses` endpoint for external apps via OpenAI SDK
- Unified API authentication: single `API_KEY` Bearer token protects `/v1/responses`, every write REST endpoint, and the AG-UI chat stream for non-loopback (LAN) callers; same-machine clients always bypass
- Web SPA authentication (optional): single-user ID/PW login with HttpOnly opaque session cookie for cloud-deployed instances; coexists with `API_KEY`
- Azure OpenAI credential lane choice: four lanes via one helper -- `AZURE_OPENAI_API_KEY` (api-key bypass, works everywhere) or Entra ID via `AZURE_CREDENTIAL_MODE` = `cli` (default; `AzureCliCredential` / `az login`) / `managed-identity` (`ManagedIdentityCredential` for Azure App Service, Container Apps, AKS, Functions, VM) / `default` (`DefaultAzureCredential` auto-discovery chain). One INFO log line per process announces the active lane at first credential resolution; no key value is ever logged. Cloud deployment guide at `assets/docs/guides/azure-cloud-deploy.md`.
- CLI Client: chat, session/template/model management, TTS, and upload from the command line with local preflight validation for filename, MIME type, and size
- `.env` upgrade tooling: `chatwalaau env diff` reports settings added / removed since your `.env` was generated, and `chatwalaau env sync` re-renders `.env` to the new release's template (keys, comments, order) while preserving your values and keeping a timestamped backup
- HTTPS/TLS support for LAN access with Secure Context (mkcert recommended)
- Multilingual chat with browser auto-translation suppressed
- Three layout scenarios: Chat, Popup, Sidebar

---

## Architecture

The platform connects the UI and agent runtime through the AG-UI protocol.

<p align="center">
<img src="assets/images/diagram1.jpg">
</p>

---

## Development Setup

### Prerequisites

| Tool      | Version | Install                                                                                                            |
| --------- | ------- | ------------------------------------------------------------------------------------------------------------------ |
| Node.js   | 20.19+ / 22.12+ | [https://nodejs.org/](https://nodejs.org/)                                                                 |
| pnpm      | 10+     | `npm install -g pnpm`                                                                                              |
| Python    | 3.12+   | [https://www.python.org/](https://www.python.org/)                                                                 |
| uv        | 0.9+    | [https://docs.astral.sh/uv/](https://docs.astral.sh/uv/)                                                           |
| Azure CLI | 2.x     | [https://learn.microsoft.com/cli/azure/install-azure-cli](https://learn.microsoft.com/cli/azure/install-azure-cli) |

---

### 1. Azure Authentication

The backend supports four Azure OpenAI credential lanes selected by
two environment variables. Pick the one that matches your deployment
surface.

| Lane | When to use | `.env` setting |
| --- | --- | --- |
| **api-key** | First-run / PoC / CI / container; cross-tenant Azure OpenAI | `AZURE_OPENAI_API_KEY=<key>` |
| **cli** (default) | Localhost development with `az login` available | `AZURE_CREDENTIAL_MODE=cli` (or unset) |
| **managed-identity** | Azure App Service, Container Apps, AKS, Functions, VM | `AZURE_CREDENTIAL_MODE=managed-identity` |
| **default** | One image targeting multiple surfaces | `AZURE_CREDENTIAL_MODE=default` |

Precedence: `AZURE_OPENAI_API_KEY` beats `AZURE_CREDENTIAL_MODE`. One
INFO log line per process announces the active lane on first credential
resolution; the key value is never logged.

#### Option A: `az login` (default, localhost)

The backend authenticates via `AzureCliCredential`. Log in before
starting.

```bash
az login
az account set --subscription <subscription-id>
```

#### Option B: `AZURE_OPENAI_API_KEY` (no Azure CLI required)

For first-run / PoC / CI / container scenarios where `az login` is
inconvenient, set the API key from your Azure OpenAI resource in
`.env`:

```
AZURE_OPENAI_API_KEY=<your-api-key>
```

When set, every Azure OpenAI client in the backend (MAF chat, Whisper
STT, image generation, RAG embed / search) authenticates with the key
instead of an Entra ID credential.

#### Option C: Managed Identity (Azure cloud deployments)

For ChatWalaʻau deployed to Azure App Service / Container Apps / AKS /
Functions / VM, assign a Managed Identity to the compute and grant it
the **`Cognitive Services OpenAI User`** role on the Azure OpenAI
resource scope, then set:

```
AZURE_CREDENTIAL_MODE=managed-identity
# AZURE_CLIENT_ID=<user-assigned-MI-client-id>   # only if using user-assigned MI
```

User-assigned identities require `AZURE_CLIENT_ID` per the
`azure-identity` SDK convention. AKS workloads using federated
identity should set `AZURE_CREDENTIAL_MODE=default` instead so the
SDK's `WorkloadIdentityCredential` is picked up automatically.

#### Option D: `default` (DefaultAzureCredential auto-discovery)

For container images that target multiple deployment surfaces
(developer box, staging container, AKS pod) without changing the
`.env`, set:

```
AZURE_CREDENTIAL_MODE=default
```

`DefaultAzureCredential` is constructed with the interactive browser
excluded so headless cloud containers never pop a sign-in window. The
chain order is: `Environment` -> `WorkloadIdentity` ->
`ManagedIdentity` -> `SharedTokenCache` -> `AzureCli` ->
`AzurePowerShell` -> `AzureDeveloperCli`. The `chatwalaau` CLI
suppresses its `az account show` precheck automatically whenever the
active lane is not `cli`.

> See `assets/docs/guides/azure-cloud-deploy.md` for the full cloud
> deployment walk-through (RBAC role assignment, AKS Workload Identity
> setup, troubleshooting).

---

### 2. Backend Setup

**Windows (PowerShell):**

```powershell
cd backend
copy .env.sample .env
# Edit .env and set your Azure OpenAI endpoint
notepad .env
uv sync --prerelease=allow
```

**macOS / Linux:**

```bash
cd backend
cp .env.sample .env
# Edit .env and set your Azure OpenAI endpoint
nano .env
uv sync --prerelease=allow
```

`.env` configuration (required):

```
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_MODELS=gpt-4o
```

---

### 3. Frontend Setup

```bash
cd frontend
pnpm install
```

---

### 4. Start Development Servers

Open two terminals:

**Terminal 1 -- Backend:**

```bash
cd backend
uv run uvicorn app.main:app --reload --app-dir src
```

Backend starts at [http://localhost:8000](http://localhost:8000)

**Terminal 2 -- Frontend:**

```bash
cd frontend
pnpm dev
```

Frontend dev server starts at [http://localhost:5173](http://localhost:5173)
(API requests are proxied to the backend)

---

### 5. Production Build

```bash
cd frontend
pnpm build

cd ../backend
uv run uvicorn app.main:app --app-dir src
```

The backend serves both frontend build artifacts and the API at [http://localhost:8000](http://localhost:8000)

---

## CLI Usage

### Server Commands

```
chatwalaau                                Start the server
chatwalaau init                           Generate .env from template
chatwalaau init --force                   Overwrite existing .env
chatwalaau env diff                       Report .env drift vs the bundled template
chatwalaau env sync                       Re-render .env to the template (dry-run)
chatwalaau env sync --write               Apply (creates a timestamped backup first)
chatwalaau hash-password                  Generate AUTH_PASSWORD_HASH (interactive)
chatwalaau hash-password --stdin --quiet  Generate hash from stdin (scripted)
chatwalaau --host 0.0.0.0                 Bind to all interfaces
chatwalaau --port 9000                    Use custom port
chatwalaau --skip-auth-check              Skip Azure CLI login check
chatwalaau --ssl-certfile cert.pem \
           --ssl-keyfile key.pem          Enable HTTPS (LAN access)
chatwalaau --version                      Show version
```

### Client Commands

Interact with a running ChatWalaʻau instance from the command line. All client commands support `--json` for machine-readable output and `--base-url` / `--api-key` for remote server access.

```bash
# Chat with the agent (single-shot)
chatwalaau chat "What is the weather in Tokyo?"

# Interactive chat (REPL mode)
chatwalaau chat -i

# Chat with specific model and session
chatwalaau chat "hello" -m gpt-4o -s <session-id>

# Session management
chatwalaau sessions list
chatwalaau sessions get <id> --messages
chatwalaau sessions delete <id>
chatwalaau sessions export <id> -o backup.json

# Template management
chatwalaau templates list
chatwalaau templates create -n "Bug Report" -c "Describe the bug..."

# Model info
chatwalaau models list

# Text-to-Speech
chatwalaau tts "Hello world" -o greeting.mp3

# File upload
chatwalaau upload document.pdf -s <session-id>

# JSON output for scripting / agent-to-agent
chatwalaau sessions list --json | jq '.[].thread_id'

# Remote server with HTTPS (self-signed cert)
chatwalaau sessions list --base-url https://192.168.1.10:8000 --no-verify
```

`chatwalaau upload` validates the local file before sending the request: the file must exist, the sanitized filename must remain valid, the MIME type must be one of the supported image formats or PDF, and the size limit must stay within 20MB for images or 50MB for PDFs.

Environment variables for client configuration:

```
CHATWALAAU_URL=http://localhost:8000       # Default server URL
CHATWALAAU_API_KEY=sk-your-key             # Bearer token (reuses API_KEY if not set)
```

---

## Tech Stack

| Layer    | Technology                   | Purpose                        |
| -------- | ---------------------------- | ------------------------------ |
| Frontend | React 19 + TypeScript + Vite | UI framework                   |
| Frontend | Tailwind CSS + shadcn/ui     | Styling + Components           |
| Frontend | Biome                        | Format + Lint                  |
| Backend  | FastAPI + Python 3.12+       | API server                     |
| Backend  | Microsoft Agent Framework    | Agent execution + Tool control |
| Backend  | Ruff                         | Format + Lint                  |
| Package  | uv                           | Python dependency management   |
| Package  | pnpm                         | Node.js dependency management  |

---

## Optional Features

### Anthropic Provider (Claude)

Enable Anthropic Claude models alongside Azure OpenAI -- they appear in the same model selector, can be picked per turn, and per-assistant-message regenerate works across providers. Default state is "Anthropic disabled" so upgrading from 0.65.0 with `ANTHROPIC_MODELS` unset is a no-op.

Two hostings, selected by one variable:

```
# direct (Anthropic public API) | foundry (Anthropic on Azure AI Foundry)
ANTHROPIC_HOSTING=direct
ANTHROPIC_MODELS=claude-sonnet-4-5-20250929,claude-haiku-4-5
```

**Direct hosting** (typical):

```
ANTHROPIC_API_KEY=sk-ant-...
# Optional override; useful for a corporate gateway or proxy
# ANTHROPIC_BASE_URL=https://your-gateway.example.com
```

**Foundry hosting** (Anthropic on Azure AI Foundry).

*Endpoint* -- supply exactly ONE (they are mutually exclusive). `ANTHROPIC_FOUNDRY_RESOURCE` is the Azure AI Services **resource name** (the subdomain) -- **not** the Foundry *project* URL or project name -- and expands to `https://<resource>.services.ai.azure.com/anthropic/`. Use `ANTHROPIC_FOUNDRY_BASE_URL` to give that full URL directly instead.

*Auth* -- pick ONE:

```
ANTHROPIC_HOSTING=foundry
ANTHROPIC_FOUNDRY_RESOURCE=my-aifoundry          # resource name only (subdomain)
# OR: ANTHROPIC_FOUNDRY_BASE_URL=https://my-aifoundry.services.ai.azure.com/anthropic/

# Auth option A -- API key (sent as the api-key header):
ANTHROPIC_FOUNDRY_API_KEY=<foundry-key>

# Auth option B -- Entra ID (Azure CLI / Managed Identity): leave the API key
# EMPTY. The runtime uses the same Azure credential lane as Azure OpenAI
# (AZURE_CREDENTIAL_MODE: cli | managed-identity | default; AZURE_TENANT_ID),
# sending an "Authorization: Bearer <token>" header. Do NOT place a token in
# ANTHROPIC_FOUNDRY_API_KEY -- it is sent verbatim as the api-key header (HTTP 401).
```

**Generation and extended thinking** (Anthropic requires `max_tokens` on every request as a hard output cap):

```
ANTHROPIC_MAX_TOKENS=8192
```

Claude Opus 4.7/4.8 use **adaptive thinking** plus an **effort level** (`output_config.effort`); the deprecated `budget_tokens` mechanism is no longer used. The reasoning effort is chosen **per message** in the UI (default `xhigh`) -- there is no environment variable for it. See [Reasoning effort](#multi-model-switching).

Hosted web search is included for Anthropic models out of the box (uses Anthropic's `web_search_20250305` tool, no extra config required). Every other agent feature -- Weather, Coding tools + Tool Approval, RAG, Image Generation, Vision input, MCP -- works on either provider as long as the chosen model supports tool calling. Speech-to-text, text-to-speech, image generation, and RAG embedding run on their own dedicated Azure models and are independent of the chat provider choice.

---

### Prompt Templates

Save and reuse prompt templates from the chat interface:

```
TEMPLATES_DIR=.templates
```

- Click **+** button > **Use template** to open the management modal
- Create, edit, delete templates with name, category, and body
- **Insert to Chat** pastes the template into the input (editable before send)
- Click the **FileText** icon on any user message to save it as a template

Templates are stored as individual JSON files in the configured directory.

---

### Image Generation

Generate and edit images via Azure OpenAI gpt-image-1.5:

```
IMAGE_DEPLOYMENT_NAME=gpt-image-1.5
```

- **generate_image**: create images from text prompts with configurable size, quality, format, background, and count (1-4)
- **edit_image**: modify existing session images using text prompts (prompt-based)
- **Canvas Mask Editor**: click the **Edit** button on any generated image to open a full-screen mask editor
  - Draw over areas to edit with brush tools (S/M/L), eraser, undo/redo
  - Enter a prompt and click Generate -- the agent edits only the masked region
- Generated images displayed inline in chat with click-to-open full-size
- Images stored in session upload directory and persist across reloads

The agent automatically uses these tools when users request image creation or editing. No opt-in flag needed -- the feature activates when `IMAGE_DEPLOYMENT_NAME` is set.

---

### Coding Tools

Enable AI-powered file operations and shell execution:

```
CODING_ENABLED=true
CODING_WORKSPACE_DIR=C:\path\to\workspace
# Optional: bound file_read output to protect memory + context window
# CODING_FILE_READ_MAX_BYTES=1048576   # 1 MiB default
```

`file_read` now stats the target before reading and caps the output at
`CODING_FILE_READ_MAX_BYTES` (default 1 MiB). When the cap or the line
limit is hit, the response ends with an explicit
`[TRUNCATED BY BYTES: ...]` / `[TRUNCATED BY LIMIT: ...]` marker that
tells the agent how to paginate with `offset=N`.

---

### Tool Approval & Conversation Compaction (Agent Harness)

Two Microsoft Agent Framework harness primitives, configured entirely
via `.env`.

**Tool approval** gates destructive coding tools behind an operator
decision. By default (`TOOL_APPROVAL_MODE=auto`), the agent's first
`bash_execute` or `file_write` call shows an inline approval card with
**Approve / Reject / Approve for this session**; read-only tools
(`file_read`, `file_glob`, `file_grep`) never require approval.

```
# auto (default) -> gate bash_execute + file_write
# always         -> gate every non-read-only tool
# skip           -> disable approval (autonomous, like before)
TOOL_APPROVAL_MODE=auto
TOOL_APPROVAL_REQUIRE_LIST=bash_execute,file_write
TOOL_APPROVAL_TIMEOUT_SEC=300
```

Set `TOOL_APPROVAL_MODE=skip` for the pre-approval "no friction"
behaviour on a trusted single-user machine (analogous to Claude Code's
`--dangerously-skip-permissions`). While skip mode is active the SPA
shows a persistent "Tool approval is DISABLED" banner. Headless lanes
(OpenAI Responses API, the `chatwalaau chat` CLI, and DevUI) auto-approve
every request and log a WARNING per auto-approval.

**Conversation compaction** keeps long sessions within the model's
context budget by compacting in-memory history before each model call
(the on-disk session JSON is never altered):

```
# none | sliding-window (default) | selective-tool-call | tool-result
COMPACTION_STRATEGY=sliding-window
COMPACTION_KEEP_LAST_GROUPS=4
COMPACTION_PRESERVE_SYSTEM=true
```

---

### Text-to-Speech

Enable on-demand TTS for messages. Pick the provider with `TTS_PROVIDER`
(default `elevenlabs`).

Option A -- [ElevenLabs](https://elevenlabs.io/):

```
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=your-api-key
TTS_MODEL_ID=eleven_multilingual_v2
TTS_VOICE_ID=your-voice-id
```

Option B -- Azure OpenAI Realtime voice (e.g. `gpt-realtime-2`), reusing your
existing `AZURE_OPENAI_ENDPOINT` and credentials:

```
TTS_PROVIDER=azure-realtime
TTS_REALTIME_DEPLOYMENT=gpt-realtime-2
TTS_REALTIME_VOICE=alloy
#TTS_REALTIME_AUDIO_RATE=24000
```

Either way the speaker button plays audio and the download button saves an MP3
file; audio is cached to avoid duplicate API calls. The Azure Realtime lane
reads the message text verbatim (it does not converse), matching ElevenLabs.

---

### Agent Skills

Extend the agent with domain knowledge packages ([Agent Skills specification](https://agentskills.io/)):

```
SKILLS_DIR=.skills
```

Place `SKILL.md` files in subdirectories. The agent discovers and loads skills on demand:

```
.skills/
  my-skill/
  +-- SKILL.md          # Required: instructions + metadata
  +-- scripts/          # Optional: executable code
  +-- references/       # Optional: documentation
  +-- assets/           # Optional: templates, resources
```

Skills use progressive disclosure to minimize context window consumption (~100 tokens per skill when idle).

---

### MCP Integration

Connect external tools and services via [Model Context Protocol](https://modelcontextprotocol.io/) using the Claude Desktop-compatible configuration format:

```
# Optional: defaults to mcp_servers.jsonc.
# Set this to an explicit empty value to disable MCP.
# MCP_CONFIG_FILE=mcp_servers.jsonc
```

ChatWalaʻau ships with `backend/mcp_servers.default.jsonc` (git-tracked bundled default). The runtime resolves the operator override `backend/mcp_servers.jsonc` (gitignored) first, then falls back to the bundled default; no manual copy is needed on first run. To customise, create `mcp_servers.jsonc` and edit freely:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/workspace"]
    },
    "remote-api": {
      "url": "https://api.example.com/mcp",
      "headers": { "Authorization": "Bearer token" }
    }
  }
}
```

- **JSONC** : the parser strips `//` and `/* */` comments so operators can annotate entries inline. Strict-JSON contents are accepted unchanged.
- **stdio** servers (with `command`): ChatWalaʻau spawns the process and communicates via stdin/stdout
- **HTTP/SSE** servers (with `url`): ChatWalaʻau connects to a running remote server
- MCP tools appear alongside built-in tools (Weather, Coding, Image Generation)
- Tool calls display with categorized icons: built-in tools have dedicated icons, Skills tools show **BookOpen**/**FileText**, MCP tools show **Plug**
- Server lifecycle managed automatically (startup/shutdown with zombie process prevention)
- **Optional per-server fields** (Claude Desktop-compatible, ignored elsewhere):
  - `"load_prompts": true` -- set when your MCP server implements `prompts/list` (most community servers are tools-only; the default is `false` so filesystem, git, github, etc. connect cleanly out of the box)
  - `"load_tools": false` -- skip the `tools/list` probe for a tools-only server's prompts-only mode
  - `"request_timeout": 30` -- per-call timeout in seconds forwarded to MAF
- Reuse your existing Claude Desktop / Claude Code / Cursor MCP configurations (strict-JSON files parse unchanged)

---

### MCP Apps

MCP tools that declare a `_meta.ui` resource automatically render interactive UI within chat messages. The HTML View runs in a secure double-iframe sandbox with CSP enforcement.

```
# Optional: change the sandbox proxy port (default 8081)
# MCP_APPS_SANDBOX_PORT=8081
```

- **Automatic discovery**: UI-enabled MCP tools detected at server startup
- **Double-iframe sandbox**: Views run on a separate origin with no access to host DOM, cookies, or storage
- **CSP enforcement**: external resources blocked by default; servers declare required domains via metadata
- **View-to-Server proxying**: all View interactions proxied through the Host (auditable)
- **Display modes**: inline (in chat) and fullscreen
- **Session persistence**: View HTML stored as files for reload restoration
- **Progressive enhancement**: tools work as text-only when UI is unavailable or unsupported

No configuration needed -- MCP Apps activates when MCP tools have `_meta.ui.resourceUri` in their definitions. The sandbox proxy starts automatically alongside MCP servers.

---

### RAG Pipeline

Upload PDF documents and ask questions about their content using vector similarity search:

```
CHROMA_DIR=.chroma
RAG_COLLECTION_NAME=default
RAG_TOP_K=5
EMBEDDING_DEPLOYMENT_NAME=text-embedding-3-small
RAG_CHUNK_SIZE=800
RAG_CHUNK_OVERLAP=200
# Optional: trailing-chunk merge threshold (unset -> RAG_CHUNK_SIZE // 4; 0 disables)
# RAG_CHUNK_MIN_SIZE=200
```

1. Click **+** button > **Attach PDF** to upload a document
2. Ask the agent: *"Please ingest this document"*
3. The batch job processes: PDF parsing > chunking > embedding > ChromaDB storage
4. Ask questions: *"What does the document say about X?"*
5. The agent searches the knowledge base and responds with source citations (filename, page)

- **ChromaDB PersistentClient**: file-based vector storage (`.chroma/` directory)
- **Azure OpenAI Embedding**: `text-embedding-3-small` for consistent multilingual quality
- **Singleton embedding client**: Azure CLI credential resolution and TLS handshake run once per batch server process, not once per 100-text batch
- **Overlap chunking**: configurable chunk size (800 chars) and overlap (200 chars) with tail-merge that drops or folds sub-minimum trailing fragments so tiny chunks do not pollute top-k retrieval
- **Metadata filtering**: source filename, page number, chunk index for precise citation
- **Deduplication**: re-ingesting the same file overwrites existing chunks automatically
- **PDF file cards**: PDFs display as file icon cards (not image thumbnails) in chat

Requires the Batch Processing MCP Server to be configured (see below).

---

### Batch Processing

Run long-running tasks (RAG ingestion, data pipelines) as background batch jobs with a real-time monitoring dashboard:

The bundled `backend/mcp_servers.default.jsonc` already registers the batch server. To customise (job dir, per-batch overrides), copy it to `backend/mcp_servers.jsonc` and edit:

1. Start the server -- the batch MCP server launches automatically.
2. Upload a PDF and ask the agent: *"Please ingest this document"*.
3. A real-time dashboard appears inline showing progress, status, and controls.

```jsonc
{
  "mcpServers": {
    "batch": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.mcp_batch.server"],
      // env intentionally empty: BATCH_JOBS_DIR and RAG_CHUNK_*
      // live in backend/.env and are loaded by the batch subprocess
      // via load_dotenv(override=False). Add keys here only to pin
      // a per-batch override (highest precedence).
      "env": {},
      "load_prompts": false
    }
  }
}
```

- **Conversation-based management**: submit, monitor, cancel, delete jobs via chat
- **MCP Apps dashboard**: auto-refreshing progress bars, cancel/delete with confirmation dialogs
- **File-based persistence**: each job stored as a JSON file (crash-resilient)
- **Registered job types**: `rag-ingest` (Operators add new job types under `backend/src/app/mcp_batch/jobs/`)
- **Cooperative cancellation**: jobs check cancel flag at each progress checkpoint

---

### Background Responses

For long-running agent operations (e.g., o3/o4-mini reasoning models), enable Background Responses to prevent timeouts:

1. Click the **BG** toggle button (left of the context window indicator)
2. ChatInput border turns blue when active
3. Continuation tokens are auto-saved to session for page reload resumption

No environment variable needed -- toggle on/off per session via the UI.

---

### API Authentication

`API_KEY` is the **unified Bearer token** that protects the external-app
OpenAI API, every write REST endpoint, and the AG-UI chat stream when
reached from a non-loopback (LAN) client. Same-machine clients
(`127.0.0.1`, `::1`, `localhost`) bypass auth so localhost development
stays zero-configuration even when `APP_HOST=0.0.0.0` for LAN exposure.

```
API_KEY=sk-chatwalaau-your-secret-key-here
# APP_REQUIRE_AUTH_ON_LAN=true   # default: fail-closed on LAN without a key
```

Decision matrix for write endpoints (image edit, upload, MCP Apps RPC,
sessions write, templates write, TTS, STT) **and the AG-UI chat stream
(`POST /ag-ui/`)**:

| Client address | `APP_REQUIRE_AUTH_ON_LAN` | `API_KEY` | Outcome |
|----------------|---------------------------|-----------|---------|
| loopback       | any                       | any       | allow   |
| LAN            | `false`                   | any       | allow (operator opt-out) |
| LAN            | `true`                    | empty     | 503     |
| LAN            | `true`                    | set       | Bearer required |

`/v1/responses` (OpenAI API below) always requires a matching Bearer key regardless of client address because it is designed for external apps.

**Upgrading from a release before v0.47.0** with `APP_HOST` non-loopback
and `API_KEY` unset: AG-UI now returns the same 503 / 401 as every other
write endpoint. Add `API_KEY=...` to `.env`, or accept LAN exposure
explicitly with `APP_REQUIRE_AUTH_ON_LAN=false`.

---

### Web SPA Authentication

For deploying ChatWalaʻau as a private cloud web app where a single
operator signs in through the browser. Coexists with `API_KEY` (which
remains for CLI / external SDK access) and is **disabled by default** --
operators who do not set `AUTH_USERNAME` see no behavior change.

```
AUTH_USERNAME=admin
AUTH_PASSWORD_HASH=scrypt$N=16384,r=8,p=1$<base64-salt>$<base64-hash>
# AUTH_SESSION_TTL_SECONDS=86400         # default 24h, sliding
# AUTH_COOKIE_SECURE=auto                # auto / true / false
# AUTH_COOKIE_NAME=chatwalaau_session
```

Generate `AUTH_PASSWORD_HASH` with the bundled CLI:

```bash
# Interactive (prompts twice for confirmation, hidden input)
chatwalaau hash-password

# From a secret manager / pipeline
echo "$PASSWORD" | chatwalaau hash-password --stdin --quiet
```

When `AUTH_USERNAME` is set, the SPA renders a `/login` page; the
server validates credentials in constant time, issues an opaque
token, and returns it via an `HttpOnly` + `SameSite=Strict` cookie.
The backend then accepts EITHER a Bearer `API_KEY` OR a valid
session cookie on every write endpoint and the AG-UI chat stream.
The `/v1/responses` external-app path stays Bearer-only.

Properties:

- No new Python dependency (uses stdlib `hashlib.scrypt` and
  `secrets`)
- Single-user model: one username + one password hash in `.env`
- Process-local session store: no on-disk persistence, restarts
  re-prompt the user
- HTTPS strongly recommended for non-loopback deployments
- Loopback CLI calls (`curl localhost`, `chatwalaau` subcommands)
  keep their no-credential bypass so the local development
  workflow is unaffected

---

### OpenAI Compatible API

Expose the agent as an OpenAI-compatible endpoint for external applications:

```
API_KEY=sk-chatwalaau-your-secret-key-here
```

Any app using the [OpenAI SDK](https://github.com/openai/openai-python) can consume the agent by pointing `base_url`:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-chatwalaau-your-secret-key-here",
)

# Non-streaming
response = client.responses.create(
    model="chatwalaau",
    input="What is the weather in Tokyo?",
)

# Streaming
stream = client.responses.create(
    model="chatwalaau",
    input="Explain quantum computing.",
    stream=True,
)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
```

- All agent Tools (Weather, Coding, Image Generation) and Skills are available
- Multi-turn conversations via `previous_response_id`
- API sessions appear in the chat sidebar with an **API** badge
- Streaming (SSE) and non-streaming response modes
- For HTTPS/LAN access, see [OpenAI API Setup Guide](assets/docs/guides/openai-api-setup.md)

---

### Voice Input via Realtime STT

ChatWalaʻau supports two transcription transport paths for the
`POST /api/transcribe` endpoint, selected automatically by the
configured deployment name:

```
# REST audio.transcriptions path (default, classic Whisper / gpt-4o-transcribe / gpt-4o-mini-transcribe)
WHISPER_DEPLOYMENT_NAME=whisper

# Realtime API WebSocket path (gpt-realtime-whisper)
WHISPER_DEPLOYMENT_NAME=gpt-realtime-whisper
WHISPER_REALTIME_CONNECTION_DEPLOYMENT=gpt-realtime-mini
```

#### REST path (zero-config)

Set `WHISPER_DEPLOYMENT_NAME` to a deployment of `whisper-1`,
`whisper`, `gpt-4o-transcribe`, or `gpt-4o-mini-transcribe`. The
backend calls `POST /audio/transcriptions` synchronously and
returns the transcript. No extra deployments needed.

#### Realtime path (gpt-realtime-whisper)

Per Microsoft Learn `realtime-audio-websockets`, the Realtime API
URL `?model=` query accepts only VOICE Realtime deployments
(`gpt-realtime` / `gpt-realtime-mini` / `gpt-realtime-1.5`).
`gpt-realtime-whisper` is a transcription-only model and runs
alongside a voice model. Two deployments are required:

1. Deploy a voice Realtime model in Azure Foundry, e.g.
   `gpt-realtime-mini` (cheapest).
2. Deploy `gpt-realtime-whisper` for transcription.
3. Set both in `.env`:
   ```
   WHISPER_DEPLOYMENT_NAME=gpt-realtime-whisper
   WHISPER_REALTIME_CONNECTION_DEPLOYMENT=gpt-realtime-mini
   ```

Transport is auto-selected by the `WHISPER_DEPLOYMENT_NAME`
substring (`realtime` -> Realtime, otherwise REST). Override with
`WHISPER_MODEL_KIND=rest|realtime` if your deployment name
violates the convention.

Optional knobs (defaults work out of the box):

```
# Empty default selects the GA URL path /openai/v1/realtime?model=...
# Set this to a preview value (e.g. 2025-04-01-preview) only for
# legacy models such as gpt-4o-realtime-preview.
AZURE_OPENAI_REALTIME_API_VERSION=

# Browser webm/Opus is resampled to this PCM rate server-side.
# Allowed: 16000, 24000 (24000 is the OpenAI Realtime default).
WHISPER_REALTIME_AUDIO_RATE=24000
```

No SPA / `useVoiceInput` change -- the `POST /api/transcribe`
contract is byte-for-byte identical across both transports.

---

### HTTPS / LAN Access

Access ChatWalaʻau from other devices on your home network (phones, tablets, other PCs).
HTTPS enables browser Secure Context for voice input and clipboard on non-localhost origins.

```
APP_HOST=0.0.0.0
APP_SSL_CERTFILE=.certs/cert.pem
APP_SSL_KEYFILE=.certs/key.pem
```

Setup:

1. Install [mkcert](https://github.com/FiloSottile/mkcert) and run `mkcert -install`
2. Issue a certificate: `mkcert -cert-file .certs/cert.pem -key-file .certs/key.pem <your-ip> localhost 127.0.0.1`
3. Set the env vars above in `.env`
4. Allow ports through firewall (8000 for production, 5173 for dev mode)
5. Install the CA certificate (`rootCA.pem`) on each client device

Access from LAN: `https://<your-ip>:8000`

When SSL is not configured, the server runs in HTTP mode as usual (no breaking change).

---

### Multi-Model Switching

Switch between OpenAI-family models mid-conversation:

```
AZURE_OPENAI_MODELS=gpt-4o,o3,gpt-4.1-mini
```

- **Model selector dropdown** appears above the chat input (hidden when only one model configured)
- **Per-session model selection** persisted across page reloads
- **Regenerate with different model**: click the chevron on the Regenerate button to choose a model
- **Per-message model label**: each assistant message shows which model generated it
- All models share the same Tools, Skills, and MCP integrations

**Reasoning effort** is selectable **per message** from a selector next to the model picker. The allowed levels and the default follow the selected model and are served by the backend (`GET /api/model`); the chosen effort is shown next to the model name + token usage and persists with the session. There is **no environment variable** -- the per-provider default is fixed (Azure OpenAI `medium`; Anthropic `xhigh`):

- Azure OpenAI (gpt-5.5 / gpt-5.4): `low`, `medium`, `high`, `xhigh` -> `reasoning.effort`
- Anthropic (Opus 4.8 / 4.7): `low`, `medium`, `high`, `xhigh`, `max` -> adaptive thinking + `output_config.effort`

Per-model context window limits (one shared variable across **all** providers):

```
MODEL_MAX_CONTEXT_TOKENS=gpt-5.5:1050000,gpt-5.4:1050000,claude-opus-4-8:200000,claude-opus-4-7:200000
```

---

### Context Window

The progress bar above the chat input shows context window consumption rate. Colors change at 80% (amber) and 95% (red). When multiple models are configured, the display updates automatically when switching models.

---

### DevUI

Enable Microsoft Agent Framework DevUI for debugging:

```
DEVUI_ENABLED=true
DEVUI_PORT=8080
# DevUI runs in a daemon thread with its own asyncio event loop.
# Loop-bound handles (MCP tool async contexts, ChromaDB / SQLite) are
# excluded from the DevUI agent by default. Opt in with false.
# DEVUI_DISABLE_MCP=true
# DEVUI_DISABLE_RAG=true
```

Access at [http://localhost:8080](http://localhost:8080)

DevUI receives a dedicated Agent instance that reuses the main agent's
function tools, skills, and model client but omits MCP tools and
`rag_search` by default. This avoids cross-loop invocation between the
DevUI daemon thread and the main FastAPI event loop.

---

### Corporate Networks (TLS-Intercepting Proxy)

Operators behind a corporate TLS-intercepting proxy (Zscaler, Netskope,
on-prem SSL middlebox) typically hit the following on the first
outbound HTTPS call from the backend (Azure AD token acquisition,
Azure OpenAI Responses, ElevenLabs, Open-Meteo, etc.):

```
httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: self-signed certificate in certificate chain
```

The root cause is that Python's bundled `certifi` store does not know
the in-house root CA that the proxy re-signs traffic with. The
recommended remedy is to install the opt-in `corp` extras:

```bash
pip install "chatwalaau[corp]"
```

This pulls `pip-system-certs`, which routes Python's TLS validation
through the host OS certificate store (where the corporate root CA is
already trusted). No application source change, no env var change,
no behaviour change for non-corporate environments.

Alternative remedies (use whichever is available in your environment):

```bash
# Point Python at an explicit CA bundle
export SSL_CERT_FILE=/path/to/corp-root-ca.pem
export REQUESTS_CA_BUNDLE=/path/to/corp-root-ca.pem
```

---

### Upgrading `.env` Across Releases

Most releases add value through new **opt-in** settings that default off,
so the server keeps working after `pip install -U chatwalaau` with no
`.env` edits. But you cannot tell from your own `.env` which new settings
became available, and old keys pile up. Two offline commands reconcile
your `.env` against the template bundled with the installed release:

```bash
# See what this release added (new settings) or removed (no longer read)
chatwalaau env diff
chatwalaau env diff --json        # machine-readable, for scripts

# Preview the reconciliation as a unified diff (writes nothing)
chatwalaau env sync

# Apply: re-render .env to the template's keys / comments / order,
# preserving your values, after writing a timestamped backup
chatwalaau env sync --write
```

- **Your values are preserved verbatim** (quoting, `export`, inline
  comments included); only the layout and per-key documentation are
  refreshed to match the installed release.
- **Nothing is deleted.** Keys in your `.env` that the template no longer
  has (removed or custom) are moved into an `Unmanaged keys` section.
- **A timestamped backup** (`.env.<UTC>.bak`) is always written before
  `--write`, and never overwrites a prior backup.
- `env sync` is a **dry-run by default**; review the diff before applying.
- On startup, the server logs one line when your `.env` is missing keys
  the installed release added, pointing you to `chatwalaau env diff`.

> Note: `env sync --write` replaces hand-written comments on
> template-managed keys with the template's comments (your values stay).
> Custom keys are preserved in the `Unmanaged keys` section, and the
> backup lets you recover anything.

---

## Supported Platforms

* Windows 10/11
* macOS (Intel / Apple Silicon)
* Linux (Ubuntu, Debian, etc.)

---

## License

[Apache-2.0](LICENSE.md)
