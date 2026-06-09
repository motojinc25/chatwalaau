import { Ghost, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface TemporaryChatToggleProps {
  /** Whether a temporary chat is currently active. */
  isTemporary: boolean
  /** Start a temporary chat. */
  onEnter: () => void
  /** Exit the temporary chat. */
  onExit: () => void
}

/**
 * Top-right control that toggles Temporary Chat (CTR-0107, PRP-0076).
 *
 * Inactive: a ghost icon button ("start temporary chat"). Active: a distinct,
 * highlighted pill showing a Ghost icon + "Temporary" label + an X affordance so
 * the user can tell they are inside a temporary chat and leave it. Rendered on
 * the full-page /chat scenario only (the parent gates this).
 */
export function TemporaryChatToggle({ isTemporary, onEnter, onExit }: TemporaryChatToggleProps) {
  if (isTemporary) {
    return (
      <button
        type="button"
        onClick={onExit}
        title="You are in a temporary chat. Click to exit."
        aria-label="Exit temporary chat"
        className={cn(
          'inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-xs font-medium',
          'bg-foreground text-background hover:opacity-90 transition-opacity',
        )}>
        <Ghost className="h-4 w-4" />
        <span>Temporary</span>
        <X className="h-3.5 w-3.5 opacity-70" />
      </button>
    )
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-8 w-8 text-muted-foreground hover:text-foreground"
      onClick={onEnter}
      title="Start a temporary chat (not saved to history, not used for personalization)"
      aria-label="Start temporary chat">
      <Ghost className="h-4 w-4" />
    </Button>
  )
}
