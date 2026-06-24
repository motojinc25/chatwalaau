import { Clock, Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * Cron Scheduler portal (CTR-0135, FEAT-0048, PRP-0089, UDR-0067).
 *
 * Opened from a launcher icon in the SessionSidebar footer (next to App Info) or
 * via the /cron slash command. A ~90% modal styled like the Skills / MCP managers:
 * left = job list (grouped by category, enabled toggle, next/last run, state); right
 * = a job editor plus a chronological TIMELINE of recent runs (click a run -> detail
 * with stdout/stderr). Create / update / delete reflect to the backend immediately
 * (CTR-0133) with a blocking saving indicator.
 *
 * Controlled component: the parent (ChatPage) owns open state so both the footer
 * icon and the /cron command can drive it. The store is server-side (CTR-0131); no
 * client-side persistence.
 */

type ScheduleType = 'cron' | 'interval' | 'oneshot'

interface CronSchedule {
  type: ScheduleType
  expr?: string
  interval_seconds?: number
  run_at?: string
}

interface CronScript {
  path: string
  interpreter?: string
  args?: string[]
}

interface CronJob {
  id: string
  category: string
  description: string
  enabled: boolean
  schedule: CronSchedule
  script?: CronScript
  repeat: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  state: string
  created_by: string
}

interface CronRun {
  run_id: string
  status: string
  started_at: string
  finished_at: string | null
  exit_code: number | null
  duration_ms: number | null
  interpreter?: string
  script?: string
  stdout?: string
  stderr?: string
}

interface CronManagerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

interface DraftState {
  id: string | null
  category: string
  description: string
  enabled: boolean
  scheduleType: ScheduleType
  expr: string
  intervalSeconds: string
  runAt: string
  scriptPath: string
  interpreter: string
  args: string
}

const EMPTY_DRAFT: DraftState = {
  id: null,
  category: '',
  description: '',
  enabled: true,
  scheduleType: 'cron',
  expr: '0 9 * * *',
  intervalSeconds: '3600',
  runAt: '',
  scriptPath: '',
  interpreter: '',
  args: '',
}

function fmt(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

function stateColor(state: string): string {
  switch (state) {
    case 'success':
      return 'text-emerald-600 dark:text-emerald-500'
    case 'failed':
      return 'text-destructive'
    case 'running':
      return 'text-blue-600 dark:text-blue-400'
    case 'completed':
      return 'text-muted-foreground'
    case 'disabled':
      return 'text-muted-foreground'
    default:
      return 'text-foreground'
  }
}

function draftFromJob(job: CronJob): DraftState {
  return {
    id: job.id,
    category: job.category,
    description: job.description,
    enabled: job.enabled,
    scheduleType: job.schedule.type,
    expr: job.schedule.expr ?? '',
    intervalSeconds: String(job.schedule.interval_seconds ?? ''),
    runAt: job.schedule.run_at ?? '',
    scriptPath: job.script?.path ?? '',
    interpreter: job.script?.interpreter ?? '',
    args: (job.script?.args ?? []).join(' '),
  }
}

function buildBody(draft: DraftState): Record<string, unknown> {
  const schedule: CronSchedule = { type: draft.scheduleType }
  if (draft.scheduleType === 'cron') schedule.expr = draft.expr.trim()
  else if (draft.scheduleType === 'interval') schedule.interval_seconds = Number(draft.intervalSeconds) || 0
  else schedule.run_at = draft.runAt.trim()
  return {
    category: draft.category.trim(),
    description: draft.description.trim(),
    enabled: draft.enabled,
    schedule,
    script: {
      path: draft.scriptPath.trim(),
      interpreter: draft.interpreter.trim(),
      args: draft.args.trim() ? draft.args.trim().split(/\s+/) : [],
    },
  }
}

export function CronManager({ open, onOpenChange }: CronManagerProps) {
  const [jobs, setJobs] = useState<CronJob[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<DraftState>(EMPTY_DRAFT)
  const [runs, setRuns] = useState<CronRun[]>([])
  const [runDetail, setRunDetail] = useState<CronRun | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const fetchJobs = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/cron/jobs')
      if (!res.ok) throw new Error('Failed to load cron jobs')
      const data = await res.json()
      setJobs((data.jobs ?? []) as CronJob[])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load cron jobs')
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchRuns = useCallback(async (jobId: string) => {
    setRunDetail(null)
    try {
      const res = await fetch(`/api/cron/jobs/${jobId}/runs`)
      if (!res.ok) return
      const data = await res.json()
      setRuns((data.runs ?? []) as CronRun[])
    } catch {
      setRuns([])
    }
  }, [])

  const openRunDetail = useCallback(async (runId: string) => {
    try {
      const res = await fetch(`/api/cron/runs/${runId}`)
      if (!res.ok) return
      setRunDetail((await res.json()) as CronRun)
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => {
    if (open) {
      void fetchJobs()
      setDraft(EMPTY_DRAFT)
      setRuns([])
      setRunDetail(null)
      setConfirmDelete(false)
    }
  }, [open, fetchJobs])

  const selectJob = useCallback(
    (job: CronJob) => {
      setDraft(draftFromJob(job))
      void fetchRuns(job.id)
    },
    [fetchRuns],
  )

  const newJob = useCallback(() => {
    setDraft(EMPTY_DRAFT)
    setRuns([])
    setRunDetail(null)
  }, [])

  const save = useCallback(async () => {
    if (!draft.scriptPath.trim()) {
      setError('Script path is required.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const body = buildBody(draft)
      const url = draft.id ? `/api/cron/jobs/${draft.id}` : '/api/cron/jobs'
      const method = draft.id ? 'PUT' : 'POST'
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('Failed to save job')
      const saved = (await res.json()) as CronJob
      await fetchJobs()
      setDraft(draftFromJob(saved))
      void fetchRuns(saved.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save job')
    } finally {
      setSaving(false)
    }
  }, [draft, fetchJobs, fetchRuns])

  const remove = useCallback(async () => {
    if (!draft.id) return
    setConfirmDelete(false)
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`/api/cron/jobs/${draft.id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete job')
      await fetchJobs()
      newJob()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete job')
    } finally {
      setSaving(false)
    }
  }, [draft.id, fetchJobs, newJob])

  const set = useCallback((patch: Partial<DraftState>) => setDraft((d) => ({ ...d, ...patch })), [])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle className="flex items-center gap-2">
            <Clock className="h-4 w-4" /> Cron Scheduler
          </DialogTitle>
          <DialogDescription>
            Schedule scripts (in the coding workspace) to run on a cron expression, a fixed interval, or once after a
            delay. Changes take effect within one tick.
          </DialogDescription>
        </DialogHeader>

        <div className="relative flex min-h-0 flex-1">
          {/* Left: job list */}
          <div className="flex w-72 shrink-0 flex-col border-r">
            <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
              <span className="text-xs font-medium text-muted-foreground">Jobs ({jobs.length})</span>
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  onClick={() => void fetchJobs()}
                  title="Refresh">
                  <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
                </Button>
                <Button variant="ghost" size="icon" className="h-6 w-6" onClick={newJob} title="New job">
                  <Plus className="h-4 w-4" />
                </Button>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {jobs.length === 0 ? (
                <p className="px-3 py-3 text-xs text-muted-foreground">
                  {loading ? 'Loading...' : 'No jobs yet. Click + to create one.'}
                </p>
              ) : (
                jobs.map((job) => (
                  <button
                    type="button"
                    key={job.id}
                    onClick={() => selectJob(job)}
                    className={cn(
                      'flex w-full flex-col items-start gap-0.5 border-b px-3 py-2 text-left',
                      draft.id === job.id && 'bg-accent',
                    )}>
                    <span className="flex w-full items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{job.description || job.id}</span>
                      <span className={cn('shrink-0 text-[10px] uppercase', stateColor(job.state))}>{job.state}</span>
                    </span>
                    <span className="text-[11px] text-muted-foreground">
                      {job.category ? `${job.category} · ` : ''}
                      {job.schedule.type}
                      {!job.enabled && ' · off'}
                    </span>
                    <span className="text-[10px] text-muted-foreground">next: {fmt(job.next_run_at)}</span>
                  </button>
                ))
              )}
            </div>
          </div>

          {/* Right: editor + run timeline */}
          <div className="min-w-0 flex-1 overflow-y-auto p-4">
            <div className="grid grid-cols-1 gap-3">
              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1 text-xs">
                  Description
                  <input
                    className="rounded-md border bg-background px-2 py-1 text-sm"
                    value={draft.description}
                    onChange={(e) => set({ description: e.target.value })}
                    placeholder="Nightly backup"
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs">
                  Category
                  <input
                    className="rounded-md border bg-background px-2 py-1 text-sm"
                    value={draft.category}
                    onChange={(e) => set({ category: e.target.value })}
                    placeholder="maintenance"
                  />
                </label>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1 text-xs">
                  Schedule type
                  <select
                    className="rounded-md border bg-background px-2 py-1 text-sm"
                    value={draft.scheduleType}
                    onChange={(e) => set({ scheduleType: e.target.value as ScheduleType })}>
                    <option value="cron">cron expression</option>
                    <option value="interval">recurring interval</option>
                    <option value="oneshot">one-shot</option>
                  </select>
                </label>
                {draft.scheduleType === 'cron' && (
                  <label className="flex flex-col gap-1 text-xs">
                    Crontab (5+ fields)
                    <input
                      className="rounded-md border bg-background px-2 py-1 font-mono text-sm"
                      value={draft.expr}
                      onChange={(e) => set({ expr: e.target.value })}
                      placeholder="0 9 * * MON"
                    />
                  </label>
                )}
                {draft.scheduleType === 'interval' && (
                  <label className="flex flex-col gap-1 text-xs">
                    Interval (seconds)
                    <input
                      type="number"
                      min={1}
                      className="rounded-md border bg-background px-2 py-1 text-sm"
                      value={draft.intervalSeconds}
                      onChange={(e) => set({ intervalSeconds: e.target.value })}
                    />
                  </label>
                )}
                {draft.scheduleType === 'oneshot' && (
                  <label className="flex flex-col gap-1 text-xs">
                    Run at (local time)
                    <input
                      type="datetime-local"
                      className="rounded-md border bg-background px-2 py-1 text-sm"
                      value={draft.runAt}
                      onChange={(e) => set({ runAt: e.target.value })}
                    />
                  </label>
                )}
              </div>

              <label className="flex flex-col gap-1 text-xs">
                Script path (relative to coding workspace)
                <input
                  className="rounded-md border bg-background px-2 py-1 font-mono text-sm"
                  value={draft.scriptPath}
                  onChange={(e) => set({ scriptPath: e.target.value })}
                  placeholder="scripts/backup.py"
                />
              </label>

              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1 text-xs">
                  Interpreter (optional)
                  <input
                    className="rounded-md border bg-background px-2 py-1 text-sm"
                    value={draft.interpreter}
                    onChange={(e) => set({ interpreter: e.target.value })}
                    placeholder="python (else by extension)"
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs">
                  Arguments (space separated)
                  <input
                    className="rounded-md border bg-background px-2 py-1 font-mono text-sm"
                    value={draft.args}
                    onChange={(e) => set({ args: e.target.value })}
                    placeholder="--full"
                  />
                </label>
              </div>

              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
                Enabled
              </label>

              <div className="flex items-center gap-2">
                <Button size="sm" onClick={() => void save()} disabled={saving}>
                  {draft.id ? 'Save changes' : 'Create job'}
                </Button>
                {draft.id && (
                  <Button variant="destructive" size="sm" onClick={() => setConfirmDelete(true)} disabled={saving}>
                    <Trash2 className="mr-1 h-3.5 w-3.5" /> Delete
                  </Button>
                )}
                {draft.id && (
                  <Button variant="outline" size="sm" onClick={newJob}>
                    New job
                  </Button>
                )}
              </div>
            </div>

            {/* Run timeline (chronological status; click for detail) */}
            {draft.id && (
              <div className="mt-5 border-t pt-4">
                <h3 className="mb-2 text-xs font-semibold text-muted-foreground">Run history</h3>
                {runs.length === 0 ? (
                  <p className="text-xs text-muted-foreground">No runs yet.</p>
                ) : (
                  <ul className="space-y-1">
                    {runs.map((run) => (
                      <li key={run.run_id}>
                        <button
                          type="button"
                          onClick={() => void openRunDetail(run.run_id)}
                          className="flex w-full items-center justify-between gap-2 rounded-md border px-2 py-1 text-left text-xs hover:bg-muted">
                          <span className="truncate">{fmt(run.started_at)}</span>
                          <span className={cn('shrink-0 uppercase', stateColor(run.status))}>{run.status}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}

                {runDetail && (
                  <div className="mt-3 rounded-md border p-3">
                    <div className="mb-2 flex items-center justify-between text-xs">
                      <span className={cn('font-semibold uppercase', stateColor(runDetail.status))}>
                        {runDetail.status}
                      </span>
                      <span className="text-muted-foreground">
                        exit {runDetail.exit_code ?? '-'} · {runDetail.duration_ms ?? '-'}ms ·{' '}
                        {runDetail.interpreter || '-'}
                      </span>
                    </div>
                    {runDetail.stdout && (
                      <>
                        <div className="text-[10px] font-medium text-muted-foreground">stdout</div>
                        <pre className="mb-2 max-h-40 overflow-auto rounded bg-muted p-2 text-[11px]">
                          {runDetail.stdout}
                        </pre>
                      </>
                    )}
                    {runDetail.stderr && (
                      <>
                        <div className="text-[10px] font-medium text-muted-foreground">stderr</div>
                        <pre className="max-h-40 overflow-auto rounded bg-muted p-2 text-[11px]">
                          {runDetail.stderr}
                        </pre>
                      </>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {(saving || confirmDelete) && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
              {saving ? (
                <div className="flex items-center gap-2 text-sm">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  Applying...
                </div>
              ) : (
                /* Final confirmation before delete (UDR-0068 D6). */
                <div className="w-[340px] rounded-lg border bg-background p-4 shadow-lg">
                  <p className="text-sm font-medium">Delete this job?</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    "{draft.description || draft.id}" and its run history will be removed. This cannot be undone.
                  </p>
                  <div className="mt-3 flex justify-end gap-2">
                    <Button variant="outline" size="sm" onClick={() => setConfirmDelete(false)}>
                      Cancel
                    </Button>
                    <Button variant="destructive" size="sm" onClick={() => void remove()}>
                      <Trash2 className="mr-1 h-3.5 w-3.5" /> Delete
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t px-6 py-3">
          <span className="text-xs text-destructive">{error}</span>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={saving}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
