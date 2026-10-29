// The decision log (CTR-0276, PRP-0235, UDR-0218 D3): every completed AI discussion across
// chats, with the person's decision apart from the AI's recommendation. It reads only the
// session index through GET /api/discussions/decisions; a row opens its chat and exports its run.
import { ListChecks } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { DiscussionExportActions } from '@/components/DiscussionExport'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { type DecisionFilter, type DecisionLogItem, discussionApi } from '@/lib/discussionApi'
import { cn } from '@/lib/utils'

const FILTERS: { id: DecisionFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'adopt', label: 'Adopted' },
  { id: 'hold', label: 'On hold' },
  { id: 'rediscuss', label: 'Re-discuss' },
  { id: 'undecided', label: 'Not decided' },
]

const VERDICT_BADGES: Record<string, { label: string; className: string }> = {
  adopt: { label: 'Adopted', className: 'bg-green-500/10 text-green-700 dark:text-green-300' },
  hold: { label: 'On hold', className: 'bg-amber-500/10 text-amber-700 dark:text-amber-300' },
  rediscuss: { label: 'Re-discuss', className: 'bg-blue-500/10 text-blue-700 dark:text-blue-300' },
}

const PAGE = 50

export function DiscussionDecisionLog({ onOpenChat }: { onOpenChat: (threadId: string) => void }) {
  const [open, setOpen] = useState(false)
  const [verdict, setVerdict] = useState<DecisionFilter>('all')
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<DecisionLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const load = useCallback(
    async (offset: number) => {
      setLoading(true)
      setError(null)
      try {
        const res = await discussionApi.decisions({ verdict, q: query, limit: PAGE, offset })
        setItems((prev) => (offset === 0 ? res.items : [...prev, ...res.items]))
        setTotal(res.total)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not load the decision log')
      } finally {
        setLoading(false)
      }
    },
    [verdict, query],
  )

  // Reload on open and when the filter changes; the search waits for typing to pause.
  useEffect(() => {
    if (!open) return
    const timer = setTimeout(() => void load(0), query ? 250 : 0)
    return () => clearTimeout(timer)
  }, [open, load, query])

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6 shrink-0 text-muted-foreground"
        onClick={() => setOpen(true)}
        aria-label="Decision log"
        title="Decision log">
        <ListChecks className="h-4 w-4" />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="flex max-h-[90vh] w-[min(900px,calc(100vw-2rem))] max-w-none flex-col gap-3 sm:max-w-none">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ListChecks className="h-4 w-4" aria-hidden="true" />
              Decision log
            </DialogTitle>
            <DialogDescription>
              Every finished AI discussion, newest first. The recommendation is the AI&apos;s; the verdict is yours.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap items-center gap-2">
            {FILTERS.map((f) => (
              <Button
                key={f.id}
                size="sm"
                variant={verdict === f.id ? 'default' : 'outline'}
                className="h-7"
                onClick={() => setVerdict(f.id)}>
                {f.label}
              </Button>
            ))}
            <Input
              aria-label="Search the decision log"
              className="h-7 min-w-0 flex-1 text-xs"
              placeholder="Search topic, recommendation, note"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          {error && (
            <p role="alert" className="text-xs text-red-600">
              {error}
            </p>
          )}
          <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1" data-testid="decision-log">
            {items.length === 0 && !loading ? (
              <p className="py-6 text-center text-sm text-muted-foreground">No finished discussion yet.</p>
            ) : (
              <table className="w-full text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-2 font-medium">Date</th>
                    <th className="py-1 pr-2 font-medium">Topic</th>
                    <th className="py-1 pr-2 font-medium">Your decision</th>
                    <th className="py-1 font-medium">AI recommendation</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => {
                    const badge = item.verdict ? VERDICT_BADGES[item.verdict] : undefined
                    const key = `${item.thread_id}:${item.run_id}`
                    return (
                      <tr key={key} className="border-t align-top" data-testid="decision-log-row">
                        <td className="whitespace-nowrap py-1.5 pr-2 tabular-nums">
                          {(item.decided_at || item.updated_at || '').slice(0, 10)}
                        </td>
                        <td className="py-1.5 pr-2">
                          <button
                            type="button"
                            className="text-left font-medium hover:underline"
                            onClick={() => {
                              setOpen(false)
                              onOpenChat(item.thread_id)
                            }}>
                            {item.topic || item.title}
                          </button>
                          {item.parent_run_id && <span className="ml-1 text-muted-foreground">(continued)</span>}
                          <div>
                            <button
                              type="button"
                              className="text-muted-foreground hover:underline"
                              onClick={() => setExpanded(expanded === key ? null : key)}>
                              {expanded === key ? 'Hide export' : 'Export...'}
                            </button>
                          </div>
                          {expanded === key && (
                            <div className="mt-1">
                              <DiscussionExportActions threadId={item.thread_id} runId={item.run_id} compact />
                            </div>
                          )}
                        </td>
                        <td className="py-1.5 pr-2">
                          <span
                            className={cn(
                              'rounded-full px-2 py-px text-[11px] font-medium',
                              badge?.className ?? 'bg-muted text-muted-foreground',
                            )}>
                            {badge?.label ?? 'Not decided'}
                          </span>
                          {item.note && <div className="mt-1 text-muted-foreground">{item.note}</div>}
                        </td>
                        <td className="py-1.5 text-muted-foreground">{item.recommendation || '-'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </div>
          {items.length < total && (
            <Button size="sm" variant="outline" className="h-7 self-center" onClick={() => void load(items.length)}>
              Show more ({total - items.length})
            </Button>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
