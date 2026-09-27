import { formatComputerUseTimings, parseComputerUseResult } from '@/lib/computerUse'
import { cn } from '@/lib/utils'

/**
 * Compact result of a computer_* call (CTR-0235, PRP-0189 Section 2.11).
 *
 * Status chip, executed steps, expectations, the window and the timings on one line.
 * No screenshot: screenshots are never persisted (UDR-0171 D8), and the raw JSON stays
 * in the tool call's expandable detail.
 */
export function ComputerUseResultCard({ result }: { result: string | undefined }) {
  const parsed = parseComputerUseResult(result)
  if (!parsed) return null
  const ok = parsed.status === 'ok'
  const timings = formatComputerUseTimings(parsed.timings)
  const expectText =
    parsed.expect && parsed.expect.length > 0
      ? `expect ${parsed.expect.filter(Boolean).length}/${parsed.expect.length}`
      : null
  const details = [
    typeof parsed.executed === 'number' ? `${parsed.executed} steps` : null,
    expectText,
    parsed.screen ?? (parsed.window ? parsed.window : null),
    timings,
  ].filter(Boolean)
  return (
    <div className="ml-5 flex min-w-0 flex-wrap items-center gap-1.5 text-[0.7rem] text-muted-foreground">
      <span
        className={cn(
          'rounded px-1.5 py-px font-mono',
          ok
            ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
            : 'bg-amber-500/10 text-amber-700 dark:text-amber-400',
        )}>
        {parsed.status}
        {parsed.by ? ` (${parsed.by})` : ''}
      </span>
      {details.map((d) => (
        <span key={d as string} className="truncate">
          {d}
        </span>
      ))}
      {!ok && parsed.reason && (
        <span className="truncate" title={parsed.reason}>
          {parsed.reason}
        </span>
      )}
    </div>
  )
}
