import { AlertTriangle, Loader2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { ExportState } from '@/hooks/useSession'

/**
 * Page-level status of a chat export (PRP-0259, UDR-0240 D4/D5, CTR-0016 v11). Visible with the
 * sidebar closed (operator Q2): an animated spinner, the chat title, a percentage when the response
 * has a Content-Length (operator Q4), and Cancel. A failure stays until dismissed.
 */
export function ExportStatus({
  state,
  onCancel,
  onDismiss,
  redactTitle,
}: {
  state: ExportState
  onCancel: () => void
  onDismiss: () => void
  redactTitle: (title: string) => string
}) {
  const title = redactTitle(state.title)
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="export-status"
      className="fixed bottom-4 left-1/2 z-50 flex max-w-[calc(100vw-2rem)] -translate-x-1/2 items-center gap-2 rounded-md border bg-background px-3 py-2 text-sm shadow-lg">
      {state.error ? (
        <>
          <AlertTriangle className="h-4 w-4 shrink-0 text-amber-500" aria-hidden="true" />
          <span className="min-w-0 truncate">Export failed: {state.error}</span>
          <Button variant="ghost" size="sm" onClick={onDismiss} aria-label="Dismiss">
            <X className="h-3.5 w-3.5" />
            Dismiss
          </Button>
        </>
      ) : (
        <>
          <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0 truncate">
            Exporting &quot;{title}&quot;...
            {state.progress !== null && <span className="ml-1 tabular-nums">{state.progress}%</span>}
          </span>
          <Button variant="outline" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        </>
      )}
    </div>
  )
}
