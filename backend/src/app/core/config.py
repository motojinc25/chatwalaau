import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_debug: bool = False
    frontend_dist: str = "../frontend/dist"
    cors_allowed_origins: str = "http://localhost:5173"

    # TLS / HTTPS (CTR-0054, PRP-0029)
    app_ssl_certfile: str = ""
    app_ssl_keyfile: str = ""

    # Azure OpenAI
    azure_openai_endpoint: str = ""

    # Azure OpenAI Authentication (PRP-0058, PRP-0059, UDR-0034)
    # When set, every Azure OpenAI client (MAF chat, image gen, STT, RAG)
    # authenticates with this API key instead of an Entra ID credential.
    # Default empty -> use the Entra ID lane selected by azure_credential_mode.
    # The helper module app.azure_credential reads this value (via os.environ
    # for cross-process compatibility with the batch MCP server).
    azure_openai_api_key: str = ""

    # Azure OpenAI Credential Mode (PRP-0059, UDR-0034)
    # Selects the Entra ID credential class used when AZURE_OPENAI_API_KEY
    # is unset. Allowed values (case-insensitive): cli, managed-identity,
    # default. Defaults to "cli" (AzureCliCredential, pre-PRP-0059
    # behaviour). "managed-identity" picks ManagedIdentityCredential for
    # Azure-hosted compute (App Service, Container Apps, AKS, Functions,
    # VM). "default" picks DefaultAzureCredential with the interactive
    # browser excluded for headless cloud containers. See CTR-0006 v18
    # and UDR-0034 decision log for the full four-way matrix.
    azure_credential_mode: str = "cli"

    # Multi-Model Configuration (CTR-0069, PRP-0035)
    # Comma-separated deployment names. First entry is the default model.
    azure_openai_models: str = ""

    # Backward compatibility: old single-model variable (removed in PRP-0035).
    # If AZURE_OPENAI_MODELS is empty, this value is used as fallback.
    azure_openai_responses_deployment_name: str = ""

    # ---- Anthropic provider (CTR-0069 v2, CTR-0102, PRP-0069, UDR-0045) ----
    # Comma-separated Claude model ids routed to the "anthropic" provider.
    # Empty (default) = Anthropic disabled; the runtime behaves exactly as the
    # pre-PRP-0069 Azure-OpenAI-only build. Model ids must be unique across
    # AZURE_OPENAI_MODELS and ANTHROPIC_MODELS (validated at startup).
    anthropic_models: str = ""

    # Anthropic hosting: "direct" (Anthropic public API, default) or "foundry"
    # (Anthropic on Azure AI Foundry). Single global selector (UDR-0045 D3/G).
    anthropic_hosting: str = "direct"

    # Direct hosting auth (ANTHROPIC_HOSTING=direct).
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""

    # Foundry hosting auth (ANTHROPIC_HOSTING=foundry). Provide RESOURCE
    # (subdomain before .services.ai.azure.com) or a full BASE_URL.
    anthropic_foundry_api_key: str = ""
    anthropic_foundry_resource: str = ""
    anthropic_foundry_base_url: str = ""

    # Anthropic generation. Anthropic requires max_tokens on every request as a
    # hard cap on thinking + text output (CTR-0006, UDR-0047 D5).
    anthropic_max_tokens: int = 8192

    # Web Search
    web_search_country: str = "US"

    # Reasoning effort (PRP-0071, UDR-0047): the per-provider allowed list and
    # DEFAULT are fixed, code-owned constants served by the backend (azure-openai
    # -> medium, anthropic -> xhigh); see app.providers.{azure_openai,anthropic}.
    # The effort is selectable per message in the UI -- there is no env var for
    # it (decision: backend-managed default, not operator-configurable).

    # Session
    sessions_dir: str = ".sessions"

    # File Upload
    upload_dir: str = ".uploads"

    # Speech-to-Text (CTR-0021, PRP-0012)
    whisper_deployment_name: str = ""

    # STT Model-Kind Dispatch (CTR-0021 v3, PRP-0061, UDR-0036)
    # Selects which transport drives the STT provider:
    # - "auto" (default): infer from WHISPER_DEPLOYMENT_NAME -- substring
    #   "realtime" -> realtime, otherwise rest.
    # - "rest": force REST audio.transcriptions path (whisper-1,
    #   gpt-4o-transcribe, gpt-4o-mini-transcribe).
    # - "realtime": force Realtime API WebSocket path
    #   (gpt-realtime-whisper). Requires `pip install
    #   "chatwalaau[realtime]"` for the PCM decoder.
    # Unknown values are treated as "auto" (no hard failure on typo).
    whisper_model_kind: str = "auto"

    # Azure OpenAI Realtime API version (PRP-0061, UDR-0036).
    # Only consumed when WHISPER_MODEL_KIND resolves to "realtime".
    # Empty (default) selects the GA path:
    #   wss://<host>/openai/v1/realtime?model=<connection-deployment>
    # This is the path required by gpt-realtime-whisper and any
    # 2025-08-28+ Realtime model.
    # A non-empty value (e.g. "2025-04-01-preview") selects the
    # preview path for legacy models such as gpt-4o-realtime-preview:
    #   wss://<host>/openai/realtime?api-version=<v>&deployment=<d>
    azure_openai_realtime_api_version: str = ""

    # Realtime API WebSocket connection deployment (PRP-0061, UDR-0036).
    # Azure deviation: the `?model=` URL query parameter must name a
    # *voice* Realtime deployment (gpt-realtime / gpt-realtime-mini /
    # gpt-realtime-1.5) per Microsoft Learn `realtime-audio-websockets`.
    # `gpt-realtime-whisper` is a TRANSCRIPTION-ONLY model and is NOT
    # accepted as a connection model -- using it on the URL returns
    # HTTP 400 at handshake.
    # Resolution:
    #   - When this value is non-empty -> use it as the URL `?model=`.
    #   - When empty -> fall back to WHISPER_DEPLOYMENT_NAME (the
    #     legacy single-deployment shape; works for `gpt-4o-realtime-*`
    #     style models that handle both connection and transcription).
    # Set this to your voice Realtime deployment name (e.g. "gpt-realtime")
    # whenever WHISPER_DEPLOYMENT_NAME is a transcription-only model
    # such as `gpt-realtime-whisper`.
    whisper_realtime_connection_deployment: str = ""

    # Realtime API input audio sample rate in Hz (PRP-0061, UDR-0036).
    # The browser MediaRecorder Opus stream is resampled to this rate
    # before `input_audio_buffer.append`. Allowed values: 16000, 24000.
    whisper_realtime_audio_rate: int = 24000

    # Model Context Window (CTR-0069, PRP-0035)
    # Per-model format: "gpt-4o:128000,o3:200000,gpt-4.1-mini:1047576"
    # Single integer fallback: "128000" (applies to all models)
    # | Model                    | max_context_tokens |
    # | gpt-4o / gpt-4o-mini    | 128000             |
    # | gpt-4.1 / gpt-4.1-mini  | 1047576            |
    # | o3 / o4-mini             | 200000             |
    model_max_context_tokens: str = "128000"

    # Text-to-Speech (CTR-0039, PRP-0022)
    elevenlabs_api_key: str = ""
    tts_model_id: str = "eleven_multilingual_v2"
    tts_voice_id: str = ""

    # TTS Provider Selection (CTR-0039 v3, PRP-0063, UDR-0038)
    # Selects which provider backs POST /api/tts:
    # - "elevenlabs" (default): ElevenLabs SDK (uses ELEVENLABS_API_KEY,
    #   TTS_MODEL_ID, TTS_VOICE_ID).
    # - "azure-realtime": Azure OpenAI Realtime API voice model
    #   (e.g. gpt-realtime-2) over a WebSocket session; PCM16 output is
    #   encoded to MP3. Uses AZURE_OPENAI_ENDPOINT, the shared credential
    #   lane (UDR-0034), and the TTS_REALTIME_* settings below plus the
    #   shared AZURE_OPENAI_REALTIME_API_VERSION (reused from the STT lane).
    # Unknown values resolve to "elevenlabs" (no hard failure on typo).
    tts_provider: str = "elevenlabs"

    # Azure OpenAI Realtime TTS voice deployment (PRP-0063, UDR-0038).
    # The voice Realtime deployment name; goes directly into the URL
    # `?model=` query. gpt-realtime-2 is itself a voice model, so unlike
    # the STT whisper case there is no transcription/voice deployment
    # split. Required when TTS_PROVIDER=azure-realtime.
    tts_realtime_deployment: str = ""

    # Azure OpenAI Realtime TTS voice name (e.g. alloy / marin / cedar).
    tts_realtime_voice: str = "alloy"

    # Azure OpenAI Realtime TTS output PCM sample rate in Hz. The
    # streamed PCM16 is encoded to MP3 at this rate. Allowed: 16000, 24000.
    tts_realtime_audio_rate: int = 24000

    # Image Generation (CTR-0049, CTR-0050, PRP-0027)
    image_deployment_name: str = ""

    # Coding Tools (CTR-0031, CTR-0032, PRP-0019, PRP-0047)
    coding_enabled: bool = False
    coding_workspace_dir: str = ""
    coding_bash_timeout: int = 30
    coding_max_output_chars: int = 100000
    coding_max_turns: int = 50
    # Upper bound on bytes read by file_read in a single call (PRP-0047).
    # Prevents memory/context blow-up on very large files.
    coding_file_read_max_bytes: int = 1_048_576

    # Agent Skills (CTR-0042, PRP-0024)
    skills_dir: str = ".skills"

    # Prompt Templates (CTR-0046, PRP-0026)
    templates_dir: str = ".templates"

    # RAG Pipeline (CTR-0075, PRP-0037)
    chroma_dir: str = ".chroma"
    rag_collection_name: str = "default"
    rag_top_k: int = 5

    # MCP Integration (CTR-0059, PRP-0031, PRP-0060)
    # PRP-0060: default is the operator override file (gitignored).
    # When absent, lifecycle._resolve_mcp_config_path() falls back to
    # the tracked bundle (mcp_servers.default.jsonc). Explicit empty
    # string keeps MCP disabled (operator-acknowledged opt-out).
    mcp_config_file: str = "mcp_servers.jsonc"

    # MCP Apps (CTR-0066, PRP-0034)
    mcp_apps_sandbox_port: int = 8081

    # API Authentication (CTR-0056, CTR-0083, PRP-0045)
    # API_KEY is the unified Bearer token for write-endpoint authentication.
    # When the client address is loopback, auth is bypassed.
    # When the client address is non-loopback, APP_REQUIRE_AUTH_ON_LAN
    # gates enforcement.
    api_key: str = ""
    app_require_auth_on_lan: bool = True

    # Web SPA Authentication (CTR-0093, PRP-0057)
    # Optional ID/PW + opaque session cookie lane for cloud-deployed SPAs.
    # When AUTH_USERNAME is empty, the entire lane is disabled and
    # verify_api_key behaves byte-for-byte as the pre-PRP-0057 inline
    # Bearer check. When set, AUTH_PASSWORD_HASH must be a scrypt-format
    # hash and verify_api_key additionally accepts a valid session cookie.
    auth_username: str = ""
    auth_password_hash: str = ""
    auth_session_ttl_seconds: int = 86400
    auth_cookie_secure: str = "auto"
    auth_cookie_name: str = "chatwalaau_session"

    # DevUI (CTR-0024, PRP-0016, PRP-0046)
    devui_enabled: bool = False
    devui_port: int = 8080
    devui_auth_enabled: bool = True
    devui_auth_token: str = ""
    devui_tracing: bool = False
    devui_mode: str = "developer"
    # PRP-0046: DevUI runs in a daemon thread with its own asyncio event
    # loop. Sharing MCP tools (whose async context is entered by the
    # main FastAPI lifespan) or the RAG ChromaDB client (SQLite is
    # thread-bound) across loops is a latent crash risk, so DevUI
    # excludes those tools by default. Opt-in via false.
    devui_disable_mcp: bool = True
    devui_disable_rag: bool = True

    # Demo Mode for Cloud Deployment (CTR-0006 v22, PRP-0066, UDR-0041)
    # When truthy every metered external provider (chat / STT / TTS /
    # image generation / RAG embedder) routes to an in-process
    # deterministic dummy implementation. Default false preserves
    # v0.62.0 byte-for-byte. Single toggle (UDR-0041 D1); per-provider
    # opt-ins are forbidden.
    demo_mode: bool = False

    # Demo model list (CTR-0006 v22, PRP-0066). Only consumed when
    # demo_mode is true; populates AgentRegistry so the model selector
    # UI is non-trivially exercised. Comma-separated. Empty falls back
    # to a single "chatwalaau-demo" entry.
    demo_models: str = "gpt-5.5,gpt-5.4"

    # Demo per-token streaming delay in milliseconds (CTR-0006 v22,
    # PRP-0066). Used by DemoChatClient to pace ChatResponseUpdate
    # emission so the streaming UI (token-by-token render,
    # ScrollToBottom suspend, abort button) is exercised. Clamped to
    # [0, 500] at use time.
    demo_latency_ms: int = 40

    # ---- Conversation Compaction (CTR-0006 v23, PRP-0067, UDR-0042) ----
    # Resolved by app.agent.compaction.resolve_compaction_strategy() at
    # AgentRegistry construction time and passed to every Agent in the
    # registry (CTR-0070, CTR-0098). Compaction is purely in-memory --
    # the on-disk session JSON (FileHistoryProvider, CTR-0014) is NOT
    # mutated, so switching back to "none" fully restores the model's
    # view of history (UDR-0042 D4).
    #
    # Allowed values (case-insensitive, unknown -> sliding-window):
    # - "none" / "off" / "disabled" / empty -> compaction disabled
    # - "sliding-window" (default) -> SlidingWindowStrategy
    # - "selective-tool-call" -> SelectiveToolCallCompactionStrategy
    # - "tool-result" -> ToolResultCompactionStrategy
    compaction_strategy: str = "sliding-window"

    # Number of trailing message groups retained verbatim by the
    # selected strategy. For sliding-window this is the "keep_last_groups"
    # constructor parameter; for the two tool-call-aware variants it is
    # the "keep_last_tool_call_groups" parameter. Range 1..32 (pydantic
    # validator below).
    compaction_keep_last_groups: int = 4

    # Preserve the system / instructions message during sliding-window
    # compaction. Only consumed by sliding-window (the other two strategies
    # ignore this flag; the resolver logs an INFO note when it is set
    # under those strategies).
    compaction_preserve_system: bool = True

    # ---- Tool Approval (CTR-0006 v23, PRP-0067, UDR-0043) ----
    # Resolved by app.agent.approval.resolve_require_set() at tool-
    # registration time inside app.agui.agent_factory. The resolved set
    # is passed to wrap_with_approval() which decorates matching tool
    # functions with @tool(approval_mode="always_require") (CTR-0099).
    #
    # Modes (case-insensitive, unknown -> auto):
    # - "skip"   -- NO tool wrapped; pre-PRP-0067 byte-for-byte behaviour.
    #               SPA renders PermissionsDisabledBanner (UDR-0043 D3).
    # - "auto"   -- (default) Tools listed in TOOL_APPROVAL_REQUIRE_LIST
    #               wrapped; the default list is "bash_execute,file_write".
    # - "always" -- Every non-readonly tool on the agent is wrapped;
    #               TOOL_APPROVAL_REQUIRE_LIST is ignored.
    tool_approval_mode: str = "auto"

    # Comma-separated tool __name__ list. Only consumed when
    # tool_approval_mode == "auto". Empty / whitespace-only falls back to
    # the documented default ("bash_execute,file_write").
    tool_approval_require_list: str = "bash_execute,file_write"

    # Maximum seconds the parked AG-UI stream waits for a matching
    # POST /api/tool-approval before auto-rejecting with source="timeout"
    # (UDR-0043 D7). Range 5..86400. The asyncio.Event resolver removes
    # the approval record within this window + 60s grace.
    tool_approval_timeout_sec: int = 300

    # Per-argument-field truncation cap on the tool_approval_request
    # CUSTOM event preview (PRP-0067 risk-assessment mitigation). The
    # full argument value still reaches the tool on approval; only the
    # operator-visible preview is shortened to avoid AG-UI events that
    # carry e.g. a 1 MiB file_write content string. Range 64..65536.
    tool_approval_arg_max_chars: int = 4096

    # ---- Multi-Model helpers (CTR-0069) ----

    @property
    def model_list(self) -> list[str]:
        """Parse AZURE_OPENAI_MODELS into an ordered list of deployment names."""
        return [m.strip() for m in self.azure_openai_models.split(",") if m.strip()]

    @property
    def anthropic_model_list(self) -> list[str]:
        """Parse ANTHROPIC_MODELS into an ordered list of Claude model ids."""
        return [m.strip() for m in self.anthropic_models.split(",") if m.strip()]

    @property
    def all_model_list(self) -> list[str]:
        """Merged ordered model list across providers (Azure first, then Anthropic).

        UDR-0045 D3: ordering is Azure-first then Anthropic, so the default
        model is the first Azure model when any is configured (preserving the
        pre-PRP-0069 default), and falls back to the first Anthropic model for
        an Anthropic-only deployment. Model ids are unique across providers
        (enforced by _validate_anthropic), but a defensive de-dup keeps order.
        """
        merged = list(self.model_list)
        for m in self.anthropic_model_list:
            if m not in merged:
                merged.append(m)
        return merged

    @property
    def default_model(self) -> str:
        """First model in the merged provider-aware list is the default."""
        models = self.all_model_list
        if not models:
            return ""
        return models[0]

    def get_max_context_tokens(self, model: str | None = None) -> int:
        """Resolve max context tokens for a specific model.

        Supports two formats:
        - Per-model: "gpt-4o:128000,o3:200000"
        - Single integer: "128000" (applies to all models)
        """
        raw = self.model_max_context_tokens.strip()
        if ":" not in raw:
            # Single integer fallback
            try:
                return int(raw)
            except ValueError:
                return 128000
        # Per-model format
        pairs: dict[str, str] = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if ":" in entry:
                name, value = entry.split(":", 1)
                pairs[name.strip()] = value.strip()
        target = model or self.default_model
        if target in pairs:
            try:
                return int(pairs[target])
            except ValueError:
                pass
        # Fallback: default model's value, or 128000
        if self.default_model in pairs:
            try:
                return int(pairs[self.default_model])
            except ValueError:
                pass
        return 128000

    @property
    def max_context_tokens_map(self) -> dict[str, int]:
        """Return a map of model -> max_context_tokens for all configured models.

        Spans every provider (Azure + Anthropic) so the frontend context-window
        indicator resolves a limit for any selectable model. Operators add
        Claude entries to MODEL_MAX_CONTEXT_TOKENS; unlisted models fall back to
        the default per get_max_context_tokens. Azure-only behavior is unchanged
        (all_model_list == model_list when ANTHROPIC_MODELS is empty).
        """
        return {model: self.get_max_context_tokens(model) for model in self.all_model_list}

    # ---- Demo Mode helpers (CTR-0006 v22, PRP-0066, UDR-0041) ----

    @property
    def demo_model_list(self) -> list[str]:
        """Parse DEMO_MODELS into an ordered list of deployment names."""
        parsed = [m.strip() for m in self.demo_models.split(",") if m.strip()]
        return parsed or ["chatwalaau-demo"]

    @property
    def demo_latency_ms_clamped(self) -> int:
        """DEMO_LATENCY_MS clamped to the documented [0, 500] range."""
        return max(0, min(500, self.demo_latency_ms))

    # ---- Validators ----

    @model_validator(mode="after")
    def _validate_models(self) -> "Settings":
        # Backward compatibility: migrate old single-model variable (PRP-0035)
        if not self.azure_openai_models and self.azure_openai_responses_deployment_name:
            self.azure_openai_models = self.azure_openai_responses_deployment_name
            _logger.warning(
                "AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME is deprecated. "
                "Please migrate to AZURE_OPENAI_MODELS=%s in your .env file.",
                self.azure_openai_models,
            )
        if not self.all_model_list:
            _logger.warning(
                "No models configured (AZURE_OPENAI_MODELS and ANTHROPIC_MODELS are both empty); "
                "agent creation will be skipped."
            )
        return self

    @model_validator(mode="after")
    def _validate_anthropic(self) -> "Settings":
        """Validate the Anthropic provider configuration (PRP-0069, UDR-0045).

        - ANTHROPIC_HOSTING must be one of {direct, foundry} (normalised).
        - Foundry hosting requires a resource or a base_url.
        - Model ids must be unique across providers (UDR-0045 D3).
        Validation only fires for the configured surface, so Azure-only
        deployments (ANTHROPIC_MODELS empty) are never affected.
        Reasoning effort (PRP-0071, UDR-0047): Anthropic uses adaptive thinking
        + output_config.effort; ANTHROPIC_THINKING_BUDGET is removed, so there is
        no budget-vs-max_tokens check. The effort default is a fixed,
        backend-owned per-provider constant (no env var).
        """
        hosting = (self.anthropic_hosting or "").strip().lower()
        allowed = {"direct", "foundry"}
        if hosting and hosting not in allowed:
            raise ValueError(f"ANTHROPIC_HOSTING must be one of {sorted(allowed)}, got {hosting!r}")
        self.anthropic_hosting = hosting or "direct"

        if (
            self.anthropic_model_list
            and self.anthropic_hosting == "foundry"
            and not (self.anthropic_foundry_resource.strip() or self.anthropic_foundry_base_url.strip())
        ):
            msg = "ANTHROPIC_HOSTING=foundry requires ANTHROPIC_FOUNDRY_RESOURCE or ANTHROPIC_FOUNDRY_BASE_URL."
            raise ValueError(msg)

        dupes = sorted(set(self.model_list) & set(self.anthropic_model_list))
        if dupes:
            msg = (
                f"Model id(s) {dupes} appear under multiple providers; ids must be unique "
                "across AZURE_OPENAI_MODELS and ANTHROPIC_MODELS."
            )
            raise ValueError(msg)

        return self

    @model_validator(mode="after")
    def _validate_credential_mode(self) -> "Settings":
        """Reject unknown AZURE_CREDENTIAL_MODE values at startup (PRP-0059)."""
        mode = self.azure_credential_mode.strip().lower()
        allowed = {"cli", "managed-identity", "default"}
        if mode and mode not in allowed:
            raise ValueError(f"AZURE_CREDENTIAL_MODE must be one of {sorted(allowed)}, got {mode!r}")
        # Normalise: empty -> "cli", and lowercase for downstream readers.
        self.azure_credential_mode = mode or "cli"
        return self

    # ---- API Authentication helpers (CTR-0083, PRP-0045) ----

    @property
    def is_loopback_bind(self) -> bool:
        """True when APP_HOST resolves to an IPv4/IPv6 loopback address.

        Name-based hosts are resolved via getaddrinfo so operators can use
        "localhost" or custom /etc/hosts aliases. Resolution errors fall back
        to a literal check.
        """
        import ipaddress
        import socket

        host = (self.app_host or "").strip()
        if not host:
            return True
        # Literal IP fast path
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        # Hostname path: resolve and require every address to be loopback
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return host.lower() == "localhost"
        if not infos:
            return host.lower() == "localhost"
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                return False
            try:
                if not ipaddress.ip_address(sockaddr[0]).is_loopback:
                    return False
            except ValueError:
                return False
        return True

    @model_validator(mode="after")
    def _warn_on_unauthenticated_lan(self) -> "Settings":
        if self.is_loopback_bind:
            return self
        if not self.api_key:
            if self.app_require_auth_on_lan:
                _logger.warning(
                    "APP_HOST=%s is non-loopback and API_KEY is unset. "
                    "Write endpoints will return 503 until API_KEY is set or "
                    "APP_REQUIRE_AUTH_ON_LAN=false is acknowledged.",
                    self.app_host,
                )
            else:
                _logger.warning(
                    "APP_HOST=%s is non-loopback, API_KEY is unset, and "
                    "APP_REQUIRE_AUTH_ON_LAN=false. Write endpoints are open "
                    "to LAN peers; this is an operator-acknowledged insecure mode.",
                    self.app_host,
                )
        return self

    # ---- Web SPA Authentication helpers (CTR-0093, PRP-0057) ----

    @property
    def web_auth_enabled(self) -> bool:
        """True when AUTH_USERNAME is non-empty (web ID/PW lane active)."""
        return bool(self.auth_username.strip())

    @model_validator(mode="after")
    def _validate_web_auth(self) -> "Settings":
        """Validate AUTH_* settings per CTR-0093.

        Startup fail-fast cases:
        - AUTH_USERNAME set, AUTH_PASSWORD_HASH empty.
        - AUTH_USERNAME set, AUTH_PASSWORD_HASH fails scrypt-format parse.
        - AUTH_SESSION_TTL_SECONDS < 60.
        - AUTH_COOKIE_SECURE not in {"auto", "true", "false"}.
        """
        cookie_secure = self.auth_cookie_secure.strip().lower()
        if cookie_secure not in {"auto", "true", "false"}:
            msg = f"AUTH_COOKIE_SECURE must be one of: auto, true, false. Got: {self.auth_cookie_secure!r}"
            raise ValueError(msg)
        self.auth_cookie_secure = cookie_secure

        if self.web_auth_enabled:
            if self.auth_session_ttl_seconds < 60:
                msg = "AUTH_SESSION_TTL_SECONDS must be >= 60."
                raise ValueError(msg)
            if not self.auth_password_hash.strip():
                msg = (
                    "AUTH_USERNAME is set but AUTH_PASSWORD_HASH is empty. "
                    "Generate a hash with the recipe in assets/docs/guides/web-auth.md."
                )
                raise ValueError(msg)
            # Defer scrypt-format parse to app.auth.password; importing it
            # here would create a circular dependency. The parse runs at
            # router import time (which is also process startup).
        return self

    @model_validator(mode="after")
    def _warn_on_no_auth_configured(self) -> "Settings":
        """Extend CTR-0083 startup warning: warn when neither lane is configured.

        Original PRP-0045 warning only checked API_KEY. With PRP-0057,
        either API_KEY or AUTH_USERNAME satisfies the non-loopback
        deployment. Warn only if BOTH are empty AND the bind is non-
        loopback AND APP_REQUIRE_AUTH_ON_LAN is true.
        """
        if self.is_loopback_bind:
            return self
        if not self.app_require_auth_on_lan:
            return self
        if self.api_key or self.web_auth_enabled:
            return self
        # Both API_KEY and AUTH_USERNAME empty on a non-loopback bind with
        # gating on -- the _warn_on_unauthenticated_lan validator already
        # logs the API_KEY-side message; this one specifically calls out
        # the new alternative.
        _logger.warning(
            "APP_HOST=%s is non-loopback; neither API_KEY (CLI/external) nor "
            "AUTH_USERNAME (browser ID/PW) is set. Configure one of them in "
            ".env or set APP_REQUIRE_AUTH_ON_LAN=false to acknowledge the "
            "open mode.",
            self.app_host,
        )
        return self

    # ---- Conversation Compaction + Tool Approval validators (PRP-0067) ----

    @model_validator(mode="after")
    def _validate_compaction(self) -> "Settings":
        """Normalize compaction settings and reject out-of-range values."""
        name = (self.compaction_strategy or "").strip().lower()
        allowed = {"", "none", "off", "disabled", "sliding-window", "selective-tool-call", "tool-result"}
        if name and name not in allowed:
            _logger.warning(
                "COMPACTION_STRATEGY=%r is not recognised; will fall back to sliding-window. Allowed: %s",
                self.compaction_strategy,
                sorted(a for a in allowed if a),
            )
        # Store normalized value so downstream readers compare lowercased.
        self.compaction_strategy = name
        if not (1 <= self.compaction_keep_last_groups <= 32):
            msg = f"COMPACTION_KEEP_LAST_GROUPS must be in 1..32; got {self.compaction_keep_last_groups}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_tool_approval(self) -> "Settings":
        """Normalize tool-approval settings and reject out-of-range values."""
        mode = (self.tool_approval_mode or "").strip().lower()
        allowed = {"skip", "auto", "always"}
        if mode and mode not in allowed:
            _logger.warning(
                "TOOL_APPROVAL_MODE=%r is not recognised; will fall back to 'auto'. Allowed: %s",
                self.tool_approval_mode,
                sorted(allowed),
            )
            mode = "auto"
        self.tool_approval_mode = mode or "auto"
        if not (5 <= self.tool_approval_timeout_sec <= 86400):
            msg = f"TOOL_APPROVAL_TIMEOUT_SEC must be in 5..86400; got {self.tool_approval_timeout_sec}"
            raise ValueError(msg)
        if not (64 <= self.tool_approval_arg_max_chars <= 65536):
            msg = f"TOOL_APPROVAL_ARG_MAX_CHARS must be in 64..65536; got {self.tool_approval_arg_max_chars}"
            raise ValueError(msg)
        return self

    @property
    def tool_approval_require_set(self) -> frozenset[str]:
        """Parse TOOL_APPROVAL_REQUIRE_LIST into a frozenset (skip mode -> empty)."""
        if self.tool_approval_mode == "skip":
            return frozenset()
        raw = (self.tool_approval_require_list or "").strip()
        items = [s.strip() for s in raw.split(",") if s.strip()] if raw else []
        # Empty / whitespace-only falls back to the documented default
        # ("bash_execute,file_write"). The default is itself two entries
        # so this returns a non-empty frozenset.
        if not items:
            items = ["bash_execute", "file_write"]
        return frozenset(items)

    @model_validator(mode="after")
    def _validate_ssl_pair(self) -> "Settings":
        has_cert = bool(self.app_ssl_certfile)
        has_key = bool(self.app_ssl_keyfile)
        if has_cert != has_key:
            msg = "APP_SSL_CERTFILE and APP_SSL_KEYFILE must both be provided or both omitted."
            raise ValueError(msg)
        if has_cert and has_key:
            from pathlib import Path

            cert_path = Path(self.app_ssl_certfile)
            key_path = Path(self.app_ssl_keyfile)
            if not cert_path.exists():
                msg = f"SSL certificate file not found: {cert_path}"
                raise ValueError(msg)
            if not key_path.exists():
                msg = f"SSL key file not found: {key_path}"
                raise ValueError(msg)
        return self


settings = Settings()
