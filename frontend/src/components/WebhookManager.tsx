import { ExternalLink, KeyRound, Loader2, Play, Plus, RefreshCw, Trash2, Webhook } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * Webhook Gateway portal (CTR-0157, FEAT-0052, PRP-0097, UDR-0075 D12).
 *
 * Opened from a launcher icon in the SessionSidebar footer NEXT TO the Declarative
 * Agents launcher. A ~90% modal styled like PipelineManager / CronManager: left = the
 * webhook source list (enabled flag, receipt count); right = the selected source's
 * detail -- a receipt-record timeline, enable/disable, and (for Microsoft Graph) a
 * subscriptions panel + token-health / validate / manual Fetch. Mutations reflect to the
 * backend (CTR-0154) with a blocking indicator. The server store (CTR-0151) is the SSOT;
 * no client-side persistence.
 */

interface WebhookSource {
  name: string
  label: string
  description?: string
  enabled: boolean
  receipt_count: number
}

interface Receipt {
  id: string
  source: string
  outcome: string
  summary: string
  job_id: string | null
  received_at: string
  detail?: string
}

interface Subscription {
  id: string
  resource?: string
  change_type?: string
  expiration?: string
  notification_url?: string
}

interface WebhookManagerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

function fmt(ts: string | null | undefined): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

function outcomeColor(outcome: string): string {
  switch (outcome) {
    case 'accepted':
      return 'text-emerald-600 dark:text-emerald-500'
    case 'rejected':
      return 'text-destructive'
    case 'duplicate':
      return 'text-amber-600 dark:text-amber-500'
    default:
      return 'text-muted-foreground'
  }
}

export function WebhookManager({ open, onOpenChange }: WebhookManagerProps) {
  const [sources, setSources] = useState<WebhookSource[]>([])
  const [selectedName, setSelectedName] = useState<string | null>(null)
  const [receipts, setReceipts] = useState<Receipt[]>([])
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [health, setHealth] = useState<Record<string, unknown> | null>(null)
  const [fetchOrganizer, setFetchOrganizer] = useState('')
  const [fetchMeeting, setFetchMeeting] = useState('')
  // Dedicated (user-delegated) device-code flow state (CTR-0159, FEAT-0054).
  const [dedicatedMeeting, setDedicatedMeeting] = useState('')
  const [dedicatedFlow, setDedicatedFlow] = useState<{
    flow_id: string
    user_code: string
    verification_uri: string
  } | null>(null)
  const [dedicatedState, setDedicatedState] = useState<string | null>(null)
  const [dedicatedJobId, setDedicatedJobId] = useState<string | null>(null)
  const [dedicatedBusy, setDedicatedBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // Subscription id pending a delete confirmation (null = no confirm open).
  const [confirmDeleteSub, setConfirmDeleteSub] = useState<string | null>(null)

  const selected = sources.find((s) => s.name === selectedName) ?? null
  const isMsGraph = selected?.name === 'msgraph'

  const fetchSources = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/webhooks/sources')
      if (!res.ok) throw new Error('Failed to load webhook sources')
      const data = await res.json()
      const list = (data.sources ?? []) as WebhookSource[]
      setSources(list)
      setSelectedName((prev) => prev ?? list[0]?.name ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load webhook sources')
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchReceipts = useCallback(async (name: string) => {
    try {
      const res = await fetch(`/api/webhooks/sources/${name}/receipts`)
      if (!res.ok) return
      const data = await res.json()
      setReceipts((data.receipts ?? []) as Receipt[])
    } catch {
      setReceipts([])
    }
  }, [])

  const fetchSubscriptions = useCallback(async (name: string) => {
    if (name !== 'msgraph') {
      setSubscriptions([])
      return
    }
    try {
      const res = await fetch('/api/webhooks/msgraph/subscriptions')
      if (!res.ok) return
      const data = await res.json()
      setSubscriptions((data.subscriptions ?? []) as Subscription[])
    } catch {
      setSubscriptions([])
    }
  }, [])

  const selectSource = useCallback(
    (name: string) => {
      setSelectedName(name)
      setHealth(null)
      setNotice(null)
      setError(null)
      void fetchReceipts(name)
      void fetchSubscriptions(name)
    },
    [fetchReceipts, fetchSubscriptions],
  )

  // Manual Refresh. The data loads almost instantly, so enforce a minimum spin so the
  // indicator is always perceptible (at least one animation cycle).
  const manualRefresh = useCallback(async () => {
    setRefreshing(true)
    const started = Date.now()
    await fetchSources()
    if (selectedName) {
      await fetchReceipts(selectedName)
      await fetchSubscriptions(selectedName)
    }
    const elapsed = Date.now() - started
    const MIN_SPIN_MS = 1000 // one full animate-spin cycle
    if (elapsed < MIN_SPIN_MS) {
      await new Promise((resolve) => setTimeout(resolve, MIN_SPIN_MS - elapsed))
    }
    setRefreshing(false)
  }, [fetchSources, fetchReceipts, fetchSubscriptions, selectedName])

  useEffect(() => {
    if (open) {
      void fetchSources()
      setHealth(null)
      setNotice(null)
    }
  }, [open, fetchSources])

  useEffect(() => {
    if (open && selectedName) {
      void fetchReceipts(selectedName)
      void fetchSubscriptions(selectedName)
    }
  }, [open, selectedName, fetchReceipts, fetchSubscriptions])

  const toggleEnabled = useCallback(
    async (name: string, enabled: boolean) => {
      setBusy(true)
      setError(null)
      try {
        const res = await fetch(`/api/webhooks/sources/${name}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
        })
        if (!res.ok) throw new Error('Failed to update source')
        await fetchSources()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to update source')
      } finally {
        setBusy(false)
      }
    },
    [fetchSources],
  )

  const callGraph = useCallback(
    async (path: string, method: 'GET' | 'POST', body?: unknown): Promise<unknown | null> => {
      setBusy(true)
      setError(null)
      setNotice(null)
      try {
        const res = await fetch(path, {
          method,
          headers: body ? { 'Content-Type': 'application/json' } : undefined,
          body: body ? JSON.stringify(body) : undefined,
        })
        const data = await res.json().catch(() => null)
        if (!res.ok) {
          throw new Error((data?.detail?.error as string) ?? 'Request failed')
        }
        return data
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Request failed')
        return null
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  const subscribe = useCallback(async () => {
    const r = await callGraph('/api/webhooks/msgraph/subscriptions', 'POST', {})
    if (r) {
      setNotice('Subscription created.')
      void fetchSubscriptions('msgraph')
    }
  }, [callGraph, fetchSubscriptions])

  const maintain = useCallback(async () => {
    const r = (await callGraph('/api/webhooks/msgraph/subscriptions/maintain', 'POST')) as {
      renewed?: string[]
    } | null
    if (r) {
      setNotice(`Maintenance done: renewed ${r.renewed?.length ?? 0}.`)
      void fetchSubscriptions('msgraph')
    }
  }, [callGraph, fetchSubscriptions])

  const renewSub = useCallback(
    async (id: string) => {
      const r = await callGraph(`/api/webhooks/msgraph/subscriptions/${id}/renew`, 'POST')
      if (r) {
        setNotice('Subscription renewed.')
        void fetchSubscriptions('msgraph')
      }
    },
    [callGraph, fetchSubscriptions],
  )

  const confirmDeleteSubscription = useCallback(async () => {
    const id = confirmDeleteSub
    if (!id) return
    setConfirmDeleteSub(null)
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const res = await fetch(`/api/webhooks/msgraph/subscriptions/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete subscription')
      setNotice('Subscription deleted.')
      await fetchSubscriptions('msgraph')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete subscription')
    } finally {
      setBusy(false)
    }
  }, [confirmDeleteSub, fetchSubscriptions])

  const tokenHealth = useCallback(async () => {
    const r = (await callGraph('/api/webhooks/msgraph/token-health', 'GET')) as Record<string, unknown> | null
    if (r) setHealth({ kind: 'token-health', ...r })
  }, [callGraph])

  const validate = useCallback(async () => {
    const r = (await callGraph('/api/webhooks/msgraph/validate', 'POST')) as Record<string, unknown> | null
    if (r) setHealth({ kind: 'validate', ...r })
  }, [callGraph])

  const runFetch = useCallback(async () => {
    const organizer = fetchOrganizer.trim()
    const meeting = fetchMeeting.trim()
    if (!organizer) {
      setError('Enter the meeting organizer (user id or UPN).')
      return
    }
    if (!meeting) {
      setError('Enter a meeting id or join URL.')
      return
    }
    const isUrl = meeting.startsWith('http')
    const r = await callGraph('/api/webhooks/msgraph/fetch', 'POST', {
      organizer_id: organizer,
      meeting_id: isUrl ? '' : meeting,
      join_web_url: isUrl ? meeting : '',
    })
    if (r) {
      setNotice('Meeting pipeline job submitted. See the Pipeline portal for progress.')
      setFetchOrganizer('')
      setFetchMeeting('')
    }
  }, [fetchOrganizer, fetchMeeting, callGraph])

  // Dedicated (user-delegated) device-code login + fetch (CTR-0159, FEAT-0054). The user
  // signs in AS THEMSELVES; access is user-scoped and limited to meetings they organized.
  const startDedicated = useCallback(async () => {
    const meeting = dedicatedMeeting.trim()
    if (!meeting) {
      setError('Enter a meeting id or join URL.')
      return
    }
    setDedicatedBusy(true)
    setError(null)
    setNotice(null)
    setDedicatedFlow(null)
    setDedicatedState(null)
    setDedicatedJobId(null)
    const isUrl = meeting.startsWith('http')
    try {
      const res = await fetch('/api/webhooks/msgraph/dedicated/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ meeting_id: isUrl ? '' : meeting, join_web_url: isUrl ? meeting : '' }),
      })
      const data = await res.json().catch(() => null)
      if (!res.ok) {
        throw new Error(
          (data?.detail?.message as string) ?? (data?.detail?.error as string) ?? 'Failed to start sign-in',
        )
      }
      setDedicatedFlow({ flow_id: data.flow_id, user_code: data.user_code, verification_uri: data.verification_uri })
      setDedicatedState('pending_auth')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start sign-in')
    } finally {
      setDedicatedBusy(false)
    }
  }, [dedicatedMeeting])

  const cancelDedicated = useCallback(async () => {
    if (!dedicatedFlow) return
    try {
      await fetch(`/api/webhooks/msgraph/dedicated/cancel/${dedicatedFlow.flow_id}`, { method: 'POST' })
    } catch {
      // best-effort
    }
    setDedicatedFlow(null)
    setDedicatedState(null)
  }, [dedicatedFlow])

  // Poll the device-code flow status until it reaches a terminal state.
  useEffect(() => {
    if (!dedicatedFlow) return
    if (dedicatedState && ['job_enqueued', 'failed', 'cancelled'].includes(dedicatedState)) return
    let cancelled = false
    const timer = setInterval(async () => {
      try {
        const res = await fetch(`/api/webhooks/msgraph/dedicated/status/${dedicatedFlow.flow_id}`)
        if (!res.ok || cancelled) return
        const data = await res.json()
        setDedicatedState(data.state)
        if (data.job_id) setDedicatedJobId(data.job_id)
        if (data.state === 'job_enqueued') {
          setNotice('Dedicated job submitted. See the Pipeline portal for progress.')
          setDedicatedMeeting('')
        } else if (data.state === 'failed') {
          setError(data.error ?? 'Dedicated fetch failed.')
        }
      } catch {
        // transient; keep polling
      }
    }, 3000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [dedicatedFlow, dedicatedState])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle className="flex items-center gap-2">
            <Webhook className="h-4 w-4" /> Webhooks
          </DialogTitle>
          <DialogDescription>
            Manage inbound webhook sources and their received notifications. The first source is Microsoft Graph (Teams
            meeting transcripts). Enable/disable a source, review its receipts, and manage Graph subscriptions.
          </DialogDescription>
        </DialogHeader>

        <div className="relative flex min-h-0 flex-1">
          {/* Left: source list */}
          <div className="flex w-72 shrink-0 flex-col border-r">
            <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
              <span className="text-xs font-medium text-muted-foreground">Sources ({sources.length})</span>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                disabled={refreshing}
                onClick={() => void manualRefresh()}
                title="Refresh">
                <RefreshCw className={cn('h-3.5 w-3.5', (refreshing || loading) && 'animate-spin')} />
              </Button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {sources.length === 0 ? (
                <p className="px-3 py-3 text-xs text-muted-foreground">{loading ? 'Loading...' : 'No sources.'}</p>
              ) : (
                sources.map((src) => (
                  <button
                    type="button"
                    key={src.name}
                    onClick={() => selectSource(src.name)}
                    className={cn(
                      'flex w-full flex-col items-start gap-0.5 border-b px-3 py-2 text-left',
                      selectedName === src.name && 'bg-accent',
                    )}>
                    <span className="flex w-full items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{src.label}</span>
                      <span
                        className={cn(
                          'shrink-0 text-[10px] uppercase',
                          src.enabled ? 'text-emerald-600 dark:text-emerald-500' : 'text-muted-foreground',
                        )}>
                        {src.enabled ? 'on' : 'off'}
                      </span>
                    </span>
                    <span className="truncate text-[11px] text-muted-foreground">{src.receipt_count} receipts</span>
                  </button>
                ))
              )}
            </div>
          </div>

          {/* Right: source detail */}
          <div className="min-w-0 flex-1 overflow-y-auto p-4">
            {!selected ? (
              <p className="text-xs text-muted-foreground">Select a source.</p>
            ) : (
              <div className="grid grid-cols-1 gap-4">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <div className="text-sm font-semibold">{selected.label}</div>
                    {selected.description && (
                      <div className="text-[11px] text-muted-foreground">{selected.description}</div>
                    )}
                  </div>
                  <Button
                    variant={selected.enabled ? 'outline' : 'default'}
                    size="sm"
                    disabled={busy}
                    onClick={() => void toggleEnabled(selected.name, !selected.enabled)}>
                    {selected.enabled ? 'Disable' : 'Enable'}
                  </Button>
                </div>

                {/* Microsoft Graph subscriptions panel */}
                {isMsGraph && (
                  <div className="rounded-md border p-3">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <span className="text-xs font-semibold text-muted-foreground">Subscriptions</span>
                      <div className="ml-auto flex flex-wrap items-center gap-1">
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => void subscribe()}>
                          <Plus className="mr-1 h-3.5 w-3.5" /> Subscribe
                        </Button>
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => void maintain()}>
                          Maintain
                        </Button>
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => void tokenHealth()}>
                          Token health
                        </Button>
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => void validate()}>
                          Validate
                        </Button>
                      </div>
                    </div>
                    {subscriptions.length === 0 ? (
                      <p className="text-[11px] text-muted-foreground">No subscriptions.</p>
                    ) : (
                      <ul className="space-y-1">
                        {subscriptions.map((sub) => (
                          <li
                            key={sub.id}
                            className="flex items-center justify-between gap-2 rounded border px-2 py-1 text-[11px]">
                            <span className="min-w-0 flex-1 truncate" title={sub.resource}>
                              {sub.resource || sub.id}
                            </span>
                            <span className="shrink-0 text-muted-foreground">exp {fmt(sub.expiration)}</span>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-5 w-5"
                              disabled={busy}
                              onClick={() => void renewSub(sub.id)}
                              title="Renew">
                              <RefreshCw className="h-3 w-3" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-5 w-5 text-destructive"
                              disabled={busy}
                              onClick={() => setConfirmDeleteSub(sub.id)}
                              title="Delete">
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          </li>
                        ))}
                      </ul>
                    )}

                    {/* Manual fetch (run the meeting pipeline on demand). App-only access is
                        organizer-scoped, so the meeting organizer is required. */}
                    <div className="mt-3 flex flex-col gap-2 border-t pt-3">
                      <input
                        className="min-w-0 rounded-md border bg-background px-2 py-1 font-mono text-xs"
                        placeholder="organizer user id or UPN (required)"
                        value={fetchOrganizer}
                        onChange={(e) => setFetchOrganizer(e.target.value)}
                      />
                      <div className="flex items-center gap-2">
                        <input
                          className="min-w-0 flex-1 rounded-md border bg-background px-2 py-1 font-mono text-xs"
                          placeholder="Join URL (recommended) or Graph onlineMeeting id"
                          value={fetchMeeting}
                          onChange={(e) => setFetchMeeting(e.target.value)}
                        />
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => void runFetch()}>
                          <Play className="mr-1 h-3.5 w-3.5" /> Fetch
                        </Button>
                      </div>
                    </div>

                    {health && (
                      <pre className="mt-2 max-h-32 overflow-auto rounded bg-muted p-2 text-[11px]">
                        {JSON.stringify(health, null, 2)}
                      </pre>
                    )}
                  </div>
                )}

                {/* Dedicated fetch (user-delegated, device-code sign-in). No app-only
                    service principal or Application Access Policy required; you summarize a
                    meeting YOU organized (CTR-0159, FEAT-0054, UDR-0077). */}
                {isMsGraph && (
                  <div className="rounded-md border p-3">
                    <div className="mb-1 flex items-center gap-2">
                      <KeyRound className="h-3.5 w-3.5 text-muted-foreground" />
                      <span className="text-xs font-semibold text-muted-foreground">
                        Dedicated fetch (sign in as yourself)
                      </span>
                    </div>
                    <p className="mb-2 text-[11px] text-muted-foreground">
                      Summarize a meeting <strong>you organized</strong> by signing in with your own account via a
                      device code. No app-only service principal or Application Access Policy is required. The sign-in
                      token is used once and never stored.
                    </p>
                    <div className="flex items-center gap-2">
                      <input
                        className="min-w-0 flex-1 rounded-md border bg-background px-2 py-1 font-mono text-xs"
                        placeholder="Join URL (recommended) or Graph onlineMeeting id"
                        value={dedicatedMeeting}
                        disabled={dedicatedBusy || (!!dedicatedFlow && dedicatedState === 'pending_auth')}
                        onChange={(e) => setDedicatedMeeting(e.target.value)}
                      />
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={dedicatedBusy || (!!dedicatedFlow && dedicatedState === 'pending_auth')}
                        onClick={() => void startDedicated()}>
                        {dedicatedBusy ? (
                          <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <KeyRound className="mr-1 h-3.5 w-3.5" />
                        )}
                        Sign in &amp; fetch
                      </Button>
                    </div>

                    {dedicatedFlow && (
                      <div className="mt-2 rounded border bg-muted/50 p-2 text-[11px]">
                        {dedicatedState === 'pending_auth' && (
                          <div className="flex flex-col gap-1">
                            <span>
                              Open{' '}
                              <a
                                href={dedicatedFlow.verification_uri}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex items-center gap-0.5 text-primary underline">
                                {dedicatedFlow.verification_uri}
                                <ExternalLink className="h-3 w-3" />
                              </a>{' '}
                              and enter this code:
                            </span>
                            <div className="flex items-center gap-2">
                              <code className="select-all rounded bg-background px-2 py-1 font-mono text-sm font-semibold tracking-widest">
                                {dedicatedFlow.user_code}
                              </code>
                              <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                              <span className="text-muted-foreground">Waiting for sign-in...</span>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="ml-auto h-6"
                                onClick={() => void cancelDedicated()}>
                                Cancel
                              </Button>
                            </div>
                          </div>
                        )}
                        {dedicatedState === 'authorized' && (
                          <span className="flex items-center gap-1 text-muted-foreground">
                            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Signed in. Submitting the job...
                          </span>
                        )}
                        {dedicatedState === 'job_enqueued' && (
                          <span className="text-emerald-600 dark:text-emerald-500">
                            Job submitted{dedicatedJobId ? ` (${dedicatedJobId})` : ''}. See the Pipeline portal for
                            progress.
                          </span>
                        )}
                        {dedicatedState === 'failed' && (
                          <span className="text-destructive">Sign-in or fetch failed.</span>
                        )}
                        {dedicatedState === 'cancelled' && <span className="text-muted-foreground">Cancelled.</span>}
                      </div>
                    )}
                  </div>
                )}

                {/* Receipt records timeline */}
                <div>
                  <h3 className="mb-2 text-xs font-semibold text-muted-foreground">Receipts ({receipts.length})</h3>
                  {receipts.length === 0 ? (
                    <p className="text-xs text-muted-foreground">No receipts yet.</p>
                  ) : (
                    <ul className="space-y-1">
                      {receipts.map((r) => (
                        <li
                          key={r.id}
                          className="flex items-center justify-between gap-2 rounded-md border px-2 py-1 text-xs">
                          <span className="min-w-0 flex-1 truncate" title={r.summary}>
                            {r.summary || r.id}
                          </span>
                          {r.job_id && <span className="shrink-0 text-[10px] text-muted-foreground">{r.job_id}</span>}
                          <span className={cn('shrink-0 text-[10px] uppercase', outcomeColor(r.outcome))}>
                            {r.outcome}
                          </span>
                          <span className="shrink-0 text-[10px] text-muted-foreground">{fmt(r.received_at)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            )}
          </div>

          {(busy || confirmDeleteSub) && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
              {busy ? (
                <div className="flex items-center gap-2 text-sm">
                  <Loader2 className="h-5 w-5 animate-spin" /> Applying...
                </div>
              ) : (
                <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
                  <p className="text-sm font-medium">Delete this subscription?</p>
                  <p className="mt-1 break-all text-xs text-muted-foreground">
                    Graph will stop sending notifications for "{confirmDeleteSub}". This cannot be undone (you can
                    re-subscribe).
                  </p>
                  <div className="mt-3 flex justify-end gap-2">
                    <Button variant="outline" size="sm" onClick={() => setConfirmDeleteSub(null)}>
                      Cancel
                    </Button>
                    <Button variant="destructive" size="sm" onClick={() => void confirmDeleteSubscription()}>
                      <Trash2 className="mr-1 h-3.5 w-3.5" /> Delete
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t px-6 py-3">
          <span className={cn('text-xs', error ? 'text-destructive' : 'text-muted-foreground')}>
            {error || notice || ''}
          </span>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
