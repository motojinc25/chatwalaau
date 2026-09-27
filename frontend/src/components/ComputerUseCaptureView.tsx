import { ScanEye, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { type ComputerUseCapture, fetchLatestCapture } from '@/lib/computerUse'

const POLL_MS = 1000

/**
 * Live view of what the agent is looking at (CTR-0235, PRP-0189 amendment A4).
 *
 * A debugging aid, nothing more: a rounded box in the top-right corner of the main
 * area that shows the newest Computer Use capture of this chat while the agent is
 * working, and goes away when that work ends. Closing it hides only the current
 * capture -- the next capture opens it again -- and never affects the run: the
 * agent keeps the desktop, and the kill switches (Ctrl+Alt+End, moving the mouse,
 * Stop, Abort) are unchanged.
 *
 * Rendered by ChatPanel on the full-width /chat surface only (not /popup, /sidebar or
 * a narrow screen).
 */
export function ComputerUseCaptureView({ threadId, active }: { threadId?: string | null; active: boolean }) {
  const [capture, setCapture] = useState<ComputerUseCapture | null>(null)
  const [dismissed, setDismissed] = useState<string | null>(null)
  const sinceRef = useRef(0)

  useEffect(() => {
    if (!active || !threadId) {
      setCapture(null)
      return
    }
    // Only captures taken during THIS activity: latest.json may still point at a
    // screen from an earlier turn.
    sinceRef.current = Date.now() - 2000
    let cancelled = false
    const tick = async () => {
      const next = await fetchLatestCapture(threadId)
      if (cancelled || !next) return
      if (Date.parse(next.ts) < sinceRef.current) return
      setCapture((prev) => (prev?.file === next.file ? prev : next))
    }
    void tick()
    const timer = window.setInterval(() => void tick(), POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [active, threadId])

  if (!active || !capture || capture.file === dismissed) return null
  return (
    <aside
      aria-label="Computer Use capture"
      className="absolute right-3 top-3 z-40 w-[min(420px,40%)] overflow-hidden rounded-xl border bg-background/95 shadow-lg backdrop-blur">
      <div className="flex items-center gap-1.5 border-b px-2.5 py-1.5 text-[0.7rem] text-muted-foreground">
        <ScanEye className="h-3.5 w-3.5 shrink-0 animate-pulse" />
        <span className="min-w-0 flex-1 truncate" title={capture.window}>
          {capture.window || 'Computer Use'}
        </span>
        <span className="shrink-0 font-mono">{capture.obs}</span>
        <button
          type="button"
          aria-label="Close capture view"
          title="Close (the agent keeps working)"
          className="-mr-1 rounded p-0.5 hover:bg-muted hover:text-foreground"
          onClick={() => setDismissed(capture.file)}>
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <img src={capture.url} alt={`Capture ${capture.obs}`} className="block h-auto w-full" />
    </aside>
  )
}
