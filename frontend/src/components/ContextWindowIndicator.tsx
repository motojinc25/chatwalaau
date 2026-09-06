import { cn } from '@/lib/utils'
import type { UsageInfo } from '@/types/chat'

interface ContextWindowIndicatorProps {
  usage: UsageInfo | undefined
  maxContextTokens: number | undefined
}

function formatTokenCount(count: number): string {
  if (count < 1000) return String(count)
  return `${Math.round(count / 1000)}K`
}

type WarningLevel = 'normal' | 'warning' | 'critical'

function getWarningLevel(rate: number): WarningLevel {
  if (rate >= 95) return 'critical'
  if (rate >= 80) return 'warning'
  return 'normal'
}

const barColors: Record<WarningLevel, string> = {
  normal: 'bg-muted-foreground/30',
  warning: 'bg-amber-500',
  critical: 'bg-red-500',
}

const textColors: Record<WarningLevel, string> = {
  normal: 'text-muted-foreground/60',
  warning: 'text-amber-500',
  critical: 'text-red-500',
}

export function ContextWindowIndicator({ usage, maxContextTokens }: ContextWindowIndicatorProps) {
  const max = usage?.max_context_tokens ?? maxContextTokens
  if (!max || max <= 0) return null

  // Context occupancy is the backend-computed, provider-normalized scalar
  // (PRP-0157, UDR-0135 D4). The legacy `input + output` is kept as the fallback
  // for messages persisted before v0.144.0 and for a provider that reports no
  // input count -- degrading to the previous behaviour, never to a false zero.
  //
  // The fallback is WRONG on Anthropic and is retained only because old messages
  // carry nothing better: Anthropic reports `input_token_count` exclusive of the
  // cached prefix, so with FEAT-0038 active it under-reported occupancy by most of
  // the window and the 95% warning arrived late. `context_base_tokens` corrects it.
  const input = usage?.input_token_count ?? 0
  const output = usage?.output_token_count ?? 0
  const consumed = usage?.context_base_tokens ?? input + output
  const rate = Math.min((consumed / max) * 100, 100)
  const level = getWarningLevel(rate)

  return (
    <div className="flex items-center gap-2 px-1 py-0.5">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
        <div className={cn('h-full rounded-full transition-all', barColors[level])} style={{ width: `${rate}%` }} />
      </div>
      <span className={cn('text-[11px] tabular-nums whitespace-nowrap', textColors[level])}>
        {Math.round(rate)}% ({formatTokenCount(consumed)} / {formatTokenCount(max)})
      </span>
    </div>
  )
}
