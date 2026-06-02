import { useEffect, useRef, useState } from 'react'
import type { NavTurn } from '@/hooks/useMessageNavigator'
import { cn } from '@/lib/utils'

interface MessageNavigatorProps {
  turns: NavTurn[]
  activeId: string | null
  onJump: (messageId: string) => void
}

/**
 * CTR-0103 Message Navigator UI (PRP-0072 / UDR-0048).
 *
 * A floating rail of user-turn ticks pinned to the right gutter, vertically
 * centered, that toggles a right-aligned popover list of user-message
 * previews. Clicking an entry jumps to that turn (onJump -> CTR-0103
 * scrollToTurn). Overlay-only: never changes the chat column width. Closes on
 * a second rail click, an outside click, or Escape. The rail is inset by the
 * always-visible scrollbar width so it never sits on top of the bar
 * (CTR-0092 v5).
 */
export function MessageNavigator({ turns, activeId, onJump }: MessageNavigatorProps) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  // Close on outside click / Escape (CTR-0103). pointerdown so the close beats
  // a click landing on a message behind the panel.
  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div
      ref={containerRef}
      className="absolute right-[calc(var(--app-scrollbar-width)_+_0.5rem)] top-1/2 z-20 -translate-y-1/2">
      {/* Rail: one tick per user turn; doubles as the open/close toggle. */}
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        aria-label="Message navigator"
        aria-expanded={open}
        title="Message navigator"
        className={cn(
          'flex max-h-[60vh] flex-col items-end gap-1.5 overflow-hidden rounded-full px-1.5 py-2 transition-colors',
          open ? 'bg-accent/60' : 'hover:bg-accent/40',
        )}>
        {turns.map((turn) => (
          <span
            key={turn.messageId}
            className={cn(
              'h-1 rounded-full transition-all',
              turn.messageId === activeId ? 'w-5 bg-primary' : 'w-3 bg-muted-foreground/60',
            )}
          />
        ))}
      </button>

      {open && (
        // Popover: to the LEFT of the rail so the rail stays clickable to close.
        // Overlay only; right-anchored; internal vertical scroll when long.
        <div
          className="absolute right-full top-1/2 mr-1 max-h-[60vh] w-72 -translate-y-1/2 overflow-y-auto rounded-lg border bg-popover p-1 shadow-lg"
          role="menu"
          aria-label="Message shortcuts">
          {turns.map((turn) => (
            <button
              key={turn.messageId}
              type="button"
              role="menuitem"
              onClick={() => onJump(turn.messageId)}
              className={cn(
                'flex w-full items-center rounded-md px-3 py-2 text-left text-xs transition-colors hover:bg-accent hover:text-accent-foreground',
                turn.messageId === activeId && 'bg-accent/60 font-medium',
              )}>
              <span className="truncate">{turn.preview}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
