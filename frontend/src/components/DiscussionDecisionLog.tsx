// The decision log (CTR-0276, PRP-0235, PRP-0249, UDR-0218 D3, UDR-0230): every completed AI
// discussion across chats, with the person's decision apart from the AI's recommendation. The
// list reads only the session index through GET /api/discussions/decisions; the detail reads the
// selected run.
import { ListChecks } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { DiscussionDecisionDetail, VerdictBadge } from '@/components/DiscussionDecisionDetail'
import { HighlightedText } from '@/components/HighlightedText'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { usePrivacyScreen } from '@/hooks/usePrivacyScreen'
import { localDateKey } from '@/lib/datetime'
import { type DecisionFilter, type DecisionLogItem, discussionApi } from '@/lib/discussionApi'
import { matchExcerpt, matchRanges } from '@/lib/textMatch'
import { cn } from '@/lib/utils'

const FILTERS: { id: DecisionFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'adopt', label: 'Adopted' },
  { id: 'hold', label: 'On hold' },
  { id: 'rediscuss', label: 'Re-discuss' },
  { id: 'undecided', label: 'Not decided' },
]

const PAGE = 50

const rowKey = (item: DecisionLogItem) => `${item.thread_id}:${item.run_id}`

/** One section per local day, in the order the server sorted the rows (UDR-0230 D2). */
function groupByDay(items: DecisionLogItem[]): { day: string; items: DecisionLogItem[] }[] {
  const days = new Map<string, DecisionLogItem[]>()
  for (const item of items) {
    const day = localDateKey(item.decided_at || item.updated_at || '') || 'No date'
    const rows = days.get(day)
    if (rows) rows.push(item)
    else days.set(day, [item])
  }
  return Array.from(days, ([day, rows]) => ({ day, items: rows }))
}

/** Why a row was found when its topic does not show it (UDR-0230 D8). */
function hitExcerpt(item: DecisionLogItem, query: string): { field: string; text: string } | null {
  if (!query.trim() || matchRanges(item.topic, query).length > 0) return null
  for (const [field, value] of [
    ['Note', item.note],
    ['AI', item.recommendation],
  ] as const) {
    const text = matchExcerpt(value, query)
    if (text) return { field, text }
  }
  return null
}

export function DiscussionDecisionLog({ onOpenChat }: { onOpenChat: (threadId: string) => void }) {
  const { enabled: redacted, redact } = usePrivacyScreen()
  const [open, setOpen] = useState(false)
  const [verdict, setVerdict] = useState<DecisionFilter>('all')
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<DecisionLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const latestLoad = useRef(0)

  const load = useCallback(
    async (offset: number) => {
      const request = ++latestLoad.current
      setLoading(true)
      setError(null)
      try {
        const res = await discussionApi.decisions({ verdict, q: query, limit: PAGE, offset })
        if (request !== latestLoad.current) return
        setItems((prev) => (offset === 0 ? res.items : [...prev, ...res.items]))
        setTotal(res.total)
      } catch (err) {
        if (request !== latestLoad.current) return
        setError(err instanceof Error ? err.message : 'Could not load the decision log')
      } finally {
        if (request === latestLoad.current) setLoading(false)
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

  const days = useMemo(() => groupByDay(items), [items])
  // A selection lasts while its run is listed; otherwise the newest row is shown.
  const selected = items.find((item) => rowKey(item) === selectedKey) ?? items[0] ?? null
  const unfiltered = verdict === 'all' && !query.trim()

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
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle className="flex items-center gap-2">
              <ListChecks className="h-4 w-4" aria-hidden="true" />
              Decision log
            </DialogTitle>
            <DialogDescription>
              Every finished AI discussion, newest first. The recommendation is the AI&apos;s; the verdict is yours.
              Searches the topic and the start of the recommendation and note.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap items-center gap-2 border-b px-6 py-2">
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
            <p role="alert" className="border-b px-6 py-2 text-xs text-red-600">
              {error}
            </p>
          )}
          {items.length === 0 && !loading ? (
            <p className="flex-1 py-6 text-center text-sm text-muted-foreground">
              {unfiltered ? 'No finished discussion yet.' : 'No decision matches.'}
            </p>
          ) : (
            <div className="flex min-h-0 flex-1">
              <div className="w-72 shrink-0 overflow-y-auto border-r" data-testid="decision-log">
                {days.map(({ day, items: rows }) => (
                  <div key={day} data-testid="decision-log-day">
                    <div className="sticky top-0 z-10 flex items-center justify-between bg-muted px-3 py-1.5 text-[11px] font-semibold tracking-wide text-muted-foreground">
                      <span className="tabular-nums">{day}</span>
                      <span className="tabular-nums">({rows.length})</span>
                    </div>
                    {rows.map((item) => {
                      const key = rowKey(item)
                      const excerpt = redacted ? null : hitExcerpt(item, query)
                      return (
                        <button
                          key={key}
                          type="button"
                          aria-current={selected !== null && rowKey(selected) === key ? 'true' : undefined}
                          data-testid="decision-log-row"
                          onClick={() => setSelectedKey(key)}
                          className={cn(
                            'block w-full border-b px-3 py-2 text-left',
                            selected !== null && rowKey(selected) === key && 'bg-accent',
                          )}>
                          <span className="flex items-start justify-between gap-2">
                            <span className="line-clamp-2 min-w-0 flex-1 break-words text-sm font-medium">
                              <HighlightedText
                                text={redact(item.topic || item.title, `decision-topic:${key}`)}
                                query={query}
                                redacted={redacted}
                              />
                            </span>
                            <VerdictBadge verdict={item.verdict} />
                          </span>
                          {excerpt && (
                            <span
                              className="mt-1 line-clamp-2 block break-words text-[11px] text-muted-foreground"
                              data-testid="decision-log-excerpt">
                              {excerpt.field}: <HighlightedText text={excerpt.text} query={query} redacted={false} />
                            </span>
                          )}
                        </button>
                      )
                    })}
                  </div>
                ))}
                {items.length < total && (
                  <div className="p-2 text-center">
                    <Button size="sm" variant="outline" className="h-7" onClick={() => void load(items.length)}>
                      Show more ({total - items.length})
                    </Button>
                  </div>
                )}
              </div>
              {selected && (
                <DiscussionDecisionDetail
                  key={rowKey(selected)}
                  item={selected}
                  query={query}
                  onOpenChat={() => {
                    setOpen(false)
                    onOpenChat(selected.thread_id)
                  }}
                />
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
