import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * A blocking "work in progress" overlay over its positioned parent (PRP-0211, UDR-0193 D2).
 *
 * `role="status"` with `aria-live="polite"` announces the label; the busy region itself
 * should carry `aria-busy` while this is shown.
 */
export function BusyOverlay({ label, className }: { label: string; className?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn('absolute inset-0 z-10 flex items-center justify-center bg-white/70', className)}>
      <div className="flex items-center gap-2 text-sm text-zinc-700">
        <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
        {label}
      </div>
    </div>
  )
}
