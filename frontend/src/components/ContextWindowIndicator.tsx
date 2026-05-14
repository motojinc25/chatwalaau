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

  const input = usage?.input_token_count ?? 0
  const output = usage?.output_token_count ?? 0
  const consumed = input + output
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
