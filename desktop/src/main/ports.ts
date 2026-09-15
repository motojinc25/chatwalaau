/**
 * Loopback port helpers. The MAIN port is bound by the launcher itself (no probe-then-bind
 * race, CTR-0208); these helpers are only used for the MCP Apps sandbox port, which the
 * existing backend binds on its own (gap G2: a residual race is documented).
 */

import { createServer } from 'node:net'
import { LOOPBACK_HOST } from './constants'

export function isPortFree(port: number, host = LOOPBACK_HOST): Promise<boolean> {
  return new Promise((resolve) => {
    const srv = createServer()
    srv.once('error', () => resolve(false))
    srv.listen({ port, host, exclusive: true }, () => srv.close(() => resolve(true)))
  })
}

export function ephemeralPort(host = LOOPBACK_HOST): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer()
    srv.once('error', reject)
    srv.listen({ port: 0, host, exclusive: true }, () => {
      const addr = srv.address()
      const port = typeof addr === 'object' && addr ? addr.port : 0
      srv.close(() => (port ? resolve(port) : reject(new Error('no port assigned'))))
    })
  })
}

export async function pickPort(preferred: number | undefined, avoid: readonly number[] = []): Promise<number> {
  if (preferred && !avoid.includes(preferred) && (await isPortFree(preferred))) return preferred
  for (let i = 0; i < 5; i++) {
    const p = await ephemeralPort()
    if (!avoid.includes(p)) return p
  }
  return ephemeralPort()
}
