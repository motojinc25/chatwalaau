import { Zap } from 'lucide-react'
import { cn } from '@/lib/utils'

interface BackgroundResponsesToggleProps {
  enabled: boolean
  onToggle: (enabled: boolean) => void
  /**
   * When false the toggle is rendered disabled and cannot be clicked. Used to
   * gate background responses to providers that support them (CTR-0045): GPT /
   * Azure OpenAI models only -- Anthropic (Opus 4.7/4.8) has no background
   * equivalent (PRP-0073).
   */
  disabled?: boolean
}

export function BackgroundResponsesToggle({ enabled, onToggle, disabled = false }: BackgroundResponsesToggleProps) {
  const title = disabled
    ? 'Background Responses: not supported by this model'
    : enabled
      ? 'Background Responses: ON'
      : 'Background Responses: OFF'

  return (
    <button
      type="button"
      onClick={() => {
        if (disabled) return
        onToggle(!enabled)
      }}
      disabled={disabled}
      aria-disabled={disabled}
      className={cn(
        'flex items-center gap-1 rounded-md px-1.5 h-6 text-xs transition-colors',
        disabled
          ? 'cursor-not-allowed text-muted-foreground/30'
          : enabled
            ? 'bg-blue-500/10 text-blue-500 hover:bg-blue-500/20'
            : 'text-muted-foreground/60 hover:text-muted-foreground',
      )}
      title={title}
      aria-label={
        disabled
          ? 'Background responses not supported by this model'
          : enabled
            ? 'Disable background responses'
            : 'Enable background responses'
      }>
      <Zap className="h-3 w-3" />
      <span>BG</span>
    </button>
  )
}
