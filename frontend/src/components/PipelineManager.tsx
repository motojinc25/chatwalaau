import { Ban, Loader2, Plus, RefreshCw, Trash2, Workflow } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * Pipeline Job portal (CTR-0148, FEAT-0021, PRP-0096, UDR-0074).
 *
 * Opened from a launcher icon in the SessionSidebar footer (next to the Declarative
 * Agents launcher). A ~90% modal styled like CronManager / SkillsManager: left = job
 * list (status badge, type, live progress); right = either a registry-driven submit
 * form (job type + its params, from GET /api/pipeline/types) or a selected job's detail
 * with a run-history timeline (click a run -> captured log). Submit / cancel / delete
 * reflect to the backend immediately (CTR-0146) with a blocking saving indicator.
 *
 * Controlled component: the parent (ChatPage) owns open state. The store is server-side
 * (CTR-0145); no client-side persistence. Unlike Cron, pipeline jobs are ON-DEMAND
 * (submit once), so there is no schedule and no in-place edit.
 */

interface ParamSpec {
  name: string
  label: string
  type: string // string | int | number
  required: boolean
  default: unknown
  help: string
}

interface JobType {
  name: string
  label: string
  description: string
  params: ParamSpec[]
}

interface PipelineJob {
  id: string
  type: string
  status: string
  progress: number
  progress_message: string
  params: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  last_run_id: string | null
}

interface PipelineRun {
  run_id: string
  job_id: string
  job_type: string
  status: string
  started_at: string
  finished_at: string | null
  progress: number
  duration_ms: number | null
  log?: string
}

interface PipelineManagerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

function fmt(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

function statusColor(status: string): string {
  switch (status) {
    case 'completed':
      return 'text-emerald-600 dark:text-emerald-500'
    case 'failed':
      return 'text-destructive'
    case 'running':
      return 'text-blue-600 dark:text-blue-400'
    case 'cancelled':
      return 'text-muted-foreground'
    case 'pending':
      return 'text-amber-600 dark:text-amber-500'
    default:
      return 'text-foreground'
  }
}

const ACTIVE = new Set(['running', 'pending'])

export function PipelineManager({ open, onOpenChange }: PipelineManagerProps) {
  const [types, setTypes] = useState<JobType[]>([])
  const [jobs, setJobs] = useState<PipelineJob[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [draftType, setDraftType] = useState<string>('')
  const [draftParams, setDraftParams] = useState<Record<string, string>>({})
  const [runs, setRuns] = useState<PipelineRun[]>([])
  const [runDetail, setRunDetail] = useState<PipelineRun | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const selected = jobs.find((j) => j.id === selectedId) ?? null

  const fetchTypes = useCallback(async () => {
    try {
      const res = await fetch('/api/pipeline/types')
      if (!res.ok) return
      const data = await res.json()
      const list = (data.types ?? []) as JobType[]
      setTypes(list)
      setDraftType((prev) => prev || (list[0]?.name ?? ''))
    } catch {
      // ignore: types are best-effort
    }
  }, [])

  const fetchJobs = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/pipeline/jobs')
      if (!res.ok) throw new Error('Failed to load pipeline jobs')
      const data = await res.json()
      setJobs((data.jobs ?? []) as PipelineJob[])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load pipeline jobs')
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchRuns = useCallback(async (jobId: string) => {
    setRunDetail(null)
    try {
      const res = await fetch(`/api/pipeline/jobs/${jobId}/runs`)
      if (!res.ok) return
      const data = await res.json()
      setRuns((data.runs ?? []) as PipelineRun[])
    } catch {
      setRuns([])
    }
  }, [])

  const openRunDetail = useCallback(async (runId: string) => {
    try {
      const res = await fetch(`/api/pipeline/runs/${runId}`)
      if (!res.ok) return
      setRunDetail((await res.json()) as PipelineRun)
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => {
    if (open) {
      void fetchTypes()
      void fetchJobs()
      setSelectedId(null)
      setRuns([])
      setRunDetail(null)
      setConfirmDelete(false)
    }
  }, [open, fetchTypes, fetchJobs])

  // Live progress: while the modal is open and any job is active, poll the list.
  const jobsRef = useRef(jobs)
  jobsRef.current = jobs
  useEffect(() => {
    if (!open) return
    const id = setInterval(() => {
      if (jobsRef.current.some((j) => ACTIVE.has(j.status))) {
        void fetchJobs()
        if (selectedId) void fetchRuns(selectedId)
      }
    }, 3000)
    return () => clearInterval(id)
  }, [open, selectedId, fetchJobs, fetchRuns])

  const selectJob = useCallback(
    (job: PipelineJob) => {
      setSelectedId(job.id)
      void fetchRuns(job.id)
    },
    [fetchRuns],
  )

  const newJob = useCallback(() => {
    setSelectedId(null)
    setDraftParams({})
    setRuns([])
    setRunDetail(null)
  }, [])

  const activeType = types.find((t) => t.name === draftType) ?? null

  const submit = useCallback(async () => {
    if (!draftType) {
      setError('Pick a job type.')
      return
    }
    const spec = types.find((t) => t.name === draftType)
    const params: Record<string, unknown> = {}
    for (const p of spec?.params ?? []) {
      const raw = (draftParams[p.name] ?? '').trim()
      if (!raw) {
        if (p.required) {
          setError(`"${p.label}" is required.`)
          return
        }
        continue
      }
      params[p.name] = p.type === 'int' || p.type === 'number' ? Number(raw) : raw
    }
    setBusy(true)
    setError(null)
    try {
      const res = await fetch('/api/pipeline/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: draftType, params }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detail?.detail?.error ?? 'Failed to submit job')
      }
      const job = (await res.json()) as PipelineJob
      await fetchJobs()
      selectJob(job)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit job')
    } finally {
      setBusy(false)
    }
  }, [draftType, draftParams, types, fetchJobs, selectJob])

  const cancel = useCallback(async () => {
    if (!selectedId) return
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/pipeline/jobs/${selectedId}/cancel`, { method: 'POST' })
      if (!res.ok) throw new Error('Failed to cancel job')
      await fetchJobs()
      void fetchRuns(selectedId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to cancel job')
    } finally {
      setBusy(false)
    }
  }, [selectedId, fetchJobs, fetchRuns])

  const remove = useCallback(async () => {
    if (!selectedId) return
    setConfirmDelete(false)
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/pipeline/jobs/${selectedId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete job')
      await fetchJobs()
      newJob()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete job')
    } finally {
      setBusy(false)
    }
  }, [selectedId, fetchJobs, newJob])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle className="flex items-center gap-2">
            <Workflow className="h-4 w-4" /> Pipeline Jobs
          </DialogTitle>
          <DialogDescription>
            Run data-processing pipeline jobs (RAG ingestion). Submit a job, watch its progress, and review the run
            history. Distinct from the Cron Scheduler (scheduled scripts).
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
                  {loading ? 'Loading...' : 'No jobs yet. Click + to submit one.'}
                </p>
              ) : (
                jobs.map((job) => (
                  <button
                    type="button"
                    key={job.id}
                    onClick={() => selectJob(job)}
                    className={cn(
                      'flex w-full flex-col items-start gap-0.5 border-b px-3 py-2 text-left',
                      selectedId === job.id && 'bg-accent',
                    )}>
                    <span className="flex w-full items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{job.type}</span>
                      <span className={cn('shrink-0 text-[10px] uppercase', statusColor(job.status))}>
                        {job.status}
                      </span>
                    </span>
                    <span className="truncate text-[11px] text-muted-foreground">
                      {job.progress}% · {job.progress_message || job.id}
                    </span>
                    <span className="text-[10px] text-muted-foreground">{fmt(job.created_at)}</span>
                  </button>
                ))
              )}
            </div>
          </div>

          {/* Right: submit form OR selected job detail + run timeline */}
          <div className="min-w-0 flex-1 overflow-y-auto p-4">
            {selected ? (
              <div className="grid grid-cols-1 gap-3">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <div className="flex items-center gap-2 text-sm font-semibold">
                      {selected.type}
                      <span className={cn('text-[11px] uppercase', statusColor(selected.status))}>
                        {selected.status}
                      </span>
                    </div>
                    <div className="text-[11px] text-muted-foreground">{selected.id}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    {ACTIVE.has(selected.status) && (
                      <Button variant="outline" size="sm" onClick={() => void cancel()} disabled={busy}>
                        <Ban className="mr-1 h-3.5 w-3.5" /> Cancel
                      </Button>
                    )}
                    {!ACTIVE.has(selected.status) && (
                      <Button variant="destructive" size="sm" onClick={() => setConfirmDelete(true)} disabled={busy}>
                        <Trash2 className="mr-1 h-3.5 w-3.5" /> Delete
                      </Button>
                    )}
                    <Button variant="outline" size="sm" onClick={newJob}>
                      New job
                    </Button>
                  </div>
                </div>

                {/* Progress bar */}
                <div>
                  <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
                    <span>{selected.progress_message || ' '}</span>
                    <span>{selected.progress}%</span>
                  </div>
                  <div className="h-2 w-full overflow-hidden rounded bg-muted">
                    <div
                      className={cn(
                        'h-full transition-all',
                        selected.status === 'failed' ? 'bg-destructive' : 'bg-primary',
                      )}
                      style={{ width: `${Math.max(0, Math.min(100, selected.progress))}%` }}
                    />
                  </div>
                </div>

                <div className="rounded-md border p-3 text-xs">
                  <div className="mb-1 font-medium text-muted-foreground">params</div>
                  <pre className="max-h-32 overflow-auto text-[11px]">{JSON.stringify(selected.params, null, 2)}</pre>
                </div>

                {selected.result && (
                  <div className="rounded-md border p-3 text-xs">
                    <div className="mb-1 font-medium text-muted-foreground">result</div>
                    <pre className="max-h-32 overflow-auto text-[11px]">{JSON.stringify(selected.result, null, 2)}</pre>
                  </div>
                )}

                {selected.error && (
                  <div className="rounded-md border border-destructive/40 p-3 text-xs text-destructive">
                    {selected.error}
                  </div>
                )}

                {/* Run history (timeline; click for captured log) */}
                <div className="mt-2 border-t pt-4">
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
                            <span className={cn('shrink-0 uppercase', statusColor(run.status))}>{run.status}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  {runDetail && (
                    <div className="mt-3 rounded-md border p-3">
                      <div className="mb-2 flex items-center justify-between text-xs">
                        <span className={cn('font-semibold uppercase', statusColor(runDetail.status))}>
                          {runDetail.status}
                        </span>
                        <span className="text-muted-foreground">{runDetail.duration_ms ?? '-'}ms</span>
                      </div>
                      {runDetail.log && (
                        <pre className="max-h-48 overflow-auto rounded bg-muted p-2 text-[11px]">{runDetail.log}</pre>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              /* Submit form (registry-driven) */
              <div className="grid grid-cols-1 gap-3">
                <h3 className="text-sm font-semibold">Submit a new job</h3>
                <label className="flex flex-col gap-1 text-xs">
                  Job type
                  <select
                    className="rounded-md border bg-background px-2 py-1 text-sm"
                    value={draftType}
                    onChange={(e) => {
                      setDraftType(e.target.value)
                      setDraftParams({})
                    }}>
                    {types.map((t) => (
                      <option key={t.name} value={t.name}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                </label>

                {activeType && <p className="text-[11px] text-muted-foreground">{activeType.description}</p>}

                {(activeType?.params ?? []).map((p) => (
                  <label key={p.name} className="flex flex-col gap-1 text-xs">
                    {p.label}
                    {p.required ? ' *' : ''}
                    <input
                      className="rounded-md border bg-background px-2 py-1 font-mono text-sm"
                      type={p.type === 'int' || p.type === 'number' ? 'number' : 'text'}
                      value={draftParams[p.name] ?? ''}
                      onChange={(e) => setDraftParams((prev) => ({ ...prev, [p.name]: e.target.value }))}
                      placeholder={p.help}
                    />
                    {p.help && <span className="text-[10px] text-muted-foreground">{p.help}</span>}
                  </label>
                ))}

                <div>
                  <Button size="sm" onClick={() => void submit()} disabled={busy || !draftType}>
                    Submit job
                  </Button>
                </div>
              </div>
            )}
          </div>

          {(busy || confirmDelete) && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
              {busy ? (
                <div className="flex items-center gap-2 text-sm">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  Applying...
                </div>
              ) : (
                <div className="w-[340px] rounded-lg border bg-background p-4 shadow-lg">
                  <p className="text-sm font-medium">Delete this job?</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    "{selected?.type} {selected?.id}" and its run history will be removed. This cannot be undone.
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
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
