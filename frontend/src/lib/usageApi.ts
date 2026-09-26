/**
 * Usage Statistics API client and presentation rules (CTR-0201 / CTR-0015 lookup,
 * CTR-0215, PRP-0173, UDR-0155).
 *
 * The SPA's only knowledge of the usage API lives here. Aggregation -- including the
 * calendar zone -- is done by the backend (UDR-0155 D1); this module never re-buckets
 * records. It only derives ratios from sums the backend returned, and only when every
 * term of the ratio was reported (UDR-0155 D4). There is no price anywhere.
 */

export const TOKEN_KINDS = [
  'uncached_input_token_count',
  'cache_read_input_token_count',
  'cache_creation_input_token_count',
  'output_token_count',
  'reasoning_output_token_count',
] as const

export type TokenKind = (typeof TOKEN_KINDS)[number]

export const INPUT_KINDS = [
  'uncached_input_token_count',
  'cache_read_input_token_count',
  'cache_creation_input_token_count',
] as const satisfies readonly TokenKind[]

/**
 * The kinds that may be STACKED in a chart. Reasoning is deliberately absent: where a
 * provider reports it, it is part of output, so stacking it would count it twice
 * (UDR-0155 D4). It is shown as an "of which" value instead.
 */
export const STACKABLE_KINDS = [...INPUT_KINDS, 'output_token_count'] as const satisfies readonly TokenKind[]

export const TOKEN_KIND_LABEL: Record<TokenKind, string> = {
  uncached_input_token_count: 'Uncached input',
  cache_read_input_token_count: 'Cache read',
  cache_creation_input_token_count: 'Cache write',
  output_token_count: 'Output',
  reasoning_output_token_count: 'Reasoning (of output)',
}

/** Fixed colours per kind, in every view (index.css --chart-1..5). */
export const TOKEN_KIND_COLOR: Record<TokenKind, string> = {
  uncached_input_token_count: 'hsl(var(--chart-1))',
  cache_read_input_token_count: 'hsl(var(--chart-2))',
  cache_creation_input_token_count: 'hsl(var(--chart-3))',
  output_token_count: 'hsl(var(--chart-4))',
  reasoning_output_token_count: 'hsl(var(--chart-5))',
}

export const GROUP_KEYS = [
  'day',
  'month',
  'chat',
  'model',
  'lane',
  'run_target',
  'node',
  'agent',
  'provider',
  'outcome',
  'kind',
] as const

export type GroupKey = (typeof GROUP_KEYS)[number]

export const LANES = [
  'spa-prompt',
  'spa-harness',
  'workflow',
  'workflow-job',
  'teams',
  'openai-api',
  // Agent runs started from a Live voice conversation (PRP-0188, UDR-0170 D10).
  'live',
  'helper',
] as const

/** Keys the backend uses for rows that cannot name a chat (CTR-0201). */
export const TEMPORARY_KEY = '(temporary)'
export const UNKNOWN_KEY = '(unknown)'

export type UsageBucket = {
  records: number
  model_calls: number
  first_ts?: string
  last_ts?: string
} & Partial<Record<TokenKind, number>>

export type UsageGroup = UsageBucket & { key: string; series?: Array<UsageBucket & { key: string }> }

export interface UsageSummary {
  from: string
  to: string
  tz: string
  group_by: GroupKey
  series?: GroupKey
  lane: string | null
  groups: UsageGroup[]
  totals: UsageBucket
  skipped_lines: number
  coverage: string
}

export interface UsageQuery {
  from: string
  to: string
  tz: string
  lane?: string | null
}

function query(params: Record<string, string | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value) search.set(key, value)
  }
  return search.toString()
}

async function errorDetail(res: Response): Promise<string> {
  const body = await res.json().catch(() => ({}))
  return typeof body?.detail === 'string' && body.detail ? body.detail : `Request failed (${res.status})`
}

export async function fetchUsageSummary(
  q: UsageQuery,
  groupBy: GroupKey,
  series?: GroupKey,
  signal?: AbortSignal,
): Promise<UsageSummary> {
  const qs = query({ from: q.from, to: q.to, tz: q.tz, lane: q.lane, group_by: groupBy, series })
  const res = await fetch(`/api/usage/summary?${qs}`, { signal })
  if (!res.ok) throw new Error(await errorDetail(res))
  return (await res.json()) as UsageSummary
}

export function usageExportUrl(q: UsageQuery): string {
  return `/api/usage/export?${query({ from: q.from, to: q.to, tz: q.tz, lane: q.lane, format: 'csv' })}`
}

export function usageExportFilename(q: UsageQuery): string {
  const slug = q.tz.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'UTC'
  return `token-usage_${q.from}_${q.to}_${slug}.csv`
}

export interface SessionLookup {
  found: Record<string, { title: string; updated_at: string; folder_id: string | null }>
  missing: string[]
}

/** Server-side cap of GET /api/sessions/lookup (CTR-0015). */
export const LOOKUP_MAX_IDS = 200

export async function lookupSessions(ids: string[], signal?: AbortSignal): Promise<SessionLookup> {
  if (ids.length === 0) return { found: {}, missing: [] }
  const res = await fetch(`/api/sessions/lookup?${query({ ids: ids.slice(0, LOOKUP_MAX_IDS).join(',') })}`, {
    signal,
  })
  if (!res.ok) throw new Error(await errorDetail(res))
  return (await res.json()) as SessionLookup
}

// ---- derived values (UDR-0155 D4) ------------------------------------------------

/** Sum of the reported input components, or undefined when none was reported. */
export function inputSum(b: Partial<Record<TokenKind, number>>): number | undefined {
  let total: number | undefined
  for (const kind of INPUT_KINDS) {
    const v = b[kind]
    if (typeof v === 'number') total = (total ?? 0) + v
  }
  return total
}

/** Input components + output: the ranking measure. Absent terms add nothing. */
export function volume(b: Partial<Record<TokenKind, number>>): number {
  return (inputSum(b) ?? 0) + (b.output_token_count ?? 0)
}

/** cache_read / all input -- only when cache reads were reported at all. */
export function cacheHitRate(b: Partial<Record<TokenKind, number>>): number | undefined {
  if (typeof b.cache_read_input_token_count !== 'number') return undefined
  const input = inputSum(b)
  if (!input) return undefined
  return b.cache_read_input_token_count / input
}

export function tokensPerCall(b: UsageBucket): number | undefined {
  if (!b.model_calls) return undefined
  return volume(b) / b.model_calls
}

// ---- formatting -----------------------------------------------------------------

const compactFmt = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 })
const fullFmt = new Intl.NumberFormat()
const pctFmt = new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 1 })

export const NOT_REPORTED = '--'

export function fmtCompact(v: number | undefined): string {
  return typeof v === 'number' ? compactFmt.format(v) : NOT_REPORTED
}

export function fmtFull(v: number | undefined): string {
  return typeof v === 'number' ? fullFmt.format(Math.round(v)) : NOT_REPORTED
}

export function fmtPct(v: number | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? pctFmt.format(v) : NOT_REPORTED
}

export function fmtInstant(iso: string | undefined, tz: string): string {
  if (!iso) return NOT_REPORTED
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return NOT_REPORTED
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeStyle: 'short', timeZone: tz }).format(d)
}

// ---- time zone and ranges (UDR-0155 D2) -----------------------------------------

const TZ_STORAGE_KEY = 'chatwalaau.usage.tz'

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

export function supportedTimeZones(): string[] {
  let zones: string[] = []
  try {
    zones = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone') ?? []
  } catch {
    zones = []
  }
  const all = new Set(['UTC', ...zones, browserTimeZone()])
  return [...all].sort((a, b) => (a === 'UTC' ? -1 : b === 'UTC' ? 1 : a.localeCompare(b)))
}

export function isValidTimeZone(tz: string): boolean {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: tz })
    return true
  } catch {
    return false
  }
}

/** The viewer's remembered zone, else the browser's. Storage failures are harmless. */
export function loadTimeZone(): string {
  try {
    const saved = window.localStorage.getItem(TZ_STORAGE_KEY)
    if (saved && isValidTimeZone(saved)) return saved
  } catch {
    // private mode / blocked storage: fall through to the browser zone
  }
  return browserTimeZone()
}

export function saveTimeZone(tz: string): void {
  try {
    if (tz === browserTimeZone()) window.localStorage.removeItem(TZ_STORAGE_KEY)
    else window.localStorage.setItem(TZ_STORAGE_KEY, tz)
  } catch {
    // a convenience only
  }
}

/** Today's calendar date (YYYY-MM-DD) in ``tz``. */
export function todayIn(tz: string): string {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(
    new Date(),
  )
}

/** Calendar arithmetic on YYYY-MM-DD strings (zone-free by construction). */
export function addDays(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

export function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000)
}

/** The UI's maximum span (PRP-0173 section 16 Q3). The API itself is unbounded. */
export const MAX_RANGE_DAYS = 366

export type RangePreset = '7d' | '30d' | '90d' | 'this-month' | 'last-month' | '12m' | 'custom'

export const RANGE_PRESETS: Array<{ id: RangePreset; label: string }> = [
  { id: '7d', label: '7 days' },
  { id: '30d', label: '30 days' },
  { id: '90d', label: '90 days' },
  { id: 'this-month', label: 'This month' },
  { id: 'last-month', label: 'Last month' },
  { id: '12m', label: '12 months' },
  { id: 'custom', label: 'Custom' },
]

export function presetRange(preset: Exclude<RangePreset, 'custom'>, tz: string): { from: string; to: string } {
  const today = todayIn(tz)
  const monthStart = `${today.slice(0, 7)}-01`
  switch (preset) {
    case '7d':
      return { from: addDays(today, -6), to: today }
    case '30d':
      return { from: addDays(today, -29), to: today }
    case '90d':
      return { from: addDays(today, -89), to: today }
    case 'this-month':
      return { from: monthStart, to: today }
    case 'last-month': {
      const lastDay = addDays(monthStart, -1)
      return { from: `${lastDay.slice(0, 7)}-01`, to: lastDay }
    }
    case '12m': {
      // The first day of the month eleven months ago through today: twelve calendar months.
      const d = new Date(`${monthStart}T00:00:00Z`)
      d.setUTCMonth(d.getUTCMonth() - 11)
      return { from: d.toISOString().slice(0, 10), to: today }
    }
  }
}

export function enumerateDays(from: string, to: string): string[] {
  const days: string[] = []
  for (let d = from; d <= to && days.length <= MAX_RANGE_DAYS; d = addDays(d, 1)) days.push(d)
  return days
}

export function enumerateMonths(from: string, to: string): string[] {
  const months: string[] = []
  let y = Number(from.slice(0, 4))
  let m = Number(from.slice(5, 7))
  const end = to.slice(0, 7)
  for (;;) {
    const key = `${y}-${String(m).padStart(2, '0')}`
    if (key > end || months.length > 24) break
    months.push(key)
    m += 1
    if (m > 12) {
      y += 1
      m = 1
    }
  }
  return months
}
