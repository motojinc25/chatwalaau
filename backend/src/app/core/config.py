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

    # ---- Model Offering Catalog (CTR-0174, PRP-0109, UDR-0087) ----
    # Path to the operator-owned model offering catalog (JSONC). When the file
    # exists it is the SINGLE SOURCE OF TRUTH for model routing: each offering
    # self-describes provider / model_ref / endpoint / hosting / auth reference /
    # operations, and the legacy AZURE_OPENAI_MODELS / ANTHROPIC_MODELS /
    # OPENAI_MODELS / FOUNDRY_MODELS namespaces below are IGNORED for routing
    # (one startup warning enumerates them). When the file is ABSENT (the
    # default, since the file is not shipped) the legacy namespaces apply
    # byte-for-byte. Set to empty to force the legacy lane even if a file exists.
    # DEMO_MODE always uses the legacy lane (the catalog is never consulted).
    # Secrets are never written in the file -- an offering references an env var
    # by NAME (api_key_env) or uses the shared Entra ID lanes (UDR-0087 D4).
    model_offerings_file: str = "model_offerings.jsonc"

    # ---- Model routing: Model Offering Catalog ONLY (PRP-0113, UDR-0094) ----
    # PRP-0113 / UDR-0094 retired the legacy env-namespace model ROUTING lane.
    # The per-provider model lists (AZURE_OPENAI_MODELS / ANTHROPIC_MODELS /
    # OPENAI_MODELS / FOUNDRY_MODELS), the ancient single-model fallback
    # (AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME), the global ANTHROPIC_HOSTING
    # switch, MODEL_MAX_CONTEXT_TOKENS, and the per-provider CHAT endpoint /
    # base_url / api-key fields (ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL /
    # ANTHROPIC_FOUNDRY_* / OPENAI_BASE_URL / FOUNDRY_PROJECT_ENDPOINT) were
    # REMOVED. Chat-model routing now comes SOLELY from model_offerings.jsonc,
    # where each offering self-describes provider / model_ref / endpoint /
    # base_url / hosting / context_window and references any api-key env var by
    # NAME (api_key_env). The Azure credential / endpoint substrate
    # (AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_CREDENTIAL_MODE,
    # AZURE_TENANT_ID) is RETAINED, shared with image / RAG / STT / TTS and used
    # as the azure-openai offering fallback (UDR-0094 D6).

    # Anthropic generation. Anthropic requires max_tokens on every request as a
    # cap on thinking + text output COMBINED (CTR-0006, UDR-0047 D5). Because
    # adaptive-thinking effort and the answer share this budget, the per-effort
    # tier (app.providers.anthropic.ANTHROPIC_EFFORT_MAX_TOKENS) is the effective
    # value; this setting is a FLOOR an operator can raise (never lowers the
    # tier). Default 8192 = the low-effort tier.
    anthropic_max_tokens: int = 8192

    # OpenAI API key (OPENAI_API_KEY). RETAINED (UDR-0094 D6): consumed by image
    # generation (app.image_gen) and the RAG embedder plain-OpenAI path; also a
    # valid value for a chat offering's api_key_env reference.
    openai_api_key: str = ""

    # Web Search
    web_search_country: str = "US"

    # ---- Prompt Caching (CTR-0006 v24, PRP-0080, FEAT-0038 / UDR-0056) ----
    # Provider-agnostic input-token cost reduction. When enabled (default) each
    # provider marks its large, stable per-call prefix (system prompt + tool
    # schemas) as cacheable, so that prefix is billed once and re-read cheaply on
    # the many model calls of a single turn (e.g. a coding tool loop). Caching is
    # OUTPUT-TRANSPARENT: model outputs are identical with it on or off; only
    # billing / latency / the additive usage cache fields change. Set false to
    # restore the pre-PRP-0080 request shape (no cache_control, no cache key, no
    # usage cache fields).
    #   - azure-openai: caching is AUTOMATIC for prefixes >= 1024 tokens (no
    #     request rewrite either way); this provider is effectively pass-through.
    #   - anthropic: injects cache_control breakpoints on the system block + the
    #     tools array at request-assembly time (app.providers.anthropic).
    prompt_cache_enabled: bool = True

    # Anthropic prompt-cache TTL: "5m" (default, GA) or "1h" (extended cache,
    # adds the extended-cache-ttl-2025-04-11 beta). Unknown -> "5m" (validator
    # below). Only the anthropic provider consumes this; azure-openai ignores it.
    anthropic_prompt_cache_ttl: str = "5m"

    # Reasoning effort (PRP-0071, UDR-0047): the per-provider allowed list and
    # DEFAULT are fixed, code-owned constants served by the backend (azure-openai
    # -> medium, anthropic -> xhigh); see app.providers.{azure_openai,anthropic}.
    # The effort is selectable per message in the UI -- there is no env var for
    # it (decision: backend-managed default, not operator-configurable).

    # Session
    sessions_dir: str = ".sessions"

    # Auto Session Title (CTR-0109, CTR-0006, PRP-0077 / UDR-0053). SESSION_TITLE_MODE
    # selects how a chat's sidebar title is set: "truncate" (default) keeps the
    # leading characters of the first user message (pre-PRP-0077 behavior,
    # byte-for-byte); "llm" upgrades it via a background task (CTR-0108) that
    # summarizes the first user message + first assistant reply, post-turn and
    # non-blocking (on failure the truncation title remains). The summarization
    # model is the catalog `roles.session_title` binding (PRP-0115 / UDR-0096); the
    # removed SESSION_TITLE_MODEL env var no longer applies. Unknown
    # SESSION_TITLE_MODE values are treated as "truncate".
    session_title_mode: str = "truncate"

    # Temporary Chat (CTR-0106, CTR-0006, PRP-0076 / UDR-0052 D4/D9).
    # Quarantine retention for ephemeral "incognito-style" conversations: a
    # Temporary Chat is stored in a separate .temporary/ directory (a sibling of
    # SESSIONS_DIR, never user-listed) for safety / abuse monitoring and is
    # auto-deleted after this many days. The whole feature has NO enable/disable
    # env var (always available, UDR-0052 D9); this retention period is the only
    # knob. <= 0 disables the sweep (entries kept indefinitely).
    temporary_chat_retention_days: int = 30

    # User Preference Memory (CTR-0105, CTR-0006, PRP-0075 / UDR-0051 D12).
    # Master toggle for the Memory Block (Prompt Assembly slot #2): the rendered
    # .agent/USER.md snapshot, the inline memory tool, and per-session snapshot
    # capture. Default true. USER_PROFILE_ENABLED=false restores pre-PRP-0075
    # behavior byte-for-byte. USER_CHAR_LIMIT bounds the curated entries
    # (boilerplate excluded); an add/replace that would exceed it is rejected with
    # guidance to consolidate (UDR-0051 D7).
    user_profile_enabled: bool = True
    user_char_limit: int = 1375

    # User Memory Background Extraction (CTR-0117, CTR-0006, PRP-0079 /
    # UDR-0051 Phase 2, resolving D5). Opt-in background pass that distills a
    # conversation into durable user preferences and merges them into
    # .agent/USER.md through the EXISTING CTR-0105 guarded write (the same
    # secret/PII filter, USER_CHAR_LIMIT cap, and backup-on-write as the inline
    # tool). USER_MEMORY_EXTRACTION (default false) gates the feature and ALSO
    # requires USER_PROFILE_ENABLED=true; false is byte-for-byte pre-PRP-0079
    # (Phase 1) behavior. The extraction model is the catalog
    # `roles.user_memory_extraction` binding (PRP-0115 / UDR-0096); the removed
    # USER_MEMORY_EXTRACTION_MODEL env var no longer applies. The pass runs at most
    # once every USER_MEMORY_EXTRACTION_EVERY_N_TURNS new user turns (default 4;
    # clamped to >= 1 at use time) -- below the threshold the task is a cheap
    # no-op (no LLM call). temp_ chats and DEMO_MODE never extract.
    user_memory_extraction: bool = False
    user_memory_extraction_every_n_turns: int = 4

    # Agent Curated Memory (CTR-0162 / CTR-0163, CTR-0006, PRP-0100 / UDR-0079).
    # A SECOND built-in file memory `.agent/MEMORY.md` -- the agent's own curated
    # notebook of durable, reusable, non-sensitive facts about the environment /
    # project / conventions / tool quirks -- stored as `§`-delimited entries and
    # rendered at Prompt Assembly slot #2b (after the User Profile). AGENT_MEMORY_ENABLED
    # is the master toggle (default true, matching USER_PROFILE_ENABLED): it gates the
    # <agent-memory> block, the inline manage_memory tool, the per-turn like UI/API,
    # and per-session snapshot capture. false restores pre-PRP-0100 behavior
    # byte-for-byte. An EMPTY MEMORY.md injects NO block, so a fresh install with the
    # default-on toggle has an unchanged per-session prompt until a first entry is
    # curated (UDR-0079 D8). MEMORY_CHAR_LIMIT bounds the serialized memory body
    # (default 2200); an over-cap add/modify is rejected with guidance to
    # remove/consolidate. The background reconcile model for the like pass (CTR-0163)
    # is the catalog `roles.agent_memory_curation` binding (PRP-0115 / UDR-0096); the
    # removed AGENT_MEMORY_CURATION_MODEL env var no longer applies. The file path is
    # fixed (not an env var).
    agent_memory_enabled: bool = True
    memory_char_limit: int = 2200

    # Built-in Memory Editor (CTR-0166, CTR-0006, PRP-0101 / UDR-0080 D4). The
    # human-facing Memory Management portal edits the three fixed `.agent/*.md`
    # files (IDENTITY / USER / MEMORY). The per-file character cap is enforced on a
    # save: USER_CHAR_LIMIT (1375) and MEMORY_CHAR_LIMIT (2200) already exist;
    # IDENTITY_CHAR_LIMIT bounds `.agent/IDENTITY.md` so all three carry the same
    # "backup + cap" guard. Identity is Prompt Assembly slot #1 injected into every
    # request, so a generous-but-present cap bounds prompt bloat. The file PATH stays
    # fixed (not an env var). The operator write skips the agent-facing secret/PII
    # filter (backup + cap only, UDR-0080 D2).
    identity_char_limit: int = 4000

    # Prompt Dump (debug / observability, CTR-0006 / CTR-0009). When
    # PROMPT_DUMP_ENABLED (default false), each AG-UI run writes the fully assembled
    # system prompt (Identity + User Profile + Agent Memory + capability guidance)
    # and the input messages to a timestamped file under PROMPT_DUMP_DIR (default
    # ".prompts") so operators can inspect the exact prompt state per run. The prompt
    # CONTENT goes ONLY to the file; logs emit metadata (path, sizes, injection flags)
    # but never the full prompt body. Best-effort: a write failure logs a WARNING and
    # never blocks the chat. Default false -> no files written, no behavior change.
    prompt_dump_enabled: bool = False
    prompt_dump_dir: str = ".prompts"

    # File Upload
    upload_dir: str = ".uploads"

    # Attached PDF handling (CTR-0009, PRP-0116, UDR-0099).
    # An attached PDF is given to the LLM as context, like an image. How:
    #   auto (default): NATIVE document attachment for providers whose connector
    #     supports it (azure-openai / openai / foundry -> sent as a Responses
    #     `input_file`); TEXT extraction for others (anthropic connector drops
    #     non-image data, so its PDF text is extracted instead).
    #   native: always attach the raw PDF (only for a provider that supports it).
    #   text: always extract the PDF text (safe fallback, e.g. if a deployment
    #     rejects `input_file`).
    pdf_attach_mode: str = "auto"
    # Character cap on TEXT-extracted PDF content (text/anthropic path). Text
    # beyond it is truncated with a `truncated="true"` marker (the
    # CODING_FILE_READ_MAX_BYTES precedent).
    pdf_inline_max_chars: int = 20000

    # Session Export/Import (CTR-0015, CTR-0006, PRP-0084 / UDR-0062 D5).
    # Maximum accepted size (bytes) of an uploaded chat import bundle (.zip).
    # Bounds the compressed upload; the import path additionally enforces an
    # internal uncompressed-total and entry-count cap (zip-bomb defense).
    session_import_max_bytes: int = 26_214_400  # 25 MiB

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

    # Model context window: removed (PRP-0113, UDR-0094 D5). The per-model
    # context limit is now an offering's `context_window` in model_offerings.jsonc,
    # defaulting to app.models_catalog.DEFAULT_CONTEXT_WINDOW (128000) when unset.

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

    # Image Generation (CTR-0049, CTR-0050).
    # PRP-0114 / UDR-0095: image model routing (deployment / api_version) AND the
    # output-behavior defaults (size / quality / format / compression / background)
    # now live on the Model Offering Catalog `image` offering (model_ref /
    # api_version / image_defaults), NOT in Settings. The former IMAGE_DEPLOYMENT_NAME
    # / IMAGE_API_VERSION / IMAGE_SIZE / IMAGE_QUALITY / IMAGE_FORMAT /
    # IMAGE_COMPRESSION / IMAGE_BACKGROUND fields were removed here; the shared Azure
    # endpoint / credential substrate (azure_openai_endpoint, azure_openai_api_key,
    # azure_credential_mode, azure_tenant_id) is retained and unchanged.

    # Coding Tools (CTR-0031, CTR-0032, PRP-0019, PRP-0047)
    coding_enabled: bool = False
    coding_workspace_dir: str = ""
    coding_bash_timeout: int = 30
    coding_max_output_chars: int = 100000
    coding_max_turns: int = 50
    # Upper bound on bytes read by file_read in a single call (PRP-0047).
    # Prevents memory/context blow-up on very large files.
    coding_file_read_max_bytes: int = 1_048_576

    # File Explorer (CTR-0006, CTR-0136/0137, PRP-0091, UDR-0069)
    # Human-facing file browse/edit over the coding workspace. OFF unless
    # FILE_EXPLORER_ENABLED; every file operation ADDITIONALLY requires
    # CODING_ENABLED and reuses the CTR-0031 realpath jail (UDR-0069 D2/D3).
    file_explorer_enabled: bool = False
    # Max size a file may be opened/saved at in the editor (memory + transport
    # guard); an oversize file is listed but refused for open/save (UDR-0069 D6).
    file_explorer_max_file_bytes: int = 5_242_880  # 5 MiB
    # Cap on entries returned per directory level; a larger dir is truncated
    # with a flag, and the tree loads one level at a time (UDR-0069 D6).
    file_explorer_max_tree_entries: int = 1000
    # Download path (CTR-0136 v2, PRP-0093, UDR-0071 D2). Separate from the
    # editor open/save cap above: a download MAY exceed FILE_EXPLORER_MAX_FILE_BYTES.
    # Upper bound on a single-file download and on a folder ZIP (total uncompressed);
    # an over-cap target is refused with a clear 400.
    file_explorer_max_download_bytes: int = 104_857_600  # 100 MiB
    # Max number of files included in a folder ZIP; a larger folder is refused (400).
    file_explorer_max_download_entries: int = 5000
    # Upload path (CTR-0136 v3, PRP-0104, UDR-0083). Bounds a single multipart
    # POST /api/workspace/upload: FILE_EXPLORER_MAX_UPLOAD_BYTES caps the TOTAL
    # bytes across every file in the request; FILE_EXPLORER_MAX_UPLOAD_FILES caps
    # the file count (a folder upload sends one file per descendant). An over-cap
    # request is refused with a clear 400. Both require FILE_EXPLORER_ENABLED +
    # CODING_ENABLED like every other write path.
    file_explorer_max_upload_bytes: int = 104_857_600  # 100 MiB
    file_explorer_max_upload_files: int = 500

    # Agent Skills (CTR-0042, PRP-0024)
    skills_dir: str = ".skills"

    # Declarative Agents (CTR-0006, CTR-0142..0144, PRP-0094, UDR-0072)
    # Folder of CUSTOM declarative agent YAML files (*.yaml / *.yml, nested folders
    # allowed) discovered through a realpath jail rooted here. Empty (default) =
    # no custom agents; only the bundled CORE agent exists and the runtime is
    # byte-for-byte the pre-PRP-0094 behavior (UDR-0072 D11/D13). The active
    # selection is in-memory only (restart re-initializes to CORE, D7); there is no
    # env var for it. The YAML is a SPECIFICATION; ChatWalaʻau owns construction
    # (D1/D2): credentials / connection are never honored, and sampling params
    # (temperature / top_p / ...) are rejected at activation (D3/D5).
    declarative_agents_dir: str = ""

    # Prompt Templates (CTR-0046, PRP-0026)
    templates_dir: str = ".templates"

    # Slash Commands (CTR-0125, PRP-0088)
    # The four built-in commands (help / prompt / skill / model) are code-backed
    # and always available, so the feature works with zero configuration. This
    # optional JSONC file lets an operator add or override command METADATA;
    # two-tier resolution falls back to a bundled commands.default.jsonc sibling.
    # An explicit empty value disables loading the operator file (built-ins stay).
    commands_config_file: str = "commands.jsonc"

    # Cron Scheduler (CTR-0006, CTR-0130..0135, PRP-0089, UDR-0067)
    # Self-contained, file-backed scheduler. The whole feature is OFF unless
    # CRON_ENABLED is true (UDR-0067 D10). Cron runs are script-only inside the
    # coding workspace and ADDITIONALLY require CODING_ENABLED (UDR-0067 D7/D8).
    cron_enabled: bool = False
    cron_tick_seconds: int = 60  # tick interval; clamped to [5, 3600] at use time
    cron_jobs_dir: str = ".cron"  # per-job JSON files; run logs under <dir>/output/
    # Missed-run catch-up tolerance: lateness beyond this fast-forwards without
    # running (UDR-0067 D4). Clamped to [120, 7200] at use time.
    cron_grace_window_seconds: int = 120
    # IANA timezone cron expressions evaluate in; empty = system local (UDR-0067 D6).
    cron_timezone: str = ""
    cron_run_timeout_seconds: int = 900  # per-run subprocess wall-clock timeout
    cron_output_max_bytes: int = 1_048_576  # cap per captured stdout/stderr log

    # Pipeline Job subsystem (CTR-0006, CTR-0072, CTR-0145/0146, PRP-0096, UDR-0074)
    # In-process, file-backed engine for curated DATA-PROCESSING job types (rag-ingest
    # first). Unlike CRON_ENABLED (default false; arbitrary scripts), pipeline jobs run
    # only curated in-process job types (no shell, no CODING_ENABLED), so the subsystem
    # is ON by default to preserve RAG ingestion availability (UDR-0074 D8). When false
    # the /api/pipeline/* surface 404s, the SPA hides the launcher icon, and the
    # manage_pipeline agent tool is not registered. PIPELINE_JOBS_DIR replaces the
    # removed BATCH_JOBS_DIR (UDR-0074 D11); run logs live under <dir>/output/.
    pipeline_enabled: bool = True
    pipeline_jobs_dir: str = ".pipeline"
    pipeline_output_max_bytes: int = 1_048_576  # cap per captured run log
    pipeline_max_concurrent_jobs: int = 2  # in-process worker pool bound

    # Microsoft Teams Integration (CTR-0006, CTR-0138..0141, PRP-0092, UDR-0070)
    # First external chat-channel integration (FEAT-0050). The whole feature is OFF
    # unless TEAMS_ENABLED (UDR-0070 D10): when false the Teams router is not mounted
    # and the microsoft-teams-apps SDK is not constructed -- byte-for-byte unchanged.
    teams_enabled: bool = False
    # Bot/app CLIENT_ID (Entra application/client id); the Bot Framework JWT audience.
    teams_app_id: str = ""
    # CLIENT_SECRET (Entra client secret). Secret; never logged.
    teams_app_password: str = ""
    # Entra TENANT_ID.
    teams_tenant_id: str = ""
    # Comma-separated Microsoft Entra ID Object IDs (aadObjectId) allowed to use the
    # bot; empty = everyone (UDR-0070 D5). Authorization is per-sender, after JWT.
    teams_allowed_users: str = ""
    # Inbound path mounted into the app and registered as the bot messaging endpoint
    # (with the tunnel host). Default /api/teams/messages (UDR-0070 D3).
    teams_messaging_endpoint_path: str = "/api/teams/messages"
    # Max characters per outbound Teams message chunk (Teams size-limit guard,
    # UDR-0070 D6). A longer reply is split into ordered chunks.
    teams_max_reply_chars: int = 25000

    @property
    def teams_allowed_users_set(self) -> frozenset[str]:
        """Parse TEAMS_ALLOWED_USERS into a set of Entra Object IDs (empty = all)."""
        return frozenset(u.strip() for u in self.teams_allowed_users.split(",") if u.strip())

    # Inbound Webhook Gateway (CTR-0006, CTR-0149..0157, PRP-0097, UDR-0075/0076)
    # A new external-boundary capability (CAP-010). The whole feature is OFF unless
    # WEBHOOK_ENABLED (UDR-0075 D11): when false the /api/webhook/* ingress and
    # /api/webhooks/* management routers are not mounted (404), the maintenance
    # scheduler does not run, the sidebar icon is hidden, the manage_webhook tool is
    # not registered, and the teams-meeting pipeline job type is not registered.
    webhook_enabled: bool = False
    # Public base path for inbound notifications; sources mount at <base>/{source}.
    webhook_ingress_base_path: str = "/api/webhook"
    # Per-source enable/disable state + per-source receipt records live here.
    webhook_store_dir: str = ".webhooks"
    # Cap per captured receipt record body in bytes (0 = unbounded).
    webhook_receipt_max_bytes: int = 1_048_576

    # Microsoft Graph webhook source (the first registered source; UDR-0075 D2)
    # clientState shared secret used to validate notifications (HMAC-safe compare).
    msgraph_webhook_client_state: str = ""
    # Public, tunnel-reachable notification URL given to Graph at subscribe time.
    msgraph_webhook_notification_url: str = ""
    # Graph resource to subscribe (e.g. communications/onlineMeetings/getAllTranscripts).
    msgraph_webhook_resource: str = ""
    # Subscription renewal interval in hours; MUST be shorter than the resource max
    # expiry (UDR-0075 D7). Auto-renewal runs only while CRON_ENABLED (UDR-0075 D8).
    msgraph_subscription_renew_hours: int = 12
    # Optional source CIDR allowlist (comma-separated; empty = off).
    msgraph_webhook_allowed_cidrs: str = ""
    # Optional resource allowlist (comma-separated; empty = off).
    msgraph_webhook_allowed_resources: str = ""

    # Microsoft Graph app-only credentials (dedicated GRAPH_* namespace; UDR-0075 D6)
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_base_url: str = "https://graph.microsoft.com/v1.0"

    # Teams Meeting Pipeline (FEAT-0053, CTR-0156, PRP-0097, UDR-0076)
    # Output subdir within the coding workspace for the summary JSON (CTR-0031 jail).
    teams_meeting_output_dir: str = "meeting-summaries"
    # Summary model = the catalog `roles.meeting_summary` binding (PRP-0115 / UDR-0096);
    # the removed TEAMS_MEETING_SUMMARY_MODEL env var no longer applies.
    # A transcript artifact is often not ready right when a meeting ends. The fetch stage
    # polls for it up to this many seconds (default 600 = 10 min) before failing.
    teams_meeting_transcript_max_wait_seconds: int = 600
    # Poll interval while waiting for the transcript artifact (seconds).
    teams_meeting_transcript_poll_seconds: int = 30

    # Ontology Concept Modeling (CTR-0006, CTR-0169..0173, PRP-0105, UDR-0084)
    # Rule-based concept models as RDF knowledge graphs (Turtle file SSOT +
    # catalog.json, pyoxigraph read-only SPARQL, NL -> SPARQL via the registry
    # chokepoint, session-common query_ontology tool). The whole feature is OFF
    # unless ONTOLOGY_ENABLED (UDR-0084 D12): when false the /api/ontology surface
    # returns 404, the tool is not registered, and the sidebar icon is hidden.
    ontology_enabled: bool = False
    # Folder holding catalog.json + one Turtle file per ontology (created on demand).
    ontology_dir: str = ".ontologies"
    # Upload/import and save size cap in bytes (UDR-0084 D10).
    ontology_max_file_bytes: int = 10_485_760  # 10 MiB
    # Cap on triples serialized into a query_ontology tool answer (UDR-0084 D9);
    # truncation is explicitly noticed in the tool result.
    ontology_tool_max_triples: int = 200
    # The NL -> SPARQL completion model is the catalog `roles.ontology_nl` binding
    # (PRP-0115 / UDR-0096); the removed ONTOLOGY_NL_MODEL env var no longer applies.

    @property
    def msgraph_webhook_allowed_cidr_list(self) -> list[str]:
        """Parse MSGRAPH_WEBHOOK_ALLOWED_CIDRS (empty = no restriction)."""
        return [c.strip() for c in self.msgraph_webhook_allowed_cidrs.split(",") if c.strip()]

    @property
    def msgraph_webhook_allowed_resource_list(self) -> list[str]:
        """Parse MSGRAPH_WEBHOOK_ALLOWED_RESOURCES (empty = no restriction)."""
        return [r.strip() for r in self.msgraph_webhook_allowed_resources.split(",") if r.strip()]

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
    # SameSite policy for the session cookie. Default "lax" (PRP-0097 fix): "strict" drops
    # the cookie on cross-site top-level navigations into the app, which breaks login behind
    # a TLS-terminating tunnel / reverse proxy (e.g. Microsoft Dev Tunnels, whose access
    # page redirects cross-site into the tunnel domain) and causes a login redirect loop.
    # "lax" still blocks cross-site POST cookies (the main CSRF vector). "none" enables
    # cross-site embedding but REQUIRES a Secure cookie (forced on automatically).
    auth_cookie_samesite: str = "lax"
    # PRP-0110 / UDR-0089: persist session token DIGESTS (sha256, never the raw
    # token) so a process restart no longer signs every browser out. Only active
    # when the web auth lane exists (AUTH_USERNAME set). Set false to restore the
    # pre-PRP-0110 in-memory-only behavior byte-for-byte. Deleting the store file
    # logs everyone out; rotating AUTH_PASSWORD_HASH does the same (UDR-0089 D5).
    auth_session_persist: bool = True
    auth_session_store_path: str = ".auth/session_tokens.json"

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

    # Human-interactive approval-round budget (PRP-0103, UDR-0082 D1/D2).
    # Replaces the previously hardcoded 16-round bound on the AG-UI /
    # Teams approval re-run loop. Only rounds that required a human
    # decision (source != "session-cache") count against this budget, so
    # a blanket "approve for session" grant no longer consumes it. When
    # exceeded the run aborts with a "Tool approval loop exceeded"
    # RUN_ERROR. Range 1..1000.
    tool_approval_max_iterations: int = 33

    # Absolute approval-round backstop (PRP-0103, UDR-0082 D2). Counts
    # EVERY round (interactive + session-cached) and bounds a runaway
    # agent that loops on tool calls forever even under a blanket session
    # grant. MUST be >= TOOL_APPROVAL_MAX_ITERATIONS. Range 1..100000.
    tool_approval_absolute_max_iterations: int = 200

    # ---- Multi-Model helpers ----
    # PRP-0113 / UDR-0094 removed the legacy env-namespace model-list accessors
    # and the context-window accessors along with the routing lane. Model routing,
    # default selection, and per-model context windows now come from the Model
    # Offering Catalog through the app.providers module and app.models_catalog.

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
    # PRP-0113 / UDR-0094: the model-routing validators (_validate_models /
    # _validate_anthropic / _validate_foundry) were REMOVED with the legacy
    # env-namespace lane. Model routing is validated by the Model Offering
    # Catalog loader (app.models_catalog.parse_catalog: >=1 chat offering, unique
    # ids, known provider, per-offering hosting) at load, and a non-demo runtime
    # with no chat offering fail-fasts at agent build (app.agui.agent_registry).

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

        cookie_samesite = self.auth_cookie_samesite.strip().lower()
        if cookie_samesite not in {"lax", "strict", "none"}:
            msg = f"AUTH_COOKIE_SAMESITE must be one of: lax, strict, none. Got: {self.auth_cookie_samesite!r}"
            raise ValueError(msg)
        self.auth_cookie_samesite = cookie_samesite

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
    def _validate_prompt_cache(self) -> "Settings":
        """Normalize the Anthropic prompt-cache TTL (PRP-0080, UDR-0056 D3)."""
        ttl = (self.anthropic_prompt_cache_ttl or "").strip().lower()
        if ttl not in {"5m", "1h"}:
            if ttl:
                _logger.warning(
                    "ANTHROPIC_PROMPT_CACHE_TTL=%r is not recognised; using 5m. Allowed: 5m, 1h",
                    self.anthropic_prompt_cache_ttl,
                )
            ttl = "5m"
        self.anthropic_prompt_cache_ttl = ttl
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
        # PRP-0103 / UDR-0082 D2: the interactive budget bounds human
        # decisions; the absolute backstop bounds total rounds and MUST
        # sit at or above it so a runaway loop always has a ceiling.
        if not (1 <= self.tool_approval_max_iterations <= 1000):
            msg = f"TOOL_APPROVAL_MAX_ITERATIONS must be in 1..1000; got {self.tool_approval_max_iterations}"
            raise ValueError(msg)
        if not (1 <= self.tool_approval_absolute_max_iterations <= 100000):
            msg = (
                "TOOL_APPROVAL_ABSOLUTE_MAX_ITERATIONS must be in 1..100000; "
                f"got {self.tool_approval_absolute_max_iterations}"
            )
            raise ValueError(msg)
        if self.tool_approval_absolute_max_iterations < self.tool_approval_max_iterations:
            msg = (
                "TOOL_APPROVAL_ABSOLUTE_MAX_ITERATIONS "
                f"({self.tool_approval_absolute_max_iterations}) must be >= "
                f"TOOL_APPROVAL_MAX_ITERATIONS ({self.tool_approval_max_iterations})"
            )
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
