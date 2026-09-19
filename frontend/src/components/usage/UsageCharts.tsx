import { Area, AreaChart, Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'
import {
  type ChartConfig,
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
} from '@/components/ui/chart'
import {
  fmtCompact,
  fmtFull,
  STACKABLE_KINDS,
  TOKEN_KIND_COLOR,
  TOKEN_KIND_LABEL,
  type UsageGroup,
  volume,
} from '@/lib/usageApi'

/**
 * Usage Dashboard charts (CTR-0215, PRP-0173).
 *
 * Presentation rules (UDR-0155 D4) live here once:
 * - token kinds stack in a fixed order with fixed colours, and reasoning is never a
 *   stacked segment (it is part of output);
 * - a value the provider did not report is left OUT of the datum, so recharts draws no
 *   segment for it -- it is never turned into 0.
 */

const KIND_CONFIG: ChartConfig = Object.fromEntries(
  STACKABLE_KINDS.map((kind) => [kind, { label: TOKEN_KIND_LABEL[kind], color: TOKEN_KIND_COLOR[kind] }]),
)

const SERIES_PALETTE = [
  'hsl(var(--chart-1))',
  'hsl(var(--chart-4))',
  'hsl(var(--chart-2))',
  'hsl(var(--chart-3))',
  'hsl(var(--chart-5))',
  'hsl(215 16% 47%)',
]

/** Top-N series plus "Other"; beyond that a stacked chart stops being readable. */
const MAX_SERIES = 5
const OTHER_ID = 's_other'

type Datum = Record<string, string | number>

function tooltipFormatter(value: unknown, name: unknown, config: ChartConfig) {
  const label = config[String(name)]?.label ?? String(name)
  return (
    <div className="flex w-full items-center justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono tabular-nums">{fmtFull(typeof value === 'number' ? value : undefined)}</span>
    </div>
  )
}

/**
 * Only the kinds the bucket reported -- absent stays absent (UDR-0135 D7). A position
 * with NO records at all (an idle day) is a measured zero, not a missing figure: nothing
 * ran, so nothing was consumed.
 */
function kindDatum(key: string, group: UsageGroup | undefined): Datum {
  const datum: Datum = { key }
  if (!group) {
    for (const kind of STACKABLE_KINDS) datum[kind] = 0
    return datum
  }
  for (const kind of STACKABLE_KINDS) {
    const v = group[kind]
    if (typeof v === 'number') datum[kind] = v
  }
  return datum
}

export function KindStackedChart({
  keys,
  groups,
  variant = 'bar',
  formatKey = (k) => k,
  height = 280,
}: {
  /** Every x position, including empty days, in order. */
  keys: string[]
  groups: UsageGroup[]
  variant?: 'bar' | 'area'
  formatKey?: (key: string) => string
  height?: number
}) {
  const byKey = new Map(groups.map((g) => [g.key, g]))
  const data = keys.map((k) => kindDatum(k, byKey.get(k)))
  const common = (
    <>
      <CartesianGrid vertical={false} />
      <XAxis dataKey="key" tickLine={false} axisLine={false} tickMargin={8} minTickGap={16} tickFormatter={formatKey} />
      <YAxis tickLine={false} axisLine={false} width={48} tickFormatter={(v: number) => fmtCompact(v)} />
      <ChartTooltip
        content={
          <ChartTooltipContent
            labelFormatter={(label) => formatKey(String(label))}
            formatter={(value, name) => tooltipFormatter(value, name, KIND_CONFIG)}
          />
        }
      />
      <ChartLegend content={<ChartLegendContent />} />
    </>
  )
  return (
    <ChartContainer config={KIND_CONFIG} className="w-full" style={{ height }}>
      {variant === 'area' ? (
        <AreaChart data={data} margin={{ left: 4, right: 12 }}>
          {common}
          {STACKABLE_KINDS.map((kind) => (
            <Area
              key={kind}
              dataKey={kind}
              type="linear"
              stackId="tokens"
              stroke={`var(--color-${kind})`}
              fill={`var(--color-${kind})`}
              fillOpacity={0.35}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      ) : (
        <BarChart data={data} margin={{ left: 4, right: 12 }}>
          {common}
          {STACKABLE_KINDS.map((kind) => (
            <Bar key={kind} dataKey={kind} stackId="tokens" fill={`var(--color-${kind})`} isAnimationActive={false} />
          ))}
        </BarChart>
      )}
    </ChartContainer>
  )
}

/**
 * One bar per x key, stacked by a second axis (``series`` from the API). The measure is
 * input components + output of each series row. Series keys are arbitrary strings
 * (model names contain dots), so they are mapped to safe ids for the CSS variables.
 */
export function SeriesStackedChart({
  keys,
  groups,
  formatKey = (k) => k,
  height = 280,
}: {
  keys: string[]
  groups: UsageGroup[]
  formatKey?: (key: string) => string
  height?: number
}) {
  const totals = new Map<string, number>()
  for (const g of groups) {
    for (const row of g.series ?? []) totals.set(row.key, (totals.get(row.key) ?? 0) + volume(row))
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k)
  const top = ranked.slice(0, MAX_SERIES)
  const hasOther = ranked.length > top.length
  const idOf = new Map(top.map((k, i) => [k, `s_${i}`]))

  const config: ChartConfig = Object.fromEntries(
    top.map((k, i) => [`s_${i}`, { label: k, color: SERIES_PALETTE[i % SERIES_PALETTE.length] }]),
  )
  if (hasOther) config[OTHER_ID] = { label: 'Other', color: SERIES_PALETTE[SERIES_PALETTE.length - 1] }

  const byKey = new Map(groups.map((g) => [g.key, g]))
  const data = keys.map((k) => {
    const datum: Datum = { key: k }
    for (const row of byKey.get(k)?.series ?? []) {
      const id = idOf.get(row.key) ?? OTHER_ID
      datum[id] = (Number(datum[id]) || 0) + volume(row)
    }
    return datum
  })
  const ids = Object.keys(config)

  return (
    <ChartContainer config={config} className="w-full" style={{ height }}>
      <BarChart data={data} margin={{ left: 4, right: 12 }}>
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="key"
          tickLine={false}
          axisLine={false}
          tickMargin={8}
          minTickGap={16}
          tickFormatter={formatKey}
        />
        <YAxis tickLine={false} axisLine={false} width={48} tickFormatter={(v: number) => fmtCompact(v)} />
        <ChartTooltip
          content={
            <ChartTooltipContent
              labelFormatter={(label) => formatKey(String(label))}
              formatter={(value, name) => tooltipFormatter(value, name, config)}
            />
          }
        />
        <ChartLegend content={<ChartLegendContent />} />
        {ids.map((id) => (
          <Bar key={id} dataKey={id} stackId="series" fill={`var(--color-${id})`} isAnimationActive={false} />
        ))}
      </BarChart>
    </ChartContainer>
  )
}

/** Horizontal ranking, each bar stacked by token kind. */
export function KindRankingChart({ groups, limit = 10 }: { groups: UsageGroup[]; limit?: number }) {
  const ranked = [...groups].sort((a, b) => volume(b) - volume(a)).slice(0, limit)
  const data = ranked.map((g) => kindDatum(g.key, g))
  const height = Math.max(120, ranked.length * 32 + 48)
  return (
    <ChartContainer config={KIND_CONFIG} className="w-full" style={{ height }}>
      <BarChart data={data} layout="vertical" margin={{ left: 4, right: 12 }}>
        <CartesianGrid horizontal={false} />
        <XAxis type="number" tickLine={false} axisLine={false} tickFormatter={(v: number) => fmtCompact(v)} />
        <YAxis
          type="category"
          dataKey="key"
          tickLine={false}
          axisLine={false}
          width={160}
          tickFormatter={(v: string) => (v.length > 24 ? `${v.slice(0, 23)}...` : v)}
        />
        <ChartTooltip
          content={<ChartTooltipContent formatter={(value, name) => tooltipFormatter(value, name, KIND_CONFIG)} />}
        />
        <ChartLegend content={<ChartLegendContent />} />
        {STACKABLE_KINDS.map((kind) => (
          <Bar key={kind} dataKey={kind} stackId="tokens" fill={`var(--color-${kind})`} isAnimationActive={false} />
        ))}
      </BarChart>
    </ChartContainer>
  )
}
