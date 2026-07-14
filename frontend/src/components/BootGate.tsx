/**
 * Backend readiness gate (CTR-0096 v3, PRP-0112 Part 1 / UDR-0092).
 *
 * Holds the application behind an animated indicator until the backend answers
 * ONCE, then gets out of the way permanently.
 *
 * Why this exists: in dev (`pnpm dev:full`) Vite serves the SPA instantly on
 * :5173 while uvicorn is still importing -- backend boot is heavy (agent registry,
 * MCP servers, cron/pipeline/teams/webhook lifespans). Before PRP-0112 the SPA
 * rendered the full chat UI anyway, because useAuth swallowed the network error
 * and synthesized an authenticated open-mode session. The user got a normal-looking
 * page where nothing worked and nothing said why (UDR-0092 D1).
 *
 * ---------------------------------------------------------------------------
 * On UDR-0088 D5 ("no proactive liveness monitoring"), which this seems to break:
 *
 * It does not. D5 forbids polling a backend that is HEALTHY AND ANSWERING, to
 * guess whether it has since died -- because that is an inference, and sleep/wake,
 * Wi-Fi roams and proxy idle timeouts make it a false-positive machine, which
 * trains users to ignore the banner.
 *
 * This gate probes a backend that has NEVER answered in this page lifetime. That
 * is an observed fact, not an inference: it cannot produce a false positive. It
 * terminates permanently on the first success -- after which NO polling of any kind
 * occurs -- and it gates rendering rather than accusing a running server of being
 * down. UDR-0088 D7 explicitly sanctioned this shape ("a bounded backoff probe of
 * GET /api/auth/status" that "runs only while an error state is displayed").
 *
 * So: probing a backend that HAS answered stays forbidden. Probing one that has
 * NEVER answered is required. Do not collapse the two.
 * ---------------------------------------------------------------------------
 */

import { Loader2, ServerCrash } from 'lucide-react'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'

/**
 * Probe schedule (UDR-0092 D3). Bounded backoff, NOT a fixed interval.
 *
 * A fixed 10s grid -- the shape originally requested -- makes a backend that is
 * ready at t=1s still show a spinner until t=10s, and "1-3s behind Vite" is the
 * common dev case. Backing off from an immediate first probe detects that in ~1s
 * while still granting a genuinely slow boot the same ~20-attempt budget.
 *
 * These are constants on purpose: no operator decision depends on them, so they
 * are deliberately NOT environment variables.
 */
const MAX_ATTEMPTS = 20
const BACKOFF_MS = [1000, 2000, 4000, 8000] as const
const BACKOFF_CAP_MS = 10_000

/** The probe target: unauthenticated by design, and the SPA's bootstrap call anyway (D6). */
const STATUS_URL = '/api/auth/status'

function delayForAttempt(attempt: number): number {
  return BACKOFF_MS[attempt - 1] ?? BACKOFF_CAP_MS
}

async function probe(): Promise<boolean> {
  try {
    const res = await fetch(STATUS_URL, { credentials: 'same-origin', cache: 'no-store' })
    return res.ok
  } catch {
    return false
  }
}

type GateState = 'probing' | 'ready' | 'unreachable'

export function BootGate({ children }: { children: ReactNode }) {
  // Retry restarts the whole budget by REMOUNTING the prober (React's canonical
  // "reset all state" pattern -- a changing `key`). Cleaner than threading a reset
  // token through the probe effect's dependencies, where it would be an unused dep.
  const [runId, setRunId] = useState(0)
  return (
    <BootProbe key={runId} onRetry={() => setRunId((n) => n + 1)}>
      {children}
    </BootProbe>
  )
}

function BootProbe({ children, onRetry }: { children: ReactNode; onRetry: () => void }) {
  const [status, setStatus] = useState<GateState>('probing')
  const [attempt, setAttempt] = useState(0)
  const [elapsedMs, setElapsedMs] = useState(0)
  const startedAtRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    setStatus('probing')
    setAttempt(0)
    setElapsedMs(0)
    startedAtRef.current = Date.now()

    const tick = window.setInterval(() => {
      if (!cancelled) setElapsedMs(Date.now() - startedAtRef.current)
    }, 250)

    const run = async (n: number) => {
      if (cancelled) return
      setAttempt(n)
      if (await probe()) {
        if (!cancelled) setStatus('ready')
        return
      }
      if (cancelled) return
      if (n >= MAX_ATTEMPTS) {
        // Budget spent: stop probing and say so plainly (D4). An unbounded retry
        // loop would spin forever against a backend that is never coming.
        setStatus('unreachable')
        return
      }
      timer = window.setTimeout(() => void run(n + 1), delayForAttempt(n))
    }

    // First probe is immediate (t=0), so a healthy backend costs no visible wait.
    void run(1)

    return () => {
      cancelled = true
      window.clearInterval(tick)
      if (timer) window.clearTimeout(timer)
    }
  }, [])

  if (status === 'ready') {
    // D2: once the backend has answered we render children and never hold them
    // again for any reason. Everything after this first success is UDR-0088's
    // territory (ReauthDialog, commit boundary), not ours.
    return <>{children}</>
  }

  if (status === 'unreachable') {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 bg-background px-6 text-center">
        <ServerCrash className="h-10 w-10 text-destructive" />
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Cannot reach the ChatWalaʻau backend</h1>
          <p className="max-w-md text-sm text-muted-foreground">
            No response from <code className="rounded bg-muted px-1 py-0.5">{STATUS_URL}</code> after {MAX_ATTEMPTS}{' '}
            attempts over {Math.round(elapsedMs / 1000)}s. The API server is probably not running.
          </p>
          <p className="max-w-md text-xs text-muted-foreground">
            In development, start it with{' '}
            <code className="rounded bg-muted px-1 py-0.5">uv run uvicorn app.main:app --app-dir src</code> from{' '}
            <code className="rounded bg-muted px-1 py-0.5">backend/</code>.
          </p>
        </div>
        <Button onClick={onRetry}>Retry</Button>
      </div>
    )
  }

  return (
    <div className="flex h-screen flex-col items-center justify-center gap-4 bg-background px-6 text-center">
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      <div className="space-y-1">
        <p className="text-sm font-medium">Waiting for the ChatWalaʻau backend...</p>
        <p className="text-xs text-muted-foreground">
          Attempt {attempt} of {MAX_ATTEMPTS} - {Math.round(elapsedMs / 1000)}s elapsed
        </p>
      </div>
    </div>
  )
}
