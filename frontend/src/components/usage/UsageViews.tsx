import { Loader2 } from 'lucide-react'
import { type ReactNode, useState } from 'react'
import {
  cacheHitRate,
  enumerateDays,
  enumerateMonths,
  fmtFull,
  fmtInstant,
  fmtPct,
  type GroupKey,
  STACKABLE_KINDS,
  TOKEN_KIND_LABEL,
  type TokenKind,
  tokensPerCall,
  UNKNOWN_KEY,
  type UsageGroup,
  type UsageQuery,
  volume,
} from '@/lib/usageApi'
import { cn } from '@/lib/utils'
import { KindRankingChart, KindStackedChart, SeriesStackedChart } from './UsageCharts'
import { type UsageColumn, UsageTable } from './UsageTable'
import { type SummaryState, useUsageSummary } from './useUsageSummary'

/**
 * The per-view panes of the Usage Dashboard (CTR-0215, PRP-0173 section 7.3).
 * Each view asks the backend for exactly the aggregation it draws.
 */

export interface ViewProps {
  query: UsageQuery
  reload: number
}

// ---- shared bits ----------------------------------------------------------------

export function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {actions}
      </div>
      {children}
    </section>
  )
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: Array<{ id: T; label: string }>
  onChange: (v: T) => void
}) {
  return (
    <div className="inline-flex rounded-md border p-0.5">
      {options.map((o) => (
        <button
          type="button"
          key={o.id}
          onClick={() => onChange(o.id)}
          className={cn(
            'rounded px-2 py-0.5 text-[11px]',
            value === o.id ? 'bg-accent font-medium' : 'text-muted-foreground hover:text-foreground',
          )}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

function Pending({ state, children }: { state: SummaryState; children: (groups: UsageGroup[]) => ReactNode }) {
  if (state.error) return <p className="text-xs text-destructive">{state.error}</p>
  if (!state.data) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
      </div>
    )
  }
  return <div className={cn('flex flex-col gap-3', state.loading && 'opacity-60')}>{children(state.data.groups)}</div>
}

function kindColumns<T extends UsageGroup>(): UsageColumn<T>[] {
  const cols: UsageColumn<T>[] = STACKABLE_KINDS.map((kind: TokenKind) => ({
    id: kind,
    label: TOKEN_KIND_LABEL[kind],
    value: (r: T) => r[kind],
    render: (r: T) => fmtFull(r[kind]),
    align: 'right' as const,
  }))
  cols.push({
    id: 'reasoning_output_token_count',
    label: 'of which reasoning',
    value: (r) => r.reasoning_output_token_count,
    render: (r) => fmtFull(r.reasoning_output_token_count),
    align: 'right',
    className: 'text-muted-foreground',
  })
  return cols
}

function countColumns<T extends UsageGroup>(): UsageColumn<T>[] {
  return [
    { id: 'records', label: 'Records', value: (r) => r.records, render: (r) => fmtFull(r.records), align: 'right' },
    {
      id: 'model_calls',
      label: 'Calls',
      value: (r) => r.model_calls,
      render: (r) => fmtFull(r.model_calls),
      align: 'right',
    },
  ]
}

function perCallColumn<T extends UsageGroup>(): UsageColumn<T> {
  return {
    id: 'per_call',
    label: 'Tokens / call',
    value: (r) => tokensPerCall(r),
    render: (r) => fmtFull(tokensPerCall(r)),
    align: 'right',
  }
}

function formatDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`)
  return Number.isNaN(d.getTime())
    ? day
    : new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(d)
}

type Stack = 'kind' | 'model' | 'lane'
const STACK_OPTIONS: Array<{ id: Stack; label: string }> = [
  { id: 'kind', label: 'Token kind' },
  { id: 'model', label: 'Model' },
  { id: 'lane', label: 'Lane' },
]

// ---- Overview -------------------------------------------------------------------

export function OverviewView({ query, reload }: ViewProps) {
  const days = useUsageSummary(query, 'day', undefined, reload)
  const models = useUsageSummary(query, 'model', undefined, reload)
  const targets = useUsageSummary(query, 'run_target', undefined, reload)
  const keys = enumerateDays(query.from, query.to)
  return (
    <div className="flex flex-col gap-6">
      <Section title="Daily tokens by kind">
        <Pending state={days}>
          {(groups) => <KindStackedChart keys={keys} groups={groups} variant="area" formatKey={formatDay} />}
        </Pending>
      </Section>
      <div className="grid gap-6 xl:grid-cols-2">
        <Section title="Top models">
          <Pending state={models}>{(groups) => <KindRankingChart groups={groups} limit={5} />}</Pending>
        </Section>
        <Section title="Top run targets">
          <Pending state={targets}>
            {(groups) => <KindRankingChart groups={groups.filter((g) => g.key !== UNKNOWN_KEY)} limit={5} />}
          </Pending>
        </Section>
      </div>
    </div>
  )
}

// ---- Daily ----------------------------------------------------------------------

export function DailyView({ query, reload }: ViewProps) {
  const [stack, setStack] = useState<Stack>('kind')
  const state = useUsageSummary(query, 'day', stack === 'kind' ? undefined : stack, reload)
  const keys = enumerateDays(query.from, query.to)
  const columns: UsageColumn<UsageGroup>[] = [
    { id: 'key', label: 'Day', value: (r) => r.key },
    ...countColumns(),
    ...kindColumns(),
    perCallColumn(),
  ]
  return (
    <div className="flex flex-col gap-6">
      <Section title="Tokens per day" actions={<Segmented value={stack} options={STACK_OPTIONS} onChange={setStack} />}>
        <Pending state={state}>
          {(groups) =>
            stack === 'kind' ? (
              <KindStackedChart keys={keys} groups={groups} formatKey={formatDay} />
            ) : (
              <SeriesStackedChart keys={keys} groups={groups} formatKey={formatDay} />
            )
          }
        </Pending>
      </Section>
      <Section title="By day">
        <Pending state={state}>
          {(groups) => (
            <UsageTable rows={groups} columns={columns} rowKey={(r) => r.key} initialSort={{ id: 'key', desc: true }} />
          )}
        </Pending>
      </Section>
    </div>
  )
}

// ---- Monthly --------------------------------------------------------------------

type MonthRow = UsageGroup & { change?: number }

export function MonthlyView({ query, reload }: ViewProps) {
  const [stack, setStack] = useState<Stack>('kind')
  const state = useUsageSummary(query, 'month', stack === 'kind' ? undefined : stack, reload)
  const keys = enumerateMonths(query.from, query.to)
  const columns: UsageColumn<MonthRow>[] = [
    { id: 'key', label: 'Month', value: (r) => r.key },
    ...countColumns<MonthRow>(),
    ...kindColumns<MonthRow>(),
    {
      id: 'change',
      label: 'vs previous month',
      value: (r) => r.change,
      render: (r) => (r.change === undefined ? '--' : `${r.change >= 0 ? '+' : ''}${fmtPct(r.change)}`),
      align: 'right',
    },
  ]
  const withChange = (groups: UsageGroup[]): MonthRow[] => {
    const byKey = new Map(groups.map((g) => [g.key, g]))
    return groups.map((g) => {
      const idx = keys.indexOf(g.key)
      const prev = idx > 0 ? byKey.get(keys[idx - 1]) : undefined
      const base = prev ? volume(prev) : 0
      return { ...g, change: base ? (volume(g) - base) / base : undefined }
    })
  }
  return (
    <div className="flex flex-col gap-6">
      <Section
        title="Tokens per month"
        actions={<Segmented value={stack} options={STACK_OPTIONS} onChange={setStack} />}>
        <Pending state={state}>
          {(groups) =>
            stack === 'kind' ? (
              <KindStackedChart keys={keys} groups={groups} />
            ) : (
              <SeriesStackedChart keys={keys} groups={groups} />
            )
          }
        </Pending>
      </Section>
      <Section title="By month">
        <Pending state={state}>
          {(groups) => (
            <UsageTable
              rows={withChange(groups)}
              columns={columns}
              rowKey={(r) => r.key}
              initialSort={{ id: 'key', desc: true }}
            />
          )}
        </Pending>
      </Section>
    </div>
  )
}

// ---- By type --------------------------------------------------------------------

type TypeAxis = Extract<GroupKey, 'lane' | 'run_target' | 'agent' | 'node' | 'kind'>
const TYPE_OPTIONS: Array<{ id: TypeAxis; label: string }> = [
  { id: 'lane', label: 'Lane' },
  { id: 'run_target', label: 'Run target' },
  { id: 'agent', label: 'Workflow agent' },
  { id: 'node', label: 'Workflow node' },
  { id: 'kind', label: 'Record kind' },
]

const TYPE_HINT: Record<TypeAxis, string> = {
  lane: 'Where the model was called: chat (prompt / harness), workflow, workflow job, Teams, OpenAI-compatible API, background helpers.',
  run_target: 'The Harness agent or Declarative Workflow that ran. "(unknown)" = plain Prompt chats and helpers.',
  agent: 'The Prompt agent a workflow node invoked. Workflow records only.',
  node: 'Workflow step, keyed "<workflow> / <step id>". Workflow records only.',
  kind: 'turn = a chat turn, helper = a background pass (titles, memory, ...), node = a workflow step.',
}

export function ByTypeView({ query, reload }: ViewProps) {
  const [axis, setAxis] = useState<TypeAxis>('lane')
  const state = useUsageSummary(query, axis, undefined, reload)
  const columns: UsageColumn<UsageGroup>[] = [
    { id: 'key', label: TYPE_OPTIONS.find((o) => o.id === axis)?.label ?? 'Key', value: (r) => r.key },
    ...countColumns(),
    ...kindColumns(),
    perCallColumn(),
    {
      id: 'last',
      label: 'Last used',
      value: (r) => r.last_ts,
      render: (r) => fmtInstant(r.last_ts, query.tz),
    },
  ]
  return (
    <div className="flex flex-col gap-6">
      <Section title="Ranking" actions={<Segmented value={axis} options={TYPE_OPTIONS} onChange={setAxis} />}>
        <p className="text-[11px] text-muted-foreground">{TYPE_HINT[axis]}</p>
        <Pending state={state}>{(groups) => <KindRankingChart groups={groups} limit={12} />}</Pending>
      </Section>
      <Section title="Detail">
        <Pending state={state}>
          {(groups) => (
            <UsageTable
              rows={groups}
              columns={columns}
              rowKey={(r) => r.key}
              initialSort={{ id: 'model_calls', desc: true }}
            />
          )}
        </Pending>
      </Section>
    </div>
  )
}

// ---- By model -------------------------------------------------------------------

type ModelRow = UsageGroup & { providers: string; errors: number; interrupted: number }

export function ByModelView({ query, reload }: ViewProps) {
  const byProvider = useUsageSummary(query, 'model', 'provider', reload)
  const byOutcome = useUsageSummary(query, 'model', 'outcome', reload)
  const outcomeOf = new Map((byOutcome.data?.groups ?? []).map((g) => [g.key, g.series ?? []]))
  const rows = (groups: UsageGroup[]): ModelRow[] =>
    groups.map((g) => {
      const outcomes = outcomeOf.get(g.key) ?? []
      const count = (k: string) => outcomes.find((o) => o.key === k)?.records ?? 0
      return {
        ...g,
        providers: (g.series ?? []).map((s) => s.key).join(', '),
        errors: count('error'),
        interrupted: count('interrupted'),
      }
    })
  const columns: UsageColumn<ModelRow>[] = [
    { id: 'key', label: 'Model', value: (r) => r.key, className: 'font-medium' },
    { id: 'providers', label: 'Provider', value: (r) => r.providers },
    ...countColumns<ModelRow>(),
    ...kindColumns<ModelRow>(),
    {
      id: 'hit',
      label: 'Cache hit',
      value: (r) => cacheHitRate(r),
      render: (r) => (
        <span title={r.cache_read_input_token_count === undefined ? 'Not reported by the provider' : undefined}>
          {fmtPct(cacheHitRate(r))}
        </span>
      ),
      align: 'right',
    },
    perCallColumn<ModelRow>(),
    {
      id: 'fail',
      label: 'Error / interrupted',
      value: (r) => (r.records ? (r.errors + r.interrupted) / r.records : undefined),
      render: (r) =>
        `${fmtPct(r.records ? (r.errors + r.interrupted) / r.records : undefined)} (${r.errors}/${r.interrupted})`,
      align: 'right',
    },
  ]
  return (
    <div className="flex flex-col gap-6">
      <Section title="Tokens by model and kind">
        <Pending state={byProvider}>{(groups) => <KindRankingChart groups={groups} limit={12} />}</Pending>
      </Section>
      <Section title="Models">
        <Pending state={byProvider}>
          {(groups) => (
            <UsageTable
              rows={rows(groups)}
              columns={columns}
              rowKey={(r) => r.key}
              initialSort={{ id: 'model_calls', desc: true }}
            />
          )}
        </Pending>
        <p className="text-[11px] text-muted-foreground">
          "--" means the provider did not report that figure; it is not zero. Charts stack only the reported input
          components and output; reasoning is part of output and is never stacked on top of it.
        </p>
      </Section>
    </div>
  )
}
