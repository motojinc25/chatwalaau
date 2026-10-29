// AI Discussion API client (CTR-0272 / CTR-0273, PRP-0232, UDR-0215).
//
// REST calls return the run; `streamAdvance` runs the next phase and hands every AG-UI event
// to `onEvent`. Nothing here retries a request: a lost phase stream leaves the run paused on the
// server, and the person continues it (UDR-0215 D6).
import type { DiscussionMessageMeta, DiscussionRun } from '@/types/chat'

export interface DiscussionPreset {
  id: string
  display_name: string
  charter: string
  color: string
}

export interface DiscussionOptions {
  presets: DiscussionPreset[]
  default_seats: string[]
  models: string[]
  default_model: string
  /** Reasoning efforts per model for a role seat; an empty `allowed` = the model has none. */
  reasoning: Record<string, { allowed: string[]; default: string | null }>
  role_default_effort: string
  limits: {
    default_rounds: number
    max_rounds: number
    max_participants: number
    token_budget: number
    summary_reserve: number
    turn_max_output_tokens: number
    phase_timeout_seconds: number
    tool_calls_per_turn?: number
    tool_result_max_chars?: number
    report_max_output_tokens?: number
  }
  /** Research tools a seat may have (PRP-0234); web search depends on the seat's model. */
  tools?: { id: DiscussionToolId; label: string; available: boolean }[]
  web_search_models?: string[]
  /** PRP-0235: whether the decision model can choose who speaks in a round. */
  speaker_selection_available?: boolean
}

export type SpeakerSelectionMode = 'all' | 'decision'

/** One row of the decision log (PRP-0235, UDR-0218 D3): a completed run from the session index. */
export interface DecisionLogItem {
  thread_id: string
  title: string
  folder_id: string | null
  run_id: string
  parent_run_id: string | null
  topic: string
  status: string
  termination_reason: string | null
  rounds: number
  recommendation: string
  verdict: 'adopt' | 'hold' | 'rediscuss' | null
  note: string
  decided_at: string | null
  decided_by: string | null
  created_at: string
  updated_at: string
}

export type DecisionFilter = 'all' | 'adopt' | 'hold' | 'rediscuss' | 'undecided'

export type DiscussionToolId = 'web_search' | 'rag_search' | 'ontology_query'

/** A role seat carries its model and reasoning; an agent seat uses the agent's own (none sent). */
export interface SeatRequest {
  kind: 'preset' | 'agent'
  preset?: string
  agent_id?: string
  model?: string
  effort?: string
  /** Research tools for this seat (PRP-0234, UDR-0217 D1). */
  tools?: DiscussionToolId[]
}

export interface CreateDiscussionRequest {
  topic: string
  goal: string
  constraints: string
  participants: SeatRequest[]
  rounds: number
  moderator_model?: string
  folder_id?: string | null
  /** No wait after a round, the first check kept (PRP-0234). */
  auto?: boolean
  /** The full report after the summary (default true). */
  report?: boolean
  /** Who speaks in a round (PRP-0235): everyone (default) or chosen by the decision model. */
  speaker_selection?: SpeakerSelectionMode
}

/** Continue discussion (PRP-0233): omitted fields keep the server defaults (1 round, opening
 *  skipped when a conclusion is carried, the same seats). */
export interface ContinueDiscussionRequest {
  focus: string
  rounds?: number
  skip_opening?: boolean
  participants?: SeatRequest[]
  auto?: boolean
  report?: boolean
  speaker_selection?: SpeakerSelectionMode
}

/** A stored message as the input endpoint echoes it (CTR-0014 shape + `discussion`). */
export interface StoredDiscussionMessage {
  message_id: string
  role: 'user' | 'assistant'
  contents: { type: string; text?: string }[]
  discussion: DiscussionMessageMeta
}

export class DiscussionApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    let code: string | undefined
    try {
      const body = await res.json()
      const detail = body?.detail
      if (detail && typeof detail === 'object') {
        message = String(detail.message ?? message)
        code = typeof detail.error === 'string' ? detail.error : undefined
      } else if (typeof detail === 'string') {
        message = detail
      } else if (Array.isArray(body?.detail)) {
        message = 'The discussion request was not valid.'
      }
    } catch {
      // keep the status text
    }
    throw new DiscussionApiError(message, res.status, code)
  }
  return (await res.json()) as T
}

const base = (threadId: string, runId: string) =>
  `/api/discussions/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}`

export const discussionApi = {
  options: () => call<DiscussionOptions>('/api/discussions/options'),
  create: (body: CreateDiscussionRequest) =>
    call<{ thread_id: string; run: DiscussionRun }>('/api/discussions', { method: 'POST', body: JSON.stringify(body) }),
  runs: (threadId: string) => call<{ runs: DiscussionRun[] }>(`/api/discussions/${encodeURIComponent(threadId)}/runs`),
  input: (
    threadId: string,
    runId: string,
    body: {
      request_id: string
      action: 'answer' | 'continue' | 'ask' | 'finish' | 'auto'
      text?: string
      speaker_id?: string
      /** The next round's speakers, chosen by the person (decision mode, PRP-0235). */
      speakers?: string[]
    },
  ) =>
    call<{ run: DiscussionRun; already_applied: boolean; messages: StoredDiscussionMessage[] }>(
      `${base(threadId, runId)}/input`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  pause: (threadId: string, runId: string) =>
    call<{ run: DiscussionRun }>(`${base(threadId, runId)}/pause`, { method: 'POST' }),
  cancel: (threadId: string, runId: string) =>
    call<{ run: DiscussionRun }>(`${base(threadId, runId)}/cancel`, { method: 'POST' }),
  decision: (threadId: string, runId: string, verdict: 'adopt' | 'hold' | 'rediscuss', note = '') =>
    call<{ run: DiscussionRun }>(`${base(threadId, runId)}/decision`, {
      method: 'POST',
      body: JSON.stringify({ verdict, note }),
    }),
  continueRun: (threadId: string, runId: string, body: ContinueDiscussionRequest) =>
    call<{ run: DiscussionRun; messages: StoredDiscussionMessage[] }>(`${base(threadId, runId)}/continue`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  /** The decision log across chats (PRP-0235): completed runs, newest first. */
  decisions: (params: { verdict?: DecisionFilter; q?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.verdict && params.verdict !== 'all') query.set('verdict', params.verdict)
    if (params.q?.trim()) query.set('q', params.q.trim())
    if (params.limit) query.set('limit', String(params.limit))
    if (params.offset) query.set('offset', String(params.offset))
    const suffix = query.toString()
    return call<{ items: DecisionLogItem[]; total: number }>(`/api/discussions/decisions${suffix ? `?${suffix}` : ''}`)
  },
  /** The run as Markdown (PRP-0235): the file name from Content-Disposition and the text. */
  exportRun: async (threadId: string, runId: string, includeLog = false) => {
    const res = await fetch(`${base(threadId, runId)}/export${includeLog ? '?log=true' : ''}`)
    if (!res.ok) throw new DiscussionApiError(`HTTP ${res.status}`, res.status)
    const disposition = res.headers.get('Content-Disposition') ?? ''
    const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? 'discussion.md'
    return { name, text: await res.text() }
  },
}

/** Hand a Markdown export to the browser as a download. */
export function downloadMarkdown(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** Workspace folder an export is saved to (PRP-0235, via the File Explorer, CTR-0136). */
export const EXPORT_FOLDER = 'discussions'

export interface DiscussionStreamEvent {
  type: string
  name?: string
  value?: Record<string, unknown>
  messageId?: string
  delta?: string
  message?: string
  code?: string
  result?: { outcome?: string }
}

/**
 * Run the next phase (CTR-0273) and pass each event to `onEvent`. Resolves with the
 * RUN_FINISHED outcome, or `null` when the stream ended without one (a lost connection --
 * the server has paused the run).
 */
export async function streamAdvance(
  threadId: string,
  runId: string,
  onEvent: (event: DiscussionStreamEvent) => void,
  signal?: AbortSignal,
): Promise<string | null> {
  const res = await fetch(`${base(threadId, runId)}/advance`, { method: 'POST', signal })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    let code: string | undefined
    try {
      const body = await res.json()
      message = String(body?.detail?.message ?? message)
      code = body?.detail?.error
    } catch {
      // keep the status text
    }
    throw new DiscussionApiError(message, res.status, code)
  }
  const reader = res.body?.getReader()
  if (!reader) throw new DiscussionApiError('No response body', 0)
  const decoder = new TextDecoder()
  let buffer = ''
  let outcome: string | null = null
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6).trim()
      if (!data) continue
      try {
        const event = JSON.parse(data) as DiscussionStreamEvent
        if (event.type === 'RUN_FINISHED') outcome = event.result?.outcome ?? 'finished'
        onEvent(event)
      } catch {
        // a malformed line is skipped, like the chat stream does
      }
    }
  }
  return outcome
}
