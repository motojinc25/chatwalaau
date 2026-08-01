export interface ToolCall {
  id: string
  name: string
  status: 'running' | 'completed'
  args?: string
  result?: string
}

export interface ReasoningBlock {
  id: string
  content: string
  status: 'thinking' | 'done'
}

export interface ImageRef {
  uri: string
  media_type: string
}

export interface UsageInfo {
  input_token_count?: number
  output_token_count?: number
  total_token_count?: number
  max_context_tokens?: number
  /** Model that produced this turn (CTR-0009 usage event). */
  model?: string
  /** Reasoning effort used for this turn (CTR-0030, PRP-0071). */
  reasoning?: string
  /** Text verbosity used for this turn (gpt-5.x only; CTR-0030, PRP-0081). */
  verbosity?: string
  /** Structured output active for this turn (CTR-0009 v14, PRP-0082). */
  structured?: boolean
  /** Run-target that produced this turn -- the Built-in / Prompt agent name, or the
   * workflow name (v0.112.2). Persisted here (like `model`) so a reloaded chat still
   * shows which agent / workflow answered. */
  run_target?: string
  /** Declarative workflow completion marker (v0.115.1). A workflow run that produced no
   * chat reply persists this so the turn reloads as a compact "Workflow complete"
   * indicator instead of an empty bubble; `steps` is the completed-node count. */
  workflow_completed?: { steps: number }
  /**
   * Full declarative workflow run record (v0.117.1). Persists what the run indicator and
   * the run canvas need to be REBUILT when an old chat is reloaded: the workflow's identity
   * and every step's final state. Per-action payload logs are deliberately NOT persisted --
   * they are unbounded in aggregate and belong to the live run.
   */
  workflow_run?: PersistedWorkflowRun
  /**
   * Soft, non-blocking validation status of the structured answer (UDR-0058 D4).
   * `parsed` false means the JSON was empty (refusal) or unparseable (truncation /
   * a non-native fallback that did not conform); it never blocks the turn.
   */
  output_status?: { parsed: boolean; reason?: string }
}

/** One persisted entry of a step's processing log (v0.117.1). */
export interface PersistedWorkflowLogEntry {
  seq: number
  status: 'idle' | 'running' | 'completed' | 'skipped' | 'failed' | 'awaiting_input'
  message?: string
  payload?: unknown
  truncated?: boolean
  iteration?: number | null
}

/** A workflow run as saved with its assistant message (v0.117.1). */
export interface PersistedWorkflowRun {
  workflow_id: string
  workflow_name: string
  status: 'running' | 'completed' | 'failed' | 'awaiting_input'
  steps: number
  skipped?: number
  error?: string
  /**
   * The last variable snapshot of the run (v0.117.1), so a reloaded chat can still show
   * what the workflow ended up holding. Already redacted and size-bounded by the backend
   * inspector (CTR-0189) before it ever reached the client.
   */
  snapshot?: {
    iteration: number | null
    namespaces: Record<string, Record<string, unknown>>
    truncated?: boolean
    redactedKeys?: number
  }
  /** True when per-step logs were dropped to stay within the persistence budget. */
  logs_truncated?: boolean
  nodes: Array<{
    node: string
    label: string
    status: 'idle' | 'running' | 'completed' | 'skipped' | 'failed' | 'awaiting_input'
    origin?: 'action' | 'derived' | 'unmapped' | 'internal'
    iteration?: number | null
    message?: string
    /** The step's processing log, most recent entries first-dropped under budget. */
    log?: PersistedWorkflowLogEntry[]
  }>
}

export type ActivityEntry = { type: 'reasoning'; id: string } | { type: 'toolCall'; id: string }

export interface McpAppEvent {
  server_name: string
  tool_name: string
  resource_uri: string
  html_ref: string
  csp?: Record<string, string[]>
  permissions?: Record<string, unknown>
  call_id: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: string
  toolCalls?: ToolCall[]
  reasoningBlocks?: ReasoningBlock[]
  activityLog?: ActivityEntry[]
  images?: ImageRef[]
  usage?: UsageInfo
  mcpApp?: McpAppEvent
  /** Model that generated this assistant message (CTR-0071, PRP-0035) */
  model?: string
  /** Run-target that produced this turn: a workflow name or a non-default agent name
   * (CTR-0185, PRP-0118). Shown in the action bar so the operator sees which agent /
   * workflow answered. */
  runTarget?: string
  /** v0.115.1: set when this assistant turn is a declarative workflow that produced no
   * chat reply. Rendered as a compact "Workflow complete" indicator instead of an empty
   * bubble; restored from `usage.workflow_completed` on reload. */
  workflowCompleted?: { steps: number }
  /** Restored workflow run (v0.117.1): rebuilds the indicator and the run canvas. */
  workflowRun?: PersistedWorkflowRun
  /** Reasoning effort used for this assistant message (CTR-0030, PRP-0071) */
  reasoning?: string
  /** Text verbosity used for this assistant message (gpt-5.x only; CTR-0030, PRP-0081) */
  verbosity?: string
  /**
   * Structured output (CTR-0118 / CTR-0012 v11, PRP-0082). When true the content is
   * JSON Schema-constrained and is rendered as a JSON code block instead of Markdown.
   */
  structured?: boolean
  /**
   * PRP-0110 / CTR-0004 v2. Set on a USER message whose send failed before the
   * AG-UI stream committed (server down / restarting). The turn never reached the
   * agent and was never persisted, so it renders with an inline error and a Retry
   * affordance. Ephemeral: never written to the session file.
   */
  failed?: boolean
}

export interface PromptTemplate {
  id: string
  name: string
  description: string
  category: string
  body: string
  /** Optional slash command token for /prompt (CTR-0047 v2, PRP-0088). */
  slash_command?: string
  created_at: string
  updated_at: string
}

// Preset folder palette tokens (UDR-0046 D2). The record stores the KEY; the
// sidebar maps it to theme-controlled classes. Keep in sync with the backend
// FOLDER_COLORS tuple in app/session/storage.py.
export const FOLDER_COLORS = ['neutral', 'red', 'orange', 'amber', 'green', 'blue', 'violet', 'pink'] as const

export type FolderColor = (typeof FOLDER_COLORS)[number]

export const DEFAULT_FOLDER_COLOR: FolderColor = 'neutral'

export interface SessionFolder {
  id: string
  name: string
  color: FolderColor
  order: number
  created_at: string
  updated_at: string
  /**
   * How many chats the folder holds, across the WHOLE store (CTR-0015, v0.106.2).
   *
   * Server-supplied on purpose. Since PRP-0112 a folder's sessions are fetched only when
   * it is expanded (UDR-0091 D4), so counting them client-side from the loaded `sessions`
   * yields 0 for every collapsed folder -- which is exactly the bug this field fixes.
   */
  session_count: number
}

export interface SessionSummary {
  thread_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
  image_count: number
  pinned_at: string | null
  folder_id: string | null
  source?: 'ag-ui' | 'openai-api' | 'teams'
  /**
   * Auto Session Title pending state (PRP-0077, CTR-0109). True while the
   * background title task is running; drives the sidebar spinner. Cleared by
   * the CTR-0110 `session_title` WebSocket push or on the next list refresh.
   */
  auto_title_pending?: boolean
  /**
   * Non-fatal import notices (CTR-0015 v1.17). Present on an imported session
   * summary when some attachment was carried with a caveat or skipped; empty
   * on a clean import.
   */
  warnings?: string[]
}

/**
 * Result of a chat bundle import (CTR-0015 v1.17 / CTR-0016 v5). On failure
 * `error` holds the server's human-readable reason so the UI can surface it
 * (previously the failure was swallowed silently). On success `warnings` lists
 * any attachment that was skipped or carried with a caveat -- the import still
 * completed, but the operator is told it may not be perfectly faithful.
 */
export interface ImportResult {
  ok: boolean
  error?: string
  warnings?: string[]
}

/**
 * Result of a session-list action that can fail server-side (CTR-0015 / CTR-0016 v6,
 * v0.117.4). Archiving writes to a directory derived from SESSIONS_DIR, so it can
 * fail for reasons only the server knows (read-only or unmounted volume). `error`
 * carries the server's human-readable reason so the sidebar can show it -- the
 * failure used to be swallowed, leaving a bare 500 in the browser console as the
 * only sign that the click did nothing.
 */
export interface SessionActionResult {
  ok: boolean
  error?: string
}
