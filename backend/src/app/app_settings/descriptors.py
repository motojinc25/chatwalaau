"""Application Settings descriptor registry (CTR-0198, PRP-0136, UDR-0120).

The registry is the backend-owned description of every operator-tunable setting
that lives in ``app_settings.jsonc`` instead of ``.env``. The App Settings screen
(CTR-0176) renders purely from these descriptors, so adding a key in a later
release is a backend-only change (UDR-0120 D8) -- the property ``role_registry``
(UDR-0096 D6) already established for task-model rows.

Two rules govern what a descriptor may say (UDR-0120 D4):

1. A descriptor carries METADATA ONLY -- label, group, control type, enum,
   scope, help. It MUST NOT carry a ``default``. The default is DERIVED from
   ``Settings.model_fields[key].default`` at call time, so ``Settings`` stays the
   single source of truth for both defaults and types and there is no third copy
   to drift (the first two being the field definition and the ``.env.template``
   comment that UDR-0039 D1 owns).
2. ``env_name`` is likewise derived (``key.upper()``), not restated. It is
   presentation only -- a hint so an operator migrating from ``.env`` can find
   the row -- because the store keys by ``Settings`` field name.

Eligibility (UDR-0120 D1) is NOT re-litigated per key: a key may appear here only
if it is neither bootstrap, nor secret, nor a security boundary. Note in
particular that a feature GATE (``CODING_ENABLED``) stays in ``.env`` while that
feature's BOUND (``CODING_BASH_TIMEOUT``) appears here -- a bound narrows what an
already-enabled feature may do, a gate decides whether it exists at all, and only
the second is a security decision.

``scope`` is three-valued (UDR-0120 D3) and each value was assigned by MEASURING
where the field is consumed, not by guessing:

- ``runtime``  -- the consumer dereferences ``settings.<field>`` at call time, so
                  assigning to the singleton is sufficient and the change applies
                  immediately.
- ``rebuild``  -- the value is baked into a tool or agent at construction, so the
                  apply path must additionally run the CTR-0070 agent rebuild.
- ``restart``  -- the value is injected into a service at ASGI construction, or
                  into a cached object that cannot be safely re-created in
                  flight; the apply path persists only and reports
                  ``restart_required``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings

# ---- Scope values (UDR-0120 D3) ------------------------------------------

SCOPE_RUNTIME = "runtime"
SCOPE_REBUILD = "rebuild"
SCOPE_RESTART = "restart"

SCOPES: frozenset[str] = frozenset({SCOPE_RUNTIME, SCOPE_REBUILD, SCOPE_RESTART})


@dataclass(frozen=True)
class SettingGroup:
    """A GUI section in the App Settings screen's settings-item list."""

    key: str
    label: str
    description: str


@dataclass(frozen=True)
class SettingDescriptor:
    """One operator-tunable setting. Metadata only -- no default (UDR-0120 D4)."""

    key: str
    label: str
    group: str
    type: str  # "bool" | "int" | "str" | "enum"
    scope: str
    help: str = ""
    enum: tuple[str, ...] | None = None
    # Predecessor key absorbed ONCE at load and rewritten under `key` (D7). An
    # absorbed name is never reused for a different meaning.
    renamed_from: str | None = None
    # A key kept only so an existing file keeps loading; hidden from the main
    # composer and shown as deprecated.
    deprecated: bool = False

    @property
    def env_name(self) -> str:
        """The historical .env spelling. Presentation only -- the store keys by `key`."""
        return self.key.upper()


GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup(
        "generation",
        "Generation & inference",
        "Token budgets, prompt caching, and hosted web search behaviour.",
    ),
    SettingGroup(
        "chat",
        "Chat & session",
        "Titling, attachment handling, import limits, and history compaction.",
    ),
    SettingGroup(
        "memory",
        "Memory",
        "Identity, user profile, and agent-curated memory limits and toggles.",
    ),
    SettingGroup(
        "speech",
        "Speech",
        "Speech-to-text and text-to-speech deployments and voices.",
    ),
    SettingGroup(
        "rag",
        "RAG",
        "Vector collection and retrieval breadth for the rag_search tool.",
    ),
    SettingGroup(
        "limits",
        "Limits",
        "Upper bounds on coding tools, the file explorer, pipelines, and replies.",
    ),
    SettingGroup(
        "schedule",
        "Schedule",
        "Cron tick cadence, run timeouts, grace window, and timezone.",
    ),
)


DESCRIPTORS: tuple[SettingDescriptor, ...] = (
    # ---- Generation / inference (runtime) --------------------------------
    SettingDescriptor(
        "anthropic_max_tokens",
        "Anthropic max tokens",
        "generation",
        "int",
        SCOPE_RUNTIME,
        help=(
            "Floor on the Anthropic max_tokens budget, which caps thinking and answer "
            "text COMBINED. The per-effort tier is the effective value; this only ever "
            "raises it, never lowers it."
        ),
    ),
    SettingDescriptor(
        "prompt_cache_enabled",
        "Prompt caching",
        "generation",
        "bool",
        SCOPE_RUNTIME,
        help=(
            "Mark the large stable prefix (system prompt + tool schemas) as cacheable. "
            "Output-transparent: only billing and latency change."
        ),
    ),
    SettingDescriptor(
        "anthropic_prompt_cache_ttl",
        "Anthropic cache TTL",
        "generation",
        "enum",
        SCOPE_RUNTIME,
        enum=("5m", "1h"),
        help="1h adds the extended-cache beta header. Only the anthropic provider reads this.",
    ),
    SettingDescriptor(
        "web_search_country",
        "Web search country",
        "generation",
        "str",
        SCOPE_RUNTIME,
        help="Two-letter country code used as the approximate user location for hosted web search.",
    ),
    # ---- Chat / session (runtime) ----------------------------------------
    SettingDescriptor(
        "session_title_mode",
        "Chat title mode",
        "chat",
        "enum",
        SCOPE_RUNTIME,
        enum=("truncate", "llm"),
        help=(
            "truncate keeps the leading characters of the first user message; llm "
            "summarizes it in the background using the session_title task model."
        ),
    ),
    SettingDescriptor(
        "pdf_attach_mode",
        "PDF attachment mode",
        "chat",
        "enum",
        SCOPE_RUNTIME,
        enum=("auto", "native", "text"),
        help=(
            "auto attaches the raw PDF where the provider supports it and extracts text "
            "elsewhere. text is the safe fallback if a deployment rejects input_file."
        ),
    ),
    SettingDescriptor(
        "pdf_inline_max_chars",
        "PDF text character cap",
        "chat",
        "int",
        SCOPE_RUNTIME,
        help="Cap on TEXT-extracted PDF content; beyond it the text is truncated with a marker.",
    ),
    SettingDescriptor(
        "session_import_max_bytes",
        "Chat import size cap (bytes)",
        "chat",
        "int",
        SCOPE_RUNTIME,
        help="Maximum accepted size of an uploaded chat import bundle (.zip).",
    ),
    SettingDescriptor(
        "temporary_chat_retention_days",
        "Temporary chat retention (days)",
        "chat",
        "int",
        SCOPE_RUNTIME,
        help="Quarantine retention for temporary chats. 0 or less disables the sweep.",
    ),
    # Compaction (PRP-0140 / UDR-0120 D3 correction). All three are `rebuild`, NOT
    # `runtime`: resolve_compaction_strategy() runs at AgentRegistry construction
    # (agent_factory.py -> Agent(compaction_strategy=...)), and the per-model Agent
    # objects are cached with the strategy instance baked in -- so assigning to the
    # singleton cannot reach them. PRP-0136 shipped `compaction_strategy` as
    # `runtime`, which badged a save "Applies immediately" while the running agents
    # kept the old strategy. The CTR-0070 rebuild re-runs the resolver, so `rebuild`
    # is correct and `restart` would be a needless demand.
    SettingDescriptor(
        "compaction_strategy",
        "History compaction strategy",
        "chat",
        "enum",
        SCOPE_REBUILD,
        enum=("none", "sliding-window", "selective-tool-call", "tool-result"),
        help=(
            "In-memory only -- the on-disk session JSON is never mutated, so switching "
            "back to none fully restores the model's view of history."
        ),
    ),
    SettingDescriptor(
        "compaction_keep_last_groups",
        "Compaction: message groups kept",
        "chat",
        "int",
        SCOPE_REBUILD,
        help=(
            "How many recent message groups the sliding-window strategy keeps verbatim. "
            "Values outside 1..32 fall back to the default with a warning."
        ),
    ),
    SettingDescriptor(
        "compaction_preserve_system",
        "Compaction: keep the system prompt",
        "chat",
        "bool",
        SCOPE_REBUILD,
        help="Exempt the system prompt from compaction so the agent's instructions survive.",
    ),
    # ---- Memory ------------------------------------------------------------
    # NOTE (PRP-0140): `user_profile_enabled` and `agent_memory_enabled` are
    # `rebuild`, not `runtime`. Both gate TOOL REGISTRATION inside
    # _build_tools_and_instructions() (agent_factory.py:339 / :351), which runs at
    # AgentRegistry construction -- so toggling one by assignment leaves the
    # already-built agents with their old tool list. Found by the strengthened
    # UDR-0120 D3 guard, which follows main.py's module-level calls into app.*;
    # the original literal-only scan could not see them. The CTR-0070 rebuild
    # re-runs the factory, so `rebuild` is correct and `restart` unnecessary.
    SettingDescriptor(
        "identity_char_limit",
        "Identity character limit",
        "memory",
        "int",
        SCOPE_RUNTIME,
        help=".agent/IDENTITY.md cap. Identity is injected into every request, so this bounds prompt bloat.",
    ),
    SettingDescriptor(
        "user_profile_enabled",
        "User profile memory",
        "memory",
        "bool",
        SCOPE_REBUILD,
        help="Master toggle for the .agent/USER.md block, the inline memory tool, and snapshot capture.",
    ),
    SettingDescriptor(
        "user_char_limit",
        "User profile character limit",
        "memory",
        "int",
        SCOPE_RUNTIME,
        help="Bounds the curated user entries; an add that would exceed it is rejected with guidance.",
    ),
    SettingDescriptor(
        "user_memory_extraction",
        "User memory extraction",
        "memory",
        "bool",
        SCOPE_RUNTIME,
        help="Background pass that distills conversations into durable preferences. Also requires the user profile toggle.",
    ),
    SettingDescriptor(
        "user_memory_extraction_every_n_turns",
        "Extraction interval (turns)",
        "memory",
        "int",
        SCOPE_RUNTIME,
        help="The extraction runs at most once every N new user turns. Clamped to at least 1.",
    ),
    SettingDescriptor(
        "agent_memory_enabled",
        "Agent curated memory",
        "memory",
        "bool",
        SCOPE_REBUILD,
        help="Master toggle for .agent/MEMORY.md, the manage_memory tool, and the per-turn like UI.",
    ),
    SettingDescriptor(
        "memory_char_limit",
        "Agent memory character limit",
        "memory",
        "int",
        SCOPE_RUNTIME,
        help="Bounds the serialized agent memory body; an over-cap edit is rejected with guidance.",
    ),
    # ---- Speech (restart -- injected at app construction) -----------------
    SettingDescriptor(
        "tts_provider",
        "TTS provider",
        "speech",
        "enum",
        SCOPE_RESTART,
        enum=("elevenlabs", "azure-realtime"),
        help="elevenlabs uses ELEVENLABS_API_KEY (kept in .env); azure-realtime uses the shared Azure lane.",
    ),
    SettingDescriptor(
        "tts_model_id",
        "ElevenLabs model id",
        "speech",
        "str",
        SCOPE_RESTART,
        help="Only consumed by the elevenlabs provider.",
    ),
    SettingDescriptor(
        "tts_voice_id",
        "ElevenLabs voice id",
        "speech",
        "str",
        SCOPE_RESTART,
        help="Only consumed by the elevenlabs provider.",
    ),
    SettingDescriptor(
        "tts_realtime_deployment",
        "Realtime TTS deployment",
        "speech",
        "str",
        SCOPE_RESTART,
        help="Azure voice Realtime deployment name. Required when the provider is azure-realtime.",
    ),
    SettingDescriptor(
        "tts_realtime_voice",
        "Realtime TTS voice",
        "speech",
        "str",
        SCOPE_RESTART,
        help="Voice name such as alloy, marin, or cedar.",
    ),
    SettingDescriptor(
        "tts_realtime_audio_rate",
        "Realtime TTS sample rate (Hz)",
        "speech",
        "enum",
        SCOPE_RESTART,
        enum=("16000", "24000"),
        help="Output PCM sample rate before MP3 encoding.",
    ),
    SettingDescriptor(
        "whisper_deployment_name",
        "STT deployment",
        "speech",
        "str",
        SCOPE_RESTART,
        help="Azure OpenAI speech-to-text deployment. Empty disables transcription.",
    ),
    SettingDescriptor(
        "whisper_model_kind",
        "STT transport",
        "speech",
        "enum",
        SCOPE_RESTART,
        enum=("auto", "rest", "realtime"),
        help="auto infers from the deployment name (a 'realtime' substring selects the WebSocket path).",
    ),
    SettingDescriptor(
        "whisper_realtime_connection_deployment",
        "Realtime STT connection deployment",
        "speech",
        "str",
        SCOPE_RESTART,
        help=(
            "The VOICE Realtime deployment used for the WebSocket URL. Set this when the "
            "STT deployment is transcription-only (e.g. gpt-realtime-whisper)."
        ),
    ),
    SettingDescriptor(
        "whisper_realtime_audio_rate",
        "Realtime STT sample rate (Hz)",
        "speech",
        "enum",
        SCOPE_RESTART,
        enum=("16000", "24000"),
        help="Input audio is resampled to this rate before being appended to the buffer.",
    ),
    SettingDescriptor(
        "azure_openai_realtime_api_version",
        "Realtime API version",
        "speech",
        "str",
        SCOPE_RESTART,
        help=(
            "Empty selects the GA Realtime path (required by 2025-08-28+ models). A value "
            "such as 2025-04-01-preview selects the preview path for legacy models."
        ),
    ),
    # ---- RAG (rebuild -- baked into tool construction) --------------------
    SettingDescriptor(
        "rag_collection_name",
        "Vector collection",
        "rag",
        "str",
        SCOPE_REBUILD,
        help="Collection the rag_search tool queries. Shared with the pipeline ingest job.",
    ),
    SettingDescriptor(
        "rag_top_k",
        "Search results (top K)",
        "rag",
        "int",
        SCOPE_REBUILD,
        help="Number of chunks the rag_search tool returns per query.",
    ),
    # Ingest chunking (PRP-0137 / UDR-0121 D3). `runtime` by measurement: the
    # rag-ingest job reads them inside the job coroutine, so an assignment
    # reaches the next job with no rebuild and no restart. Each is the operator
    # DEFAULT -- an explicit job.params value still wins.
    SettingDescriptor(
        "rag_chunk_size",
        "Ingest chunk size (characters)",
        "rag",
        "int",
        SCOPE_RUNTIME,
        help=(
            "Target size of each chunk the ingest job writes. Lower it when a rag_search "
            "result overflows the model context. A per-job chunk_size parameter overrides this."
        ),
    ),
    SettingDescriptor(
        "rag_chunk_overlap",
        "Ingest chunk overlap (characters)",
        "rag",
        "int",
        SCOPE_RUNTIME,
        help="How much text consecutive chunks share, so a fact spanning a boundary stays retrievable.",
    ),
    SettingDescriptor(
        "rag_chunk_min_size",
        "Ingest minimum chunk size (characters)",
        "rag",
        "int",
        SCOPE_RUNTIME,
        help="Trailing chunks below this size are merged into the previous one. 0 disables the merge.",
    ),
    # ---- Limits -----------------------------------------------------------
    SettingDescriptor(
        "coding_bash_timeout",
        "Shell timeout (seconds)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Wall-clock cap on a single bash_execute call. Also bounds skill script execution.",
    ),
    SettingDescriptor(
        "coding_max_output_chars",
        "Shell output cap (characters)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Captured stdout/stderr beyond this is truncated before it reaches the model.",
    ),
    SettingDescriptor(
        "coding_max_turns",
        "Coding agent max turns",
        "limits",
        "int",
        SCOPE_REBUILD,
        help="Bound on the coding tool loop. Baked into the agent, so applying it rebuilds the registry.",
    ),
    SettingDescriptor(
        "coding_file_read_max_bytes",
        "File read cap (bytes)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Upper bound on bytes returned by a single file_read call.",
    ),
    SettingDescriptor(
        "file_explorer_max_file_bytes",
        "Explorer file size cap (bytes)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Largest file the workspace explorer will open for viewing or editing.",
    ),
    SettingDescriptor(
        "file_explorer_max_tree_entries",
        "Explorer tree entry cap",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Maximum entries returned in one directory listing.",
    ),
    SettingDescriptor(
        "file_explorer_max_download_bytes",
        "Explorer download cap (bytes)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Total uncompressed size cap on a workspace download.",
    ),
    SettingDescriptor(
        "file_explorer_max_download_entries",
        "Explorer download entry cap",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Maximum number of files included in one workspace download.",
    ),
    SettingDescriptor(
        "file_explorer_max_upload_bytes",
        "Explorer upload cap (bytes)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Total size cap on one workspace upload.",
    ),
    SettingDescriptor(
        "file_explorer_max_upload_files",
        "Explorer upload file cap",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Maximum number of files accepted in one workspace upload.",
    ),
    SettingDescriptor(
        "pipeline_output_max_bytes",
        "Pipeline log cap (bytes)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Cap per captured pipeline run log.",
    ),
    SettingDescriptor(
        "pipeline_max_concurrent_jobs",
        "Pipeline worker pool size",
        "limits",
        "int",
        SCOPE_RESTART,
        help=(
            "In-process worker bound. Restart-scope because the semaphore is created once "
            "and has no safe in-flight resize; re-creating it while workers hold permits "
            "would let the pool exceed its bound."
        ),
    ),
    SettingDescriptor(
        "workflow_max_iterations",
        "Workflow superstep cap",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Bound on declarative workflow supersteps. Clamped to at least 1 at use time.",
    ),
    SettingDescriptor(
        "workflow_http_timeout_ms",
        "Workflow HTTP timeout (ms)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Per-request timeout for workflow HTTP actions. The host allowlist stays in .env.",
    ),
    SettingDescriptor(
        "teams_max_reply_chars",
        "Teams reply cap (characters)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Longest reply posted back to a Teams conversation before truncation.",
    ),
    # Teams meeting summarization bounds (PRP-0140). `runtime` by measurement: the
    # meeting pipeline reads all three inside the job coroutine, so an assignment
    # reaches the next run with no rebuild and no restart. They live in `limits`
    # rather than an eighth group, next to teams_max_reply_chars.
    SettingDescriptor(
        "teams_meeting_output_dir",
        "Meeting summary folder",
        "limits",
        "str",
        SCOPE_RUNTIME,
        help=(
            "Folder for meeting summary JSON, RELATIVE to CODING_WORKSPACE_DIR. It is "
            "resolved through the CTR-0031 realpath jail, so a value that would escape "
            "the workspace fails the job rather than writing outside it."
        ),
    ),
    SettingDescriptor(
        "teams_meeting_transcript_max_wait_seconds",
        "Transcript wait limit (seconds)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="How long to keep polling for a transcript before giving up. Graph publishes it minutes after a meeting ends.",
    ),
    SettingDescriptor(
        "teams_meeting_transcript_poll_seconds",
        "Transcript poll interval (seconds)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Delay between transcript polls. Clamped to a 5 second floor at use time.",
    ),
    SettingDescriptor(
        "ontology_max_file_bytes",
        "Ontology file cap (bytes)",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="Largest ontology document accepted.",
    ),
    SettingDescriptor(
        "ontology_tool_max_triples",
        "Ontology query triple cap",
        "limits",
        "int",
        SCOPE_RUNTIME,
        help="CONSTRUCT results beyond this are truncated with a note to the model.",
    ),
    # ---- Schedule (runtime) -----------------------------------------------
    SettingDescriptor(
        "cron_tick_seconds",
        "Scheduler tick (seconds)",
        "schedule",
        "int",
        SCOPE_RUNTIME,
        help="How often the scheduler wakes to check due jobs. Clamped to 5..3600 at use time.",
    ),
    SettingDescriptor(
        "cron_grace_window_seconds",
        "Missed-run grace window (seconds)",
        "schedule",
        "int",
        SCOPE_RUNTIME,
        help="How late a missed run may still fire after a restart or a paused loop.",
    ),
    SettingDescriptor(
        "cron_run_timeout_seconds",
        "Run timeout (seconds)",
        "schedule",
        "int",
        SCOPE_RUNTIME,
        help="Wall-clock cap on a single scheduled run's subprocess.",
    ),
    SettingDescriptor(
        "cron_timezone",
        "Scheduler timezone",
        "schedule",
        "str",
        SCOPE_RUNTIME,
        help="IANA timezone name for cron expressions. Empty uses the host's local timezone.",
    ),
    SettingDescriptor(
        "cron_output_max_bytes",
        "Run log cap (bytes)",
        "schedule",
        "int",
        SCOPE_RUNTIME,
        help="Cap per captured stdout/stderr log of a scheduled run.",
    ),
)


_BY_KEY: dict[str, SettingDescriptor] = {d.key: d for d in DESCRIPTORS}

# Predecessor key -> successor descriptor key (UDR-0120 D7). Empty today; an entry
# appears only when a key is renamed, and the predecessor is then permanently
# reserved (never reused for a different meaning).
RENAMED_FROM: dict[str, str] = {d.renamed_from: d.key for d in DESCRIPTORS if d.renamed_from}

# Keys that once existed here and were RETIRED outright -- no successor, the
# behaviour is gone (UDR-0120 D7). Listing one is what lets the key-removal
# invariant test tell a deliberate retirement from an accidental deletion; the
# test fails on a key that vanishes from DESCRIPTORS without landing here or in
# RENAMED_FROM. A retired name is permanently reserved and MUST NOT be reused for
# a different meaning, exactly like a retired ANCA ID.
RETIRED_KEYS: frozenset[str] = frozenset()


def descriptor(key: str) -> SettingDescriptor | None:
    """Return the descriptor for a settings key, or None when it is unknown."""
    return _BY_KEY.get(key)


def known_keys() -> frozenset[str]:
    """Every key the store owns. Anything else in the file is an unknown key (D5)."""
    return frozenset(_BY_KEY)


def default_for(key: str) -> Any:
    """Derive a key's default from ``Settings`` -- never from the descriptor (D4).

    Raises KeyError for a key that is not a ``Settings`` field, which is a
    programming error the invariant tests catch before it ships.
    """
    return Settings.model_fields[key].default


def annotation_for(key: str) -> Any:
    """The declared type of a key, used to coerce operator input (D4)."""
    return Settings.model_fields[key].annotation


def group_registry() -> list[dict[str, str]]:
    """Group descriptors for the App Settings screen's settings-item list (D8)."""
    return [{"key": g.key, "label": g.label, "description": g.description} for g in GROUPS]


def descriptor_registry() -> list[dict[str, Any]]:
    """Serialize the descriptors for the management API (CTR-0199 GET).

    ``default``, ``env_name``, and ``requires_restart`` are DERIVED here rather
    than stored on the descriptor (D4): the first from ``Settings``, the second
    from the key, the third from ``scope``. A stored copy of any of the three
    would be a second place to change when the first one moves.
    """
    return [
        {
            "key": d.key,
            "env_name": d.env_name,
            "label": d.label,
            "group": d.group,
            "type": d.type,
            "enum": list(d.enum) if d.enum else None,
            "scope": d.scope,
            "requires_restart": d.scope == SCOPE_RESTART,
            "help": d.help,
            "default": default_for(d.key),
            "deprecated": d.deprecated,
        }
        for d in DESCRIPTORS
    ]


__all__ = [
    "DESCRIPTORS",
    "GROUPS",
    "RENAMED_FROM",
    "RETIRED_KEYS",
    "SCOPES",
    "SCOPE_REBUILD",
    "SCOPE_RESTART",
    "SCOPE_RUNTIME",
    "SettingDescriptor",
    "SettingGroup",
    "annotation_for",
    "default_for",
    "descriptor",
    "descriptor_registry",
    "group_registry",
    "known_keys",
]
