import {
  cacheHitRate,
  fmtCompact,
  fmtPct,
  inputSum,
  TOKEN_KIND_COLOR,
  TOKEN_KIND_LABEL,
  type TokenKind,
  tokensPerCall,
  type UsageBucket,
  type UsageGroup,
} from '@/lib/usageApi'

/**
 * KPI row (CTR-0215). There is deliberately no single "total tokens" figure
 * (UDR-0135 D6): input is shown as the sum of its reported components, output apart.
 */

function Kind({ kind, value }: { kind: TokenKind; value: number | undefined }) {
  return (
    <div className="flex items-center justify-between gap-2 text-[11px]">
      <span className="flex items-center gap-1 text-muted-foreground">
        <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: TOKEN_KIND_COLOR[kind] }} />
        {TOKEN_KIND_LABEL[kind]}
      </span>
      <span
        className="font-mono tabular-nums"
        title={value === undefined ? 'Not reported by the provider' : value.toLocaleString()}>
        {fmtCompact(value)}
      </span>
    </div>
  )
}

function Card({
  title,
  value,
  caption,
  children,
}: {
  title: string
  value: string
  caption?: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg border p-3">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{title}</span>
      <span className="font-mono text-xl font-semibold tabular-nums">{value}</span>
      {caption && <span className="text-[10px] text-muted-foreground">{caption}</span>}
      {children}
    </div>
  )
}

export function UsageKpis({
  totals,
  models,
  outcomes,
}: {
  totals: UsageBucket
  /** group_by=model rows: the cache hit rate is computed only over rows that report cache reads. */
  models: UsageGroup[]
  /** group_by=outcome rows. */
  outcomes: UsageGroup[]
}) {
  const reporting = models.filter((m) => typeof m.cache_read_input_token_count === 'number')
  const cacheBasis = reporting.reduce<Partial<Record<TokenKind, number>>>((acc, m) => {
    for (const k of [
      'uncached_input_token_count',
      'cache_read_input_token_count',
      'cache_creation_input_token_count',
    ] as const) {
      const v = m[k]
      if (typeof v === 'number') acc[k] = (acc[k] ?? 0) + v
    }
    return acc
  }, {})
  const hit = reporting.length ? cacheHitRate(cacheBasis) : undefined

  const outcomeCount = (key: string) => outcomes.find((o) => o.key === key)?.records ?? 0
  const failed = outcomeCount('error') + outcomeCount('interrupted')
  const failShare = totals.records ? failed / totals.records : undefined

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
      <Card title="Input" value={fmtCompact(inputSum(totals))} caption="sum of reported components">
        <Kind kind="uncached_input_token_count" value={totals.uncached_input_token_count} />
        <Kind kind="cache_read_input_token_count" value={totals.cache_read_input_token_count} />
        <Kind kind="cache_creation_input_token_count" value={totals.cache_creation_input_token_count} />
      </Card>
      <Card title="Output" value={fmtCompact(totals.output_token_count)}>
        <Kind kind="reasoning_output_token_count" value={totals.reasoning_output_token_count} />
      </Card>
      <Card
        title="Model calls"
        value={fmtCompact(totals.model_calls)}
        caption={`${fmtCompact(tokensPerCall(totals))} tokens / call · ${totals.records.toLocaleString()} records`}
      />
      <Card
        title="Cache hit rate"
        value={fmtPct(hit)}
        caption={
          reporting.length === models.length
            ? 'cache read / all input'
            : `over ${reporting.length} of ${models.length} models that report cache reads`
        }
      />
      <Card
        title="Error / interrupted"
        value={fmtPct(failShare)}
        caption={`${outcomeCount('error')} error · ${outcomeCount('interrupted')} interrupted`}
      />
    </div>
  )
}
