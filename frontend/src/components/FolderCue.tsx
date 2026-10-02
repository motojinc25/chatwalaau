/**
 * Folder-aware chat cue (PRP-0196, UDR-0178, CTR-0016).
 *
 * Shows which folder the open chat belongs to. Rendered by ChatPage ONLY (D2): the
 * compact /popup and /sidebar surfaces share ChatPanel and must stay unchanged, so
 * neither component is ever imported by ChatPanel.
 *
 * - FolderGlow (D3): an edge-only inset shadow over the chat area in the folder's
 *   palette color. `pointer-events-none` + `aria-hidden`, so it can never intercept a
 *   click, a selection or a scroll, and never tints the background behind the text.
 *   Static apart from a short fade; no transition under reduced motion. It never
 *   pulses, which keeps it distinct from the Computer Use frame (UDR-0174).
 * - FolderBadge (D4): the folder icon in the folder color plus the folder name, routed
 *   through the Privacy Screen redaction seam (UDR-0107). Clicking it reveals the
 *   folder in the sidebar (operator answer Q2).
 */

import { Folder } from 'lucide-react'
import { usePrivacyScreen } from '@/hooks/usePrivacyScreen'
import { folderColorClasses } from '@/lib/folderColors'
import { cn } from '@/lib/utils'
import type { SessionFolder } from '@/types/chat'

/** Edge glow in the folder color; renders an invisible layer when `folder` is null. */
export function FolderGlow({ folder }: { folder: SessionFolder | null }) {
  return (
    <div
      aria-hidden="true"
      data-testid="folder-glow"
      className={cn(
        'pointer-events-none absolute inset-0 z-[15] transition-[box-shadow,opacity] duration-150 motion-reduce:transition-none',
        folder ? cn('opacity-100', folderColorClasses(folder.color).glow) : 'opacity-0',
      )}
    />
  )
}

export function FolderBadge({ folder, onReveal }: { folder: SessionFolder; onReveal: () => void }) {
  const { redact } = usePrivacyScreen()
  // The title / aria-label carry the name too, so they take the redacted string: a
  // plaintext attribute would publish the name to hover, DevTools and the a11y tree.
  const name = redact(folder.name, `folder:${folder.id}`)
  return (
    <button
      type="button"
      onClick={onReveal}
      title={name}
      aria-label={`Folder: ${name}`}
      className="inline-flex h-7 min-w-0 max-w-[16rem] items-center gap-1.5 rounded-full border bg-background/80 px-2.5 text-xs font-medium text-foreground shadow-sm backdrop-blur transition-colors hover:bg-accent">
      <Folder className={cn('h-3.5 w-3.5 shrink-0', folderColorClasses(folder.color).icon)} aria-hidden="true" />
      <span className="truncate">{name}</span>
    </button>
  )
}
