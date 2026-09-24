import type { UsageInfo } from '@/types/chat'

/**
 * Context window occupancy (CTR-0041 v5, FEAT-0008, PRP-0186, UDR-0168 D5/D6).
 *
 * This module replaces the `ContextWindowIndicator` COMPONENT. Occupancy is no longer
 * a readout above the composer -- it is the composer's outline state -- so what the
 * chat panel needs is a level and a sentence, not a bar. A pure resolver also keeps
 * the thresholds testable without rendering anything.
 *
 * WHAT DID NOT CHANGE. The thresholds (80 / 95), the source field and the arithmetic
 * are exactly what the indicator computed since PRP-0157 (UDR-0135 D4). This release
 * changes the PRESENTATION of occupancy, not its measurement, and keeping the
 * computation byte-for-byte is what makes that claim true.
 *
 * WHAT DID CHANGE. `null` is returned for everything below the warning threshold,
 * including a chat that has not run a turn yet (D6). The old component resolved its
 * maximum as `usage?.max_context_tokens ?? maxContextTokens`, so a fresh chat fell
 * back to the model's window, computed 0 / 200K and rendered a permanent `0% (0 /
 * 200K)` -- in direct contradiction of CTR-0041's own "hidden when no usage data
 * available" rule, which the fallback defeated without amending. The fallback is gone:
 * `maxContextTokens` is now only a backstop for a MEASURED turn whose usage object
 * omits the window, never a substitute for having measured anything.
 */

/** Occupancy at or above this percentage is worth interrupting for. */
const WARNING_THRESHOLD = 80
/** Occupancy at or above this percentage is about to fail. */
const CRITICAL_THRESHOLD = 95

export type ContextLevel = 'warning' | 'critical'

export interface ContextOccupancy {
  level: ContextLevel
  /** Percentage, rounded, capped at 100. */
  percent: number
  /**
   * The non-colour equivalent (D5): a ring is invisible to a colour-blind operator
   * and silent to a screen reader, and this is a warning. Rendered into `title` and
   * referenced by `aria-describedby` on the composer.
   */
  description: string
}

function formatTokenCount(count: number): string {
  if (count < 1000) return String(count)
  return `${Math.round(count / 1000)}K`
}

/**
 * Resolve the composer's occupancy state, or `null` when there is nothing to say.
 *
 * `null` means BOTH "no measured turn yet" and "comfortably below the threshold", and
 * the composer renders identically in both cases. That collapse is deliberate: the
 * operator does not need to distinguish "0% used" from "40% used" before a send, and
 * a control that is present only when it has something to report cannot be misread as
 * broken when it is silent.
 */
export function resolveContextOccupancy(
  usage: UsageInfo | undefined,
  maxContextTokens: number | undefined,
): ContextOccupancy | null {
  if (!usage) return null

  const max = usage.max_context_tokens ?? maxContextTokens
  if (!max || max <= 0) return null

  // The provider-normalized scalar when present; the legacy `input + output` only for
  // messages persisted before v0.144.0 and for a provider reporting no input count.
  // That fallback UNDER-reports on Anthropic (its input count excludes the cached
  // prefix) and is kept only because old messages carry nothing better -- degrading to
  // the previous behaviour, never to a false zero.
  const input = usage.input_token_count ?? 0
  const output = usage.output_token_count ?? 0
  const consumed = usage.context_base_tokens ?? input + output
  // A usage object carrying NEITHER field is a workflow's token-less run-target label
  // (UDR-0152 D8), not a measurement of zero.
  if (usage.context_base_tokens === undefined && usage.input_token_count === undefined) return null

  const percent = Math.min(Math.round((consumed / max) * 100), 100)
  if (percent < WARNING_THRESHOLD) return null

  const estimated = usage.context_estimated ? ' (estimated)' : ''
  return {
    level: percent >= CRITICAL_THRESHOLD ? 'critical' : 'warning',
    percent,
    description: `Context ${percent}% full -- ${formatTokenCount(consumed)} of ${formatTokenCount(max)} tokens${estimated}. Start a new chat if answers get truncated.`,
  }
}
