/**
 * Tool Approval hook (CTR-0100, PRP-0067, UDR-0043).
 *
 * Owns the SPA-side state for parked tool calls awaiting operator
 * approval. Consumed by ChatPanel to render `ToolApprovalCard` inline.
 *
 * Wire-up:
 *   - useChat receives `onCustomEvent` callback from this hook so that
 *     AG-UI `tool_approval_request` and `tool_approval_response` CUSTOM
 *     events flow into local state.
 *   - The card's Approve / Reject / Approve-for-session buttons POST to
 *     `/api/tool-approval` (CTR-0099 part 2). On success the server
 *     emits a matching `tool_approval_response` event, which the
 *     onCustomEvent handler turns into a resolution chip.
 *
 * Per UDR-0043 D6 no state here is persisted to localStorage / session
 * JSON; reloading the page drops every pending request.
 */

import { useCallback, useState } from 'react'

export type ApprovalSource = 'user' | 'session-cache' | 'timeout' | 'abort' | 'api-auto'

export interface ToolApprovalRequest {
  id: string
  callId: string
  toolName: string
  arguments: Record<string, unknown>
  expiresAtUnix: number
  cachedDecision: boolean | null
  /** PRP-0103 / UDR-0082 D3: 1-based interactive-round number (frozen under a session grant). */
  iteration: number | null
  /** PRP-0103 / UDR-0082 D3: the configured interactive-round budget (TOOL_APPROVAL_MAX_ITERATIONS). */
  maxIterations: number | null
}

export interface ToolApprovalResolution {
  id: string
  approved: boolean
  source: ApprovalSource
  resolvedAtUnix: number
}

export interface ToolApprovalState {
  pending: ToolApprovalRequest[]
  resolutions: Map<string, ToolApprovalResolution>
}

export interface ToolApprovalApi extends ToolApprovalState {
  /** Forwarded to useChat as its `onCustomEvent` callback. */
  ingestCustomEvent: (name: string | undefined, value: Record<string, unknown> | undefined) => void
  approve: (id: string, opts?: { rememberForSession?: boolean }) => Promise<void>
  reject: (id: string) => Promise<void>
  /** Drop every record (used on session switch). */
  reset: () => void
}

function normalizeRequest(value: Record<string, unknown>): ToolApprovalRequest | null {
  const id = typeof value.id === 'string' ? value.id : null
  if (!id) return null
  return {
    id,
    callId: typeof value.call_id === 'string' ? value.call_id : '',
    toolName: typeof value.tool_name === 'string' ? value.tool_name : '<unknown>',
    arguments:
      value.arguments && typeof value.arguments === 'object' ? (value.arguments as Record<string, unknown>) : {},
    expiresAtUnix: typeof value.expires_at_unix === 'number' ? value.expires_at_unix : 0,
    cachedDecision: typeof value.cached_decision === 'boolean' ? value.cached_decision : null,
    iteration: typeof value.iteration === 'number' ? value.iteration : null,
    maxIterations: typeof value.max_iterations === 'number' ? value.max_iterations : null,
  }
}

function normalizeResolution(value: Record<string, unknown>): ToolApprovalResolution | null {
  const id = typeof value.id === 'string' ? value.id : null
  if (!id) return null
  const approved = value.approved === true
  const source =
    value.source === 'user' ||
    value.source === 'session-cache' ||
    value.source === 'timeout' ||
    value.source === 'abort' ||
    value.source === 'api-auto'
      ? (value.source as ApprovalSource)
      : 'user'
  return { id, approved, source, resolvedAtUnix: Date.now() / 1000 }
}

export function useToolApproval(): ToolApprovalApi {
  const [pending, setPending] = useState<ToolApprovalRequest[]>([])
  const [resolutions, setResolutions] = useState<Map<string, ToolApprovalResolution>>(new Map())

  const ingestCustomEvent = useCallback((name: string | undefined, value: Record<string, unknown> | undefined) => {
    if (!name || !value) return
    if (name === 'tool_approval_request') {
      const req = normalizeRequest(value)
      if (!req) return
      setPending((prev) => (prev.some((p) => p.id === req.id) ? prev : [...prev, req]))
      return
    }
    if (name === 'tool_approval_response') {
      const res = normalizeResolution(value)
      if (!res) return
      setPending((prev) => prev.filter((p) => p.id !== res.id))
      setResolutions((prev) => {
        const next = new Map(prev)
        next.set(res.id, res)
        return next
      })
      return
    }
  }, [])

  const approve = useCallback(async (id: string, opts?: { rememberForSession?: boolean }) => {
    try {
      const body = JSON.stringify({ id, approved: true, remember_for_session: !!opts?.rememberForSession })
      const res = await fetch('/api/tool-approval', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        credentials: 'same-origin',
      })
      if (res.status === 410) {
        const data = (await res.json().catch(() => null)) as { detail?: { resolved_by?: string } } | null
        const source = (data?.detail?.resolved_by ?? 'user') as ApprovalSource
        setPending((prev) => prev.filter((p) => p.id !== id))
        setResolutions((prev) => {
          const next = new Map(prev)
          next.set(id, { id, approved: false, source, resolvedAtUnix: Date.now() / 1000 })
          return next
        })
      }
    } catch {
      // network error; leave the card in the pending list so the user can retry
    }
  }, [])

  const reject = useCallback(async (id: string) => {
    try {
      await fetch('/api/tool-approval', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, approved: false }),
        credentials: 'same-origin',
      })
    } catch {
      // network error; SPA stays in pending state for retry
    }
  }, [])

  const reset = useCallback(() => {
    setPending([])
    setResolutions(new Map())
  }, [])

  return { pending, resolutions, ingestCustomEvent, approve, reject, reset }
}
