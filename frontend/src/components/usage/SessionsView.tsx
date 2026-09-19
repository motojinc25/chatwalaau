import { ExternalLink, Loader2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  fmtFull,
  fmtInstant,
  inputSum,
  lookupSessions,
  TEMPORARY_KEY,
  UNKNOWN_KEY,
  type UsageGroup,
  volume,
} from '@/lib/usageApi'
import { type UsageColumn, UsageTable } from './UsageTable'
import { Section, type ViewProps } from './UsageViews'
import { useUsageSummary } from './useUsageSummary'

/**
 * Sessions view (CTR-0215, UDR-0155 D6).
 *
 * The ledger names chats only by id, and an id may no longer exist (deleted,
 * re-imported). Titles come from the session index through the lookup endpoint for
 * the visible page only, and "Open" re-checks the one id at click time before the
 * parent navigates -- the table can be minutes old.
 */

const PAGE_SIZE = 50

type RowState = 'exists' | 'missing' | 'temporary' | 'unknown' | 'pending'

type SessionRow = UsageGroup & { state: RowState; title?: string }

const STATE_TEXT: Record<Exclude<RowState, 'exists'>, string> = {
  missing: 'Deleted or re-imported chat',
  temporary: 'Temporary chats',
  unknown: 'No chat (jobs, API, Teams)',
  pending: 'Checking...',
}

export interface OpenChatRequest {
  threadId: string
  title: string
}

export function SessionsView({
  query,
  reload,
  onRequestOpen,
}: ViewProps & { onRequestOpen: (req: OpenChatRequest, markMissing: () => void) => void }) {
  const state = useUsageSummary(query, 'chat', undefined, reload)
  const [page, setPage] = useState(0)
  const [lookup, setLookup] = useState<Record<string, { state: RowState; title?: string }>>({})
  const [lookupError, setLookupError] = useState<string | null>(null)

  const ranked = useMemo(() => [...(state.data?.groups ?? [])].sort((a, b) => volume(b) - volume(a)), [state.data])
  const pages = Math.max(1, Math.ceil(ranked.length / PAGE_SIZE))
  const visible = ranked.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  // Keep the page in range when a refresh shrinks the list.
  useEffect(() => {
    setPage((p) => Math.min(p, pages - 1))
  }, [pages])

  const visibleIds = visible.map((g) => g.key).filter((k) => k !== TEMPORARY_KEY && k !== UNKNOWN_KEY)
  const idsKey = visibleIds.join(',')

  // biome-ignore lint/correctness/useExhaustiveDependencies: idsKey captures visibleIds
  useEffect(() => {
    if (!idsKey) return
    const controller = new AbortController()
    setLookupError(null)
    lookupSessions(idsKey.split(','), controller.signal)
      .then((res) => {
        setLookup((prev) => {
          const next = { ...prev }
          for (const [id, meta] of Object.entries(res.found)) next[id] = { state: 'exists', title: meta.title }
          for (const id of res.missing) next[id] = { state: 'missing' }
          return next
        })
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setLookupError(err instanceof Error ? err.message : String(err))
      })
    return () => controller.abort()
  }, [idsKey, reload])

  const rows: SessionRow[] = visible.map((g) => {
    if (g.key === TEMPORARY_KEY) return { ...g, state: 'temporary' }
    if (g.key === UNKNOWN_KEY) return { ...g, state: 'unknown' }
    const known = lookup[g.key]
    return { ...g, state: known?.state ?? 'pending', title: known?.title }
  })

  const markMissing = (id: string) => () => setLookup((prev) => ({ ...prev, [id]: { state: 'missing' } }))

  const columns: UsageColumn<SessionRow>[] = [
    {
      id: 'title',
      label: 'Chat',
      value: (r) => (r.state === 'exists' ? r.title || '(untitled)' : STATE_TEXT[r.state]),
      render: (r) =>
        r.state === 'exists' ? (
          <span className="block max-w-[320px] truncate font-medium" title={r.title}>
            {r.title || '(untitled)'}
          </span>
        ) : (
          <span className="italic text-muted-foreground">{STATE_TEXT[r.state]}</span>
        ),
    },
    {
      id: 'id',
      label: 'Thread',
      value: (r) => r.key,
      render: (r) =>
        r.key.startsWith('(') ? (
          ''
        ) : (
          <span className="font-mono text-[10px] text-muted-foreground" title={r.key}>
            {r.key.slice(0, 8)}
          </span>
        ),
    },
    { id: 'records', label: 'Records', value: (r) => r.records, render: (r) => fmtFull(r.records), align: 'right' },
    {
      id: 'model_calls',
      label: 'Calls',
      value: (r) => r.model_calls,
      render: (r) => fmtFull(r.model_calls),
      align: 'right',
    },
    { id: 'input', label: 'Input', value: (r) => inputSum(r), render: (r) => fmtFull(inputSum(r)), align: 'right' },
    {
      id: 'output',
      label: 'Output',
      value: (r) => r.output_token_count,
      render: (r) => fmtFull(r.output_token_count),
      align: 'right',
    },
    {
      id: 'last',
      label: 'Last used',
      value: (r) => r.last_ts,
      render: (r) => fmtInstant(r.last_ts, query.tz),
    },
    {
      id: 'open',
      label: '',
      value: () => undefined,
      render: (r) => (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-[11px]"
          disabled={r.state !== 'exists'}
          onClick={() => onRequestOpen({ threadId: r.key, title: r.title || '(untitled)' }, markMissing(r.key))}
          title={r.state === 'exists' ? 'Open this chat' : 'This chat cannot be opened'}>
          <ExternalLink className="mr-1 h-3 w-3" /> Open
        </Button>
      ),
    },
  ]

  return (
    <Section
      title={`Chats (${ranked.length})`}
      actions={
        pages > 1 && (
          <div className="flex items-center gap-2 text-[11px]">
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2"
              disabled={page === 0}
              onClick={() => setPage(page - 1)}>
              Prev
            </Button>
            <span className="text-muted-foreground">
              {page + 1} / {pages}
            </span>
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2"
              disabled={page >= pages - 1}
              onClick={() => setPage(page + 1)}>
              Next
            </Button>
          </div>
        )
      }>
      <p className="text-[11px] text-muted-foreground">
        Ranked by input + output. A chat that was deleted or re-imported keeps its recorded usage but cannot be opened;
        temporary chats are recorded without an id.
      </p>
      {lookupError && <p className="text-xs text-destructive">Could not check chats: {lookupError}</p>}
      {state.error ? (
        <p className="text-xs text-destructive">{state.error}</p>
      ) : !state.data ? (
        <div className="flex h-24 items-center justify-center text-xs text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
        </div>
      ) : (
        <UsageTable
          rows={rows}
          columns={columns}
          rowKey={(r) => r.key}
          initialSort={{ id: 'input', desc: true }}
          empty="No chat usage in this range."
        />
      )}
    </Section>
  )
}
