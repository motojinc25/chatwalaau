/**
 * Computer Use in the SPA (CTR-0235, PRP-0189, UDR-0171).
 *
 * The SPA never drives the desktop: the computer_* tools run in the backend. This
 * module only recognises their tool calls, reads the compact JSON they return
 * (CTR-0230 ActionResult / Observation -- never an image, screenshots are not
 * persisted), and sends the one command the SPA owns: the abort (CTR-0233).
 */

export const COMPUTER_USE_TOOLS = new Set([
  'computer_list_windows',
  'computer_focus_window',
  'computer_capture_screen',
  'computer_perform_actions',
  'computer_get_active_window',
  'computer_wait_for_change',
  'computer_abort',
])

/** The fixed kill-switch hotkey (UDR-0171 D11, Q6). */
export const COMPUTER_USE_HOTKEY = 'Ctrl+Alt+End'

export function isComputerUseTool(name: string | undefined): boolean {
  return !!name && COMPUTER_USE_TOOLS.has(name)
}

export interface ComputerUseResult {
  status: string
  executed?: number
  reason?: string
  by?: string
  expect?: boolean[]
  screen?: string
  typed?: string[]
  timings?: Record<string, number>
  window?: string
  observationId?: string
  changed?: boolean
}

/** Parse a computer_* tool result; null when it is not the expected JSON. */
export function parseComputerUseResult(raw: string | undefined): ComputerUseResult | null {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || typeof parsed.status !== 'string') return null
    const obs = parsed.observation && typeof parsed.observation === 'object' ? parsed.observation : null
    return {
      status: parsed.status,
      executed: typeof parsed.executed === 'number' ? parsed.executed : undefined,
      reason: typeof parsed.reason === 'string' ? parsed.reason : undefined,
      by: typeof parsed.by === 'string' ? parsed.by : undefined,
      expect: Array.isArray(parsed.expect) ? parsed.expect.map(Boolean) : undefined,
      screen: typeof parsed.screen === 'string' ? parsed.screen : undefined,
      typed: Array.isArray(parsed.typed) ? parsed.typed.map(String) : undefined,
      timings: parsed.timings && typeof parsed.timings === 'object' ? parsed.timings : undefined,
      window: obs && typeof obs.window === 'string' ? obs.window : undefined,
      observationId: obs && typeof obs.id === 'string' ? obs.id : undefined,
      changed: obs && typeof obs.changed === 'boolean' ? obs.changed : undefined,
    }
  } catch {
    return null
  }
}

/** "act 0.6 s | settle 0.8 s | capture 0.1 s" from the result timings. */
export function formatComputerUseTimings(timings: Record<string, number> | undefined): string | null {
  if (!timings) return null
  const order: Array<[string, string]> = [
    ['act_ms', 'act'],
    ['settle_ms', 'settle'],
    ['capture_ms', 'capture'],
    ['encode_ms', 'encode'],
  ]
  const parts = order
    .filter(([key]) => typeof timings[key] === 'number')
    .map(([key, label]) => `${label} ${(timings[key] / 1000).toFixed(1)} s`)
  return parts.length > 0 ? parts.join(' | ') : null
}

/** POST /api/computer-use/abort -- the SPA's kill switch (CTR-0233). */
export async function abortComputerUse(): Promise<boolean> {
  try {
    const res = await fetch('/api/computer-use/abort', { method: 'POST' })
    return res.ok
  } catch {
    return false
  }
}

/** The newest capture of a chat (CTR-0233 captures/{thread_id}/latest). */
export interface ComputerUseCapture {
  file: string
  obs: string
  run: string
  window: string
  ts: string
  url: string
}

export async function fetchLatestCapture(threadId: string): Promise<ComputerUseCapture | null> {
  try {
    const res = await fetch(`/api/computer-use/captures/${encodeURIComponent(threadId)}/latest`, {
      cache: 'no-store',
    })
    if (res.status !== 200) return null
    const body = await res.json()
    return body && typeof body.file === 'string' && typeof body.url === 'string' ? (body as ComputerUseCapture) : null
  } catch {
    return null
  }
}
