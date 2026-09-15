/**
 * Launcher event lines (CTR-0208). Pure module.
 *
 * The launcher writes `@@CWDESKTOP@@ {json}` lines to stdout. Everything else on stdout is
 * backend log output. A line counts as an event only when it carries the prefix, parses,
 * stays under the length cap and names the exact launchId of this launch -- so no log
 * line (including one a user or model managed to print) can be mistaken for readiness.
 */

import { EVENT_PREFIX } from './constants'

export const MAX_EVENT_LINE = 16 * 1024

export type LauncherEvent =
  | { type: 'bound'; port: number }
  | { type: 'ready'; port: number; pid: number; backendVersion: string; envId: string | null }
  | { type: 'startup-error'; code: string; message: string }
  | { type: 'stopping' }
  | { type: 'stopped' }

function isPort(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v > 0 && v < 65536
}

export function parseEventLine(line: string, launchId: string): LauncherEvent | null {
  if (!line.startsWith(EVENT_PREFIX) || line.length > MAX_EVENT_LINE) return null
  let obj: unknown
  try {
    obj = JSON.parse(line.slice(EVENT_PREFIX.length))
  } catch {
    return null
  }
  if (typeof obj !== 'object' || obj === null) return null
  const e = obj as Record<string, unknown>
  if (e.launchId !== launchId) return null
  switch (e.type) {
    case 'bound':
      return isPort(e.port) ? { type: 'bound', port: e.port } : null
    case 'ready':
      if (!isPort(e.port) || typeof e.pid !== 'number' || typeof e.backendVersion !== 'string') return null
      return {
        type: 'ready',
        port: e.port,
        pid: e.pid,
        backendVersion: e.backendVersion,
        envId: typeof e.envId === 'string' ? e.envId : null,
      }
    case 'startup-error':
      return {
        type: 'startup-error',
        code: typeof e.code === 'string' ? e.code : 'BACKEND_START_FAILED',
        message: typeof e.message === 'string' ? e.message : '',
      }
    case 'stopping':
      return { type: 'stopping' }
    case 'stopped':
      return { type: 'stopped' }
    default:
      return null
  }
}

/** The one line Main writes to the launcher's stdin to start it. */
export interface StartCommand {
  type: 'start'
  launchId: string
  token: string
  port: number
  sandboxPort: number
  profileDir: string
  envId: string | null
}

export function encodeCommand(cmd: StartCommand | { type: 'shutdown' }): string {
  return `${JSON.stringify(cmd)}\n`
}
