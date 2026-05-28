/**
 * ToolApprovalCard (CTR-0100, PRP-0067, UDR-0043).
 *
 * Inline approval card rendered above ChatInput while one or more
 * tool calls are parked waiting for the operator's decision.
 * Approve / Reject / Approve-for-session map to the API actions
 * exposed by `useToolApproval`.
 */

import { CheckCircle2, ShieldAlert, Timer, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import type { ToolApprovalApi, ToolApprovalRequest } from '@/hooks/useToolApproval'

function formatArgValue(value: unknown): string {
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function useCountdown(expiresAtUnix: number): string {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!expiresAtUnix) return undefined
    const handle = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => window.clearInterval(handle)
  }, [expiresAtUnix])
  if (!expiresAtUnix) return ''
  const seconds = Math.max(0, Math.floor(expiresAtUnix - now))
  if (seconds <= 0) return 'Expiring...'
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return m > 0 ? `${m}m ${s.toString().padStart(2, '0')}s` : `${s}s`
}

interface ToolApprovalCardProps {
  request: ToolApprovalRequest
  approve: ToolApprovalApi['approve']
  reject: ToolApprovalApi['reject']
}

export function ToolApprovalCard({ request, approve, reject }: ToolApprovalCardProps) {
  const countdown = useCountdown(request.expiresAtUnix)
  const [rememberForSession, setRememberForSession] = useState(false)
  const [busy, setBusy] = useState(false)

  const handleApprove = async () => {
    setBusy(true)
    try {
      await approve(request.id, { rememberForSession })
    } finally {
      setBusy(false)
    }
  }

  const handleReject = async () => {
    setBusy(true)
    try {
      await reject(request.id)
    } finally {
      setBusy(false)
    }
  }

  const argEntries = Object.entries(request.arguments)

  return (
    <div className="rounded-lg border border-amber-500/50 bg-amber-50/40 p-3 text-sm dark:bg-amber-950/20">
      <div className="mb-2 flex items-center gap-2 font-medium text-amber-900 dark:text-amber-200">
        <ShieldAlert className="size-4" />
        Tool approval required
        <span className="ml-auto inline-flex items-center gap-1 text-xs font-normal text-muted-foreground">
          <Timer className="size-3.5" />
          {countdown && <>Times out in {countdown}</>}
        </span>
      </div>
      <div className="mb-2 font-mono text-xs">
        <span className="font-semibold">{request.toolName}</span>
      </div>
      {argEntries.length > 0 && (
        <dl className="mb-3 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-xs">
          {argEntries.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="text-muted-foreground">{key}:</dt>
              <dd className="break-all">{formatArgValue(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={handleApprove} disabled={busy} className="gap-1.5">
          <CheckCircle2 className="size-3.5" />
          Approve
        </Button>
        <Button size="sm" variant="outline" onClick={handleReject} disabled={busy} className="gap-1.5">
          <XCircle className="size-3.5" />
          Reject
        </Button>
        <label className="ml-2 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={rememberForSession}
            onChange={(e) => setRememberForSession(e.target.checked)}
            disabled={busy}
          />
          Approve all <span className="font-mono">{request.toolName}</span> calls in this session
        </label>
      </div>
    </div>
  )
}

interface ToolApprovalListProps {
  api: ToolApprovalApi
}

export function ToolApprovalList({ api }: ToolApprovalListProps) {
  if (api.pending.length === 0) return null
  return (
    <div className="mb-2 flex flex-col gap-2">
      {api.pending.map((req) => (
        <ToolApprovalCard key={req.id} request={req} approve={api.approve} reject={api.reject} />
      ))}
    </div>
  )
}
