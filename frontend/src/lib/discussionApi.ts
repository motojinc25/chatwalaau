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
  }
}

/** A role seat carries its model and reasoning; an agent seat uses the agent's own (none sent). */
export interface SeatRequest {
  kind: 'preset' | 'agent'
  preset?: string
  agent_id?: string
  model?: string
  effort?: string
}

export interface CreateDiscussionRequest {
  topic: string
  goal: string
  constraints: string
  participants: SeatRequest[]
  rounds: number
  moderator_model?: string
  folder_id?: string | null
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
    body: { request_id: string; action: 'answer' | 'continue' | 'ask' | 'finish'; text?: string; speaker_id?: string },
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
  continueRun: (threadId: string, runId: string, focus = '') =>
    call<{ run: DiscussionRun; messages: StoredDiscussionMessage[] }>(`${base(threadId, runId)}/continue`, {
      method: 'POST',
      body: JSON.stringify({ focus }),
    }),
}

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
