import { ExternalLink, Loader2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type { OpenChatRequest } from '@/components/usage/SessionsView'
import { Section, type ViewProps } from '@/components/usage/UsageViews'
import { lookupSessions } from '@/lib/usageApi'

/**
 * Live voice time (PRP-0188 step 3, UDR-0170 D10; CTR-0215 / CTR-0201).
 *
 * GPT-Live is metered in voice SECONDS, which the token ledger does not hold, so this
 * view reads its own stream (`GET /api/usage/voice`). Minutes per local day and per
 * chat; a session the service never reported seconds for is counted, never shown as
 * zero. Nothing is priced.
 */
interface VoiceBucket {
  key: string
  seconds: number
  sessions: number
  sessions_without_seconds: number
  delegations: number
  last_ts?: string
}

interface VoiceSummary {
  totals: Omit<VoiceBucket, 'key'>
  days: VoiceBucket[]
  chats: VoiceBucket[]
  skipped_lines: number
}

const TEMPORARY_KEY = '(temporary)'

export function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

export function VoiceUsageView({
  query,
  reload,
  onRequestOpen,
}: ViewProps & { onRequestOpen: (req: OpenChatRequest, markMissing: () => void) => void }) {
  const [data, setData] = useState<VoiceSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [titles, setTitles] = useState<Record<string, string | null>>({})

  // biome-ignore lint/correctness/useExhaustiveDependencies: reload is an explicit refetch trigger
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    const qs = new URLSearchParams({ from: query.from, to: query.to, tz: query.tz })
    fetch(`/api/usage/voice?${qs}`, { signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`Request failed (${res.status})`)
        return (await res.json()) as VoiceSummary
      })
      .then((body) => {
        setData(body)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : String(err))
        setLoading(false)
      })
    return () => controller.abort()
  }, [query.from, query.to, query.tz, reload])

  const chatIds = useMemo(() => (data?.chats ?? []).map((c) => c.key).filter((k) => k !== TEMPORARY_KEY), [data])
  const idsKey = chatIds.join(',')
  useEffect(() => {
    if (!idsKey) return
    const controller = new AbortController()
    lookupSessions(idsKey.split(','), controller.signal)
      .then((res) => {
        const next: Record<string, string | null> = {}
        for (const id of idsKey.split(',')) next[id] = res.found[id] ? res.found[id].title || '(untitled)' : null
        setTitles(next)
      })
      .catch(() => undefined)
    return () => controller.abort()
  }, [idsKey])

  if (error) return <p className="text-xs text-destructive">{error}</p>
  if (!data) return loading ? <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /> : null
  const t = data.totals
  if (t.sessions === 0) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
        No Live voice conversations in this range.
      </div>
    )
  }

  const tiles: Array<{ label: string; value: string; hint?: string }> = [
    { label: 'Voice time', value: fmtDuration(t.seconds), hint: 'Seconds the service reported for ended sessions' },
    { label: 'Live sessions', value: String(t.sessions) },
    { label: 'Delegated tasks', value: String(t.delegations), hint: 'Their tokens are under the lane "live"' },
  ]
  if (t.sessions_without_seconds > 0) {
    tiles.push({
      label: 'Sessions without seconds',
      value: String(t.sessions_without_seconds),
      hint: 'The service never reported their time; they are not counted as zero',
    })
  }
  const maxDay = Math.max(1, ...data.days.map((d) => d.seconds))

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {tiles.map((tile) => (
          <div key={tile.label} className="rounded-lg border p-3" title={tile.hint}>
            <div className="text-[11px] text-muted-foreground">{tile.label}</div>
            <div className="text-lg font-semibold tabular-nums">{tile.value}</div>
          </div>
        ))}
      </div>

      <Section title="Per day">
        <div className="flex flex-col gap-1">
          {data.days.map((d) => (
            <div key={d.key} className="flex items-center gap-2 text-xs">
              <span className="w-24 shrink-0 tabular-nums text-muted-foreground">{d.key}</span>
              <div className="h-3 flex-1 rounded bg-muted">
                <div className="h-3 rounded bg-cyan-500" style={{ width: `${(d.seconds / maxDay) * 100}%` }} />
              </div>
              <span className="w-20 shrink-0 text-right tabular-nums">{fmtDuration(d.seconds)}</span>
              <span className="w-16 shrink-0 text-right text-muted-foreground">{d.sessions} sess.</span>
            </div>
          ))}
        </div>
      </Section>

      <Section title={`Chats (${data.chats.length})`}>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Chat</th>
              <th className="py-1 text-right font-medium">Voice time</th>
              <th className="py-1 text-right font-medium">Sessions</th>
              <th className="py-1 text-right font-medium">Tasks</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {data.chats.map((c) => {
              const temporary = c.key === TEMPORARY_KEY
              const title = temporary ? 'Temporary chats' : titles[c.key]
              const exists = !temporary && typeof title === 'string'
              return (
                <tr key={c.key} className="border-t">
                  <td className="max-w-[320px] truncate py-1" title={c.key}>
                    {temporary ? title : title === undefined ? '...' : (title ?? 'Deleted or re-imported chat')}
                  </td>
                  <td className="py-1 text-right tabular-nums">{fmtDuration(c.seconds)}</td>
                  <td className="py-1 text-right tabular-nums">{c.sessions}</td>
                  <td className="py-1 text-right tabular-nums">{c.delegations}</td>
                  <td className="py-1 text-right">
                    {exists && (
                      <button
                        type="button"
                        className="inline-flex items-center text-muted-foreground hover:text-foreground"
                        onClick={() =>
                          onRequestOpen({ threadId: c.key, title: title ?? '(untitled)' }, () =>
                            setTitles((prev) => ({ ...prev, [c.key]: null })),
                          )
                        }
                        aria-label="Open this chat"
                        title="Open this chat">
                        <ExternalLink className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Section>

      {data.skipped_lines > 0 && (
        <p className="text-[11px] text-amber-700">{data.skipped_lines} unreadable voice record(s) were skipped.</p>
      )}
    </div>
  )
}
