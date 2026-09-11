# ChatWalaʻau

**A local-first AI agent runtime — chat, tools, RAG, and MCP in one `pip install`.**

[![PyPI](https://img.shields.io/pypi/v/chatwalaau)](https://pypi.org/project/chatwalaau/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE.md)
[![Python](https://img.shields.io/pypi/pyversions/chatwalaau)](https://pypi.org/project/chatwalaau/)

Run an AI agent workspace on localhost. Chat with models from multiple providers, work with files and documents, connect tools, and build agents and workflows — all from one app.

The UI, runtime, and local storage run on your machine. Model requests go to your configured providers.

> Built in Hawaiʻi, powered by [Microsoft Agent Framework](https://github.com/microsoft/agent-framework).

**[Documentation](https://www.chatwalaau.com)** · **[Installation](https://www.chatwalaau.com/docs/getting-started/installation)** · **[Configuration](https://www.chatwalaau.com/docs/getting-started/configuration)**

## Quick Start

### 1. Install and initialize

```bash
pip install chatwalaau
chatwalaau init
```

The setup creates your `.env` file and offers a guided first-model configuration.

### 2. Configure credentials and a model

Add your provider credentials to `.env`. For example, with Azure OpenAI:

```ini
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<your-key>
```

Configure at least one chat model through the setup wizard or:

```bash
chatwalaau models add
```

Models are stored in `model_offerings.jsonc`. You can also manage chat, image, embedding, and helper-model assignments in **App Settings**.

Supported providers: **Azure OpenAI · Anthropic · OpenAI · Microsoft Foundry**

See [Models & Reasoning](https://www.chatwalaau.com/docs/features/models-and-reasoning) for provider-specific setup.

### 3. Start chatting

```bash
chatwalaau
```

Open **[localhost:8000/chat](http://localhost:8000/chat)**.

> **Using Entra ID instead of an Azure API key?** See the [Authentication guide](https://www.chatwalaau.com/docs/api-and-cli/authentication).
>
> **Behind a corporate TLS proxy?** Install with `pip install "chatwalaau[corp]"`.

<details>
<summary><strong>Upgrading an existing installation</strong></summary>

```bash
pip install --upgrade chatwalaau
chatwalaau settings migrate --write
```

Runtime settings previously stored in `.env` now belong in `app_settings.jsonc`, editable through **App Settings**. Credentials, endpoints, bootstrap paths, and feature enable-gates remain in `.env`.

Legacy model environment variables are no longer supported. Configure models in `model_offerings.jsonc` using `chatwalaau models add` or **App Settings**.

See the [Configuration guide](https://www.chatwalaau.com/docs/getting-started/configuration) before migrating.

</details>

## Highlights

| Area | What you can do |
| --- | --- |
| **Chat & media** | Render Markdown, code, math, and Mermaid diagrams. Use voice, image analysis, PDF attachments, web search with citations, and a built-in paint canvas. |
| **Conversation controls** | Use slash commands, search and bookmark chats, navigate messages, start temporary chats, and mask your content with Privacy Screen for screen sharing. |
| **Models** | Switch providers mid-conversation. Configure reasoning, verbosity, structured output, prompt caching, and deployment-specific capabilities. Availability depends on the model and deployment. |
| **Tools & Skills** | Generate and edit images, check weather, work with code, and use prompt templates and Agent Skills. Review tool actions through an approval workflow. |
| **Knowledge & RAG** | Ingest PDFs into a ChromaDB-backed knowledge base. Submit, monitor, and cancel ingestion through Pipeline Jobs. |
| **Ontology** | Build RDF concept models on a visual graph canvas. Import/export RDF, query with SPARQL or natural language, and let agents use your ontologies. |
| **MCP** | Connect MCP servers using Claude Desktop-compatible configuration. Toggle servers and tools, reload connections, and render interactive MCP Apps in chat. |
| **Memory** | Maintain agent identity, user preferences, and project knowledge in editable Markdown files with automatic backups. |
| **Workspace** | Browse and edit files in a Monaco-based editor with tabs and split panes. Upload folders, download ZIPs, preview PDFs and images, and attach them to chat. |
| **Agents & workflows** | Define agents in YAML or a GUI. Compose workflows in a visual DAG editor with branching, loops, tool calls, human input, and live execution views. Run in chat or as background jobs. |
| **Harness agents** | Run autonomous coding agents with planning, todo lists, session memory, workspace-scoped file and shell access, Skills, and context compaction. |
| **Automation & Teams** | Schedule workspace scripts, chat with agents in Microsoft Teams, and summarize meeting transcripts through Microsoft Graph webhooks or on demand. |
| **API & usage** | Connect OpenAI-SDK applications through `/v1/responses`. Track token usage by day, month, chat, model, or lane; workflow runs are excluded, and token counts are not billing totals. |
| **Settings & diagnostics** | Manage models and runtime settings in the app, with clear notices about when changes take effect. Inspect prompts and tool availability with opt-in Prompt Dump. |
| **Local-first storage & access** | Keep sessions, vectors, and uploads on your machine. Use API-key authentication, optional web sign-in, session-protected images, and restart-persistent sign-in. |

Some features require explicit enablement or additional provider configuration. See the [feature documentation](https://www.chatwalaau.com/docs/features/chat-and-ui) for setup and limitations.

## UI Preview

<p align="center">
  <img src="assets/images/screenshot1.png" alt="ChatWalaʻau chat interface with weather tools">
</p>

<details>
<summary><strong>More screenshots</strong></summary>

<p align="center">
  <img src="assets/images/screenshot2.png" alt="Mermaid diagrams in chat">
  <img src="assets/images/screenshot3.png" alt="Image analysis">
  <img src="assets/images/screenshot4.png" alt="Agent development UI">
  <img src="assets/images/screenshot5.png" alt="Conversation search">
  <img src="assets/images/screenshot6.png" alt="Image generation">
</p>

</details>

## Documentation

Full guides are available in **English and 日本語**, with full-text search.

- **Get started:** [Installation](https://www.chatwalaau.com/docs/getting-started/installation) · [Configuration](https://www.chatwalaau.com/docs/getting-started/configuration) · [Authentication](https://www.chatwalaau.com/docs/api-and-cli/authentication)
- **Use the app:** [Chat & UI](https://www.chatwalaau.com/docs/features/chat-and-ui) · [Models & Reasoning](https://www.chatwalaau.com/docs/features/models-and-reasoning) · [Memory & Sessions](https://www.chatwalaau.com/docs/features/memory-and-sessions)
- **Build agents:** [Tools & Skills](https://www.chatwalaau.com/docs/features/agents-and-tools) · [Knowledge & MCP](https://www.chatwalaau.com/docs/features/knowledge-and-mcp) · [Declarative Agents](https://www.chatwalaau.com/docs/features/declarative-agents)
- **Integrate:** [OpenAI-compatible API](https://www.chatwalaau.com/docs/api-and-cli/openai-compatible-api) · [CLI](https://www.chatwalaau.com/docs/api-and-cli/cli) · [Usage Statistics](https://www.chatwalaau.com/docs/api-and-cli/usage-statistics)
- **Develop & deploy:** [Development Setup](https://www.chatwalaau.com/docs/deployment/development) · [Networking & Operations](https://www.chatwalaau.com/docs/deployment/operations)

## Development

The backend uses **Python + uv**; the frontend uses **pnpm**.

For prerequisites, local development commands, and production builds, see the [Development Setup guide](https://www.chatwalaau.com/docs/deployment/development).

## Supported Platforms

Windows 10/11 · macOS (Intel / Apple Silicon) · Linux

## About the Name

**Walaʻau** means “to chat, talk, or converse” in Hawaiian. Built in Hawaiʻi, this project uses the name with respect and gratitude for the Hawaiian language and its community.

## License

[Apache-2.0](LICENSE.md)
