import { useEffect, useState } from 'react'
import { fetchUsageSummary, type GroupKey, type UsageQuery, type UsageSummary } from '@/lib/usageApi'

export interface SummaryState {
  data: UsageSummary | null
  loading: boolean
  error: string | null
}

/**
 * Fetch one CTR-0201 summary for the current query. ``reload`` is a counter the
 * dashboard bumps on Refresh. A stale response (query changed meanwhile) is aborted.
 */
export function useUsageSummary(
  q: UsageQuery | null,
  groupBy: GroupKey,
  series: GroupKey | undefined,
  reload: number,
): SummaryState {
  const [state, setState] = useState<SummaryState>({ data: null, loading: false, error: null })
  const from = q?.from
  const to = q?.to
  const tz = q?.tz
  const lane = q?.lane

  // biome-ignore lint/correctness/useExhaustiveDependencies: reload is an explicit refetch trigger
  useEffect(() => {
    if (!from || !to || !tz) return
    const controller = new AbortController()
    setState((s) => ({ ...s, loading: true, error: null }))
    fetchUsageSummary({ from, to, tz, lane }, groupBy, series, controller.signal)
      .then((data) => setState({ data, loading: false, error: null }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setState({ data: null, loading: false, error: err instanceof Error ? err.message : String(err) })
      })
    return () => controller.abort()
  }, [from, to, tz, lane, groupBy, series, reload])

  return state
}
