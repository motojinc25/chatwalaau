import { ChartColumn, Download, ExternalLink, Loader2, RefreshCw } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { type OpenChatRequest, SessionsView } from '@/components/usage/SessionsView'
import { TimeZonePicker } from '@/components/usage/TimeZonePicker'
import { UsageKpis } from '@/components/usage/UsageKpis'
import { ByModelView, ByTypeView, DailyView, MonthlyView, OverviewView } from '@/components/usage/UsageViews'
import { useUsageSummary } from '@/components/usage/useUsageSummary'
import {
  daysBetween,
  LANES,
  loadTimeZone,
  lookupSessions,
  MAX_RANGE_DAYS,
  presetRange,
  RANGE_PRESETS,
  type RangePreset,
  saveTimeZone,
  todayIn,
  type UsageQuery,
  usageExportFilename,
  usageExportUrl,
} from '@/lib/usageApi'
import { cn } from '@/lib/utils'
import { downloadUrl } from '@/lib/workspace-download'

/**
 * Token Usage Dashboard (CTR-0215, FEAT-0069, PRP-0173, UDR-0155).
 *
 * Opened from the SessionSidebar footer icon next to Ontology. Same ~90% modal
 * structure as the Cron Scheduler (CTR-0135): left = range / lane filters and the view
 * list; right = KPIs, the view's chart and table. Read-only over CTR-0201: it never
 * edits a ledger record and shows no money.
 *
 * The calendar zone is always visible and changeable (UDR-0155 D2); every request and
 * the export carry it. A chat is opened only after the parent-provided check succeeds
 * (UDR-0155 D6).
 */

type View = 'overview' | 'daily' | 'monthly' | 'type' | 'model' | 'sessions'

const VIEWS: Array<{ id: View; label: string; hint: string }> = [
  { id: 'overview', label: 'Overview', hint: 'Trend and top consumers' },
  { id: 'daily', label: 'Daily', hint: 'Per local day' },
  { id: 'monthly', label: 'Monthly', hint: 'Per month, change vs previous' },
  { id: 'type', label: 'By type', hint: 'Lane, run target, workflow node' },
  { id: 'model', label: 'By model', hint: 'Token kinds, cache, errors' },
  { id: 'sessions', label: 'Sessions', hint: 'Per chat, open a chat' },
]

interface UsageDashboardProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Opens a chat on the chat page; the dashboard closes itself first. */
  onOpenChat: (threadId: string) => void
}

export function UsageDashboard({ open, onOpenChange, onOpenChat }: UsageDashboardProps) {
  const [tz, setTz] = useState(loadTimeZone)
  const [preset, setPreset] = useState<RangePreset>('30d')
  const [custom, setCustom] = useState(() => presetRange('30d', loadTimeZone()))
  const [lane, setLane] = useState<string>('')
  const [view, setView] = useState<View>('overview')
  const [reload, setReload] = useState(0)
  const [exporting, setExporting] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<(OpenChatRequest & { markMissing: () => void }) | null>(null)
  const [checking, setChecking] = useState(false)

  const range = preset === 'custom' ? custom : presetRange(preset, tz)
  const span = daysBetween(range.from, range.to)
  const rangeError =
    range.from > range.to
      ? 'The start date is after the end date.'
      : span >= MAX_RANGE_DAYS
        ? `Choose at most ${MAX_RANGE_DAYS} days.`
        : null

  const query: UsageQuery | null = useMemo(
    () => (rangeError ? null : { from: range.from, to: range.to, tz, lane: lane || null }),
    [range.from, range.to, tz, lane, rangeError],
  )

  // KPI sources (shared by every view).
  const models = useUsageSummary(open ? query : null, 'model', undefined, reload)
  const outcomes = useUsageSummary(open ? query : null, 'outcome', undefined, reload)
  const summary = models.data

  const changeTz = (next: string) => {
    setTz(next)
    saveTimeZone(next)
  }

  const changePreset = (next: RangePreset) => {
    if (next === 'custom' && preset !== 'custom') setCustom(range)
    setPreset(next)
  }

  const exportCsv = async () => {
    if (!query) return
    setExporting(true)
    setMessage(null)
    const failure = await downloadUrl(usageExportUrl(query), usageExportFilename(query))
    setExporting(false)
    if (failure) setMessage(failure)
  }

  const requestOpen = useCallback((req: OpenChatRequest, markMissing: () => void) => {
    setMessage(null)
    setConfirm({ ...req, markMissing })
  }, [])

  const confirmOpen = async () => {
    if (!confirm) return
    setChecking(true)
    try {
      // Re-check at the moment of the click (UDR-0155 D6).
      const res = await lookupSessions([confirm.threadId])
      if (!res.found[confirm.threadId]) {
        confirm.markMissing()
        setMessage('That chat no longer exists. Its usage stays in the statistics.')
        setConfirm(null)
        return
      }
      setConfirm(null)
      onOpenChange(false)
      onOpenChat(confirm.threadId)
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setChecking(false)
    }
  }

  const viewProps = query ? { query, reload } : null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <div className="flex flex-wrap items-start justify-between gap-3 pr-8">
            <div className="min-w-0">
              <DialogTitle className="flex items-center gap-2">
                <ChartColumn className="h-4 w-4" /> Token Usage
              </DialogTitle>
              <DialogDescription>
                Token counts observed by this server, per day, month, type, model and chat. Not a bill.
              </DialogDescription>
            </div>
            <div className="flex items-center gap-2">
              <TimeZonePicker value={tz} onChange={changeTz} />
              <Button
                variant="outline"
                size="sm"
                className="h-7 gap-1.5 px-2 text-xs"
                disabled={!query || exporting}
                onClick={() => void exportCsv()}
                title="Download the raw usage records in this range as CSV (for BI tools)">
                {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                Export CSV
              </Button>
            </div>
          </div>
          {summary?.coverage && (
            <details className="mt-1 text-[11px] text-muted-foreground">
              <summary className="cursor-pointer select-none">What these numbers cover</summary>
              <p className="mt-1 max-w-3xl">{summary.coverage}</p>
            </details>
          )}
        </DialogHeader>

        <div className="relative flex min-h-0 flex-1">
          {/* Left: filters + views */}
          <div className="flex w-72 shrink-0 flex-col border-r">
            <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
              <span className="text-xs font-medium text-muted-foreground">
                {summary ? `${summary.totals.records.toLocaleString()} records` : 'Records'}
              </span>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                onClick={() => setReload((n) => n + 1)}
                title="Refresh">
                <RefreshCw className={cn('h-3.5 w-3.5', models.loading && 'animate-spin')} />
              </Button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="flex flex-col gap-2 border-b px-3 py-3">
                <span className="text-[11px] font-medium text-muted-foreground">Range</span>
                <div className="flex flex-wrap gap-1">
                  {RANGE_PRESETS.map((p) => (
                    <button
                      type="button"
                      key={p.id}
                      onClick={() => changePreset(p.id)}
                      className={cn(
                        'rounded border px-2 py-0.5 text-[11px]',
                        preset === p.id ? 'border-foreground bg-accent font-medium' : 'text-muted-foreground',
                      )}>
                      {p.label}
                    </button>
                  ))}
                </div>
                {preset === 'custom' ? (
                  <div className="grid grid-cols-2 gap-2">
                    <Input
                      type="date"
                      aria-label="From"
                      className="h-7 px-1 text-[11px]"
                      value={custom.from}
                      max={todayIn(tz)}
                      onChange={(e) => e.target.value && setCustom((c) => ({ ...c, from: e.target.value }))}
                    />
                    <Input
                      type="date"
                      aria-label="To"
                      className="h-7 px-1 text-[11px]"
                      value={custom.to}
                      max={todayIn(tz)}
                      onChange={(e) => e.target.value && setCustom((c) => ({ ...c, to: e.target.value }))}
                    />
                  </div>
                ) : (
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {range.from} .. {range.to}
                  </span>
                )}
                {rangeError && <span className="text-[11px] text-destructive">{rangeError}</span>}

                <label className="mt-1 flex flex-col gap-1 text-[11px] font-medium text-muted-foreground">
                  Lane
                  <select
                    value={lane}
                    onChange={(e) => setLane(e.target.value)}
                    className="h-7 rounded-md border bg-background px-1 text-[11px] font-normal text-foreground">
                    <option value="">All lanes</option>
                    {LANES.map((l) => (
                      <option key={l} value={l}>
                        {l}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <nav aria-label="Usage views">
                {VIEWS.map((v) => (
                  <button
                    type="button"
                    key={v.id}
                    onClick={() => setView(v.id)}
                    className={cn(
                      'flex w-full flex-col items-start gap-0.5 border-b px-3 py-2 text-left',
                      view === v.id && 'bg-accent',
                    )}>
                    <span className="text-sm font-medium">{v.label}</span>
                    <span className="text-[11px] text-muted-foreground">{v.hint}</span>
                  </button>
                ))}
              </nav>
            </div>
          </div>

          {/* Right: KPIs + view */}
          <div className="min-w-0 flex-1 overflow-y-auto p-6">
            {!viewProps ? (
              <p className="text-sm text-muted-foreground">Choose a valid range.</p>
            ) : (
              <div className="flex flex-col gap-6">
                {summary && summary.totals.records === 0 && !models.loading ? (
                  <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                    No usage recorded in this range.
                    <p className="mt-1 text-[11px]">
                      Demo mode records nothing, because its token counts are synthetic.
                    </p>
                  </div>
                ) : summary ? (
                  <UsageKpis totals={summary.totals} models={summary.groups} outcomes={outcomes.data?.groups ?? []} />
                ) : models.error ? (
                  <p className="text-xs text-destructive">{models.error}</p>
                ) : null}

                {summary && summary.skipped_lines > 0 && (
                  <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
                    {summary.skipped_lines} unreadable ledger line(s) were skipped in this range.
                  </p>
                )}

                {view === 'overview' && <OverviewView {...viewProps} />}
                {view === 'daily' && <DailyView {...viewProps} />}
                {view === 'monthly' && <MonthlyView {...viewProps} />}
                {view === 'type' && <ByTypeView {...viewProps} />}
                {view === 'model' && <ByModelView {...viewProps} />}
                {view === 'sessions' && <SessionsView {...viewProps} onRequestOpen={requestOpen} />}
              </div>
            )}
          </div>

          {confirm && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
              <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
                <p className="text-sm font-medium">Open this chat?</p>
                <p className="mt-1 truncate text-xs" title={confirm.title}>
                  "{confirm.title}"
                </p>
                <p className="mt-1 text-xs text-muted-foreground">The dashboard will close.</p>
                <div className="mt-3 flex justify-end gap-2">
                  <Button variant="outline" size="sm" onClick={() => setConfirm(null)} disabled={checking}>
                    Cancel
                  </Button>
                  <Button size="sm" onClick={() => void confirmOpen()} disabled={checking}>
                    {checking ? (
                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <ExternalLink className="mr-1 h-3.5 w-3.5" />
                    )}
                    Open
                  </Button>
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t px-6 py-3">
          <span className="text-xs text-destructive">{message}</span>
          <div className="flex items-center gap-3">
            <span className="text-[11px] text-muted-foreground">
              Dates in <span className="font-mono">{tz}</span>
            </span>
            <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
              Close
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
