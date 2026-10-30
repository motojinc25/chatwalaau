# ChatWalaʻau

Hawaii-built localhost-first AI agent platform powered by Microsoft Agent Framework.

## Install

```bash
pip install chatwalaau
```

## Quick Start

### Linux / macOS

```bash
# Generate .env configuration
chatwalaau init

# Edit .env and set AZURE_OPENAI_ENDPOINT
nano .env

# Login to Azure CLI
az login

# Start the server
chatwalaau
```

### Windows (PowerShell)

```powershell
# Generate .env configuration
chatwalaau init

# Edit .env and set AZURE_OPENAI_ENDPOINT
notepad .env

# Login to Azure CLI
az login

# Start the server
chatwalaau
```

Open http://localhost:8000/chat

## CLI Usage

```
chatwalaau                    Start the server
chatwalaau init               Generate .env from template
chatwalaau init --force       Overwrite existing .env
chatwalaau --host 0.0.0.0     Bind to all interfaces
chatwalaau --port 9000        Use custom port
chatwalaau --skip-auth-check  Skip Azure CLI login check
chatwalaau --version          Show version
```

## Requirements

- Python 3.12+
- Azure OpenAI resource with a deployed model
- Azure CLI (`az login`)

## Supported Platforms

- Windows 10/11
- macOS (Intel / Apple Silicon)
- Linux (Ubuntu, Debian, etc.)

## License

Apache-2.0
