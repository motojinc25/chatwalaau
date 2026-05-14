import { Zap } from 'lucide-react'
import { cn } from '@/lib/utils'

interface BackgroundResponsesToggleProps {
  enabled: boolean
  onToggle: (enabled: boolean) => void
}

export function BackgroundResponsesToggle({ enabled, onToggle }: BackgroundResponsesToggleProps) {
  return (
    <button
      type="button"
      onClick={() => onToggle(!enabled)}
      className={cn(
        'flex items-center gap-1 rounded-md px-1.5 h-6 text-xs transition-colors',
        enabled
          ? 'bg-blue-500/10 text-blue-500 hover:bg-blue-500/20'
          : 'text-muted-foreground/60 hover:text-muted-foreground',
      )}
      title={enabled ? 'Background Responses: ON' : 'Background Responses: OFF'}
      aria-label={enabled ? 'Disable background responses' : 'Enable background responses'}>
      <Zap className="h-3 w-3" />
      <span>BG</span>
    </button>
  )
}
