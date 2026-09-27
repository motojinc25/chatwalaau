import { MonitorCog, OctagonX } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { abortComputerUse, COMPUTER_USE_HOTKEY } from '@/lib/computerUse'

/**
 * "Controlling the desktop" banner (CTR-0235, PRP-0189 Section 2.11, UDR-0171 D11).
 *
 * Shown while a reply that uses the computer_* tools is streaming. It is one of four
 * kill switches; the other three (the global hotkey, moving the mouse, the Stop
 * button) work even when the target application covers this window, which is the
 * usual case while the agent works -- so the banner names them.
 */
export function ComputerUseBanner({ active }: { active: boolean }) {
  const [pending, setPending] = useState(false)
  if (!active) return null
  return (
    <div
      role="status"
      className="mx-3 mb-2 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-700 dark:text-amber-400">
      <MonitorCog className="h-3.5 w-3.5 shrink-0 animate-pulse" />
      <span className="min-w-0 flex-1">
        Controlling the desktop -- press <kbd className="font-mono">{COMPUTER_USE_HOTKEY}</kbd> or move the mouse to
        stop
      </span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-6 gap-1 px-2 text-xs"
        disabled={pending}
        onClick={() => {
          setPending(true)
          void abortComputerUse().finally(() => setPending(false))
        }}>
        <OctagonX className="h-3 w-3" />
        Abort
      </Button>
    </div>
  )
}
