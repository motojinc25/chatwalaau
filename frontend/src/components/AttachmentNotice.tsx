import { Info, X } from 'lucide-react'
import { useEffect } from 'react'
import type { AttachmentNoticeState } from '@/hooks/useImageAttachment'

const AUTO_HIDE_MS = 8000

interface AttachmentNoticeProps {
  notice: AttachmentNoticeState
  onDismiss: () => void
}

/**
 * Inline composer notice for attachments that were not attached (CTR-0023 v2,
 * UDR-0211 D4). It sits above the attachment row, is announced politely to screen
 * readers, and hides after 8 s, on X, or when the message is sent. A new notice
 * (new `id`) restarts the timer.
 */
export function AttachmentNotice({ notice, onDismiss }: AttachmentNoticeProps) {
  // The caller keys this component by notice.id, so each notice mounts afresh and
  // gets its own timer.
  useEffect(() => {
    const timer = window.setTimeout(onDismiss, AUTO_HIDE_MS)
    return () => window.clearTimeout(timer)
  }, [onDismiss])

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="attachment-notice"
      className="mx-3 mt-2 flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-xs text-foreground">
      <Info className="mt-px h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden="true" />
      <span className="min-w-0 flex-1 break-words">{notice.text}</span>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss attachment notice"
        className="shrink-0 rounded-sm text-muted-foreground hover:text-foreground">
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}
