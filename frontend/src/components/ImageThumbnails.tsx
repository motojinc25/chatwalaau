import { Loader2, Pencil, X } from 'lucide-react'
import type { ImageAttachment } from '@/hooks/useImageAttachment'
import { cn } from '@/lib/utils'

interface ImageThumbnailsProps {
  attachments: ImageAttachment[]
  onRemove: (id: string) => void
  /** Re-edit a paint-origin attachment (CTR-0160/CTR-0161, PRP-0099). */
  onEdit?: (attachment: ImageAttachment) => void
}

export function ImageThumbnails({ attachments, onRemove, onEdit }: ImageThumbnailsProps) {
  if (attachments.length === 0) return null

  return (
    <div className="flex gap-2 overflow-x-auto px-3 pt-2 pb-1">
      {attachments.map((attachment) => (
        <div key={attachment.id} className="group/thumb relative shrink-0">
          <div
            className={cn(
              'h-16 w-16 overflow-hidden rounded-lg border',
              attachment.status === 'error' && 'border-destructive',
            )}>
            <img src={attachment.previewUrl} alt={attachment.file.name} className="h-full w-full object-cover" />
            {attachment.status === 'uploading' && (
              <div className="absolute inset-0 flex items-center justify-center bg-background/60">
                <Loader2 className="h-4 w-4 animate-spin" />
              </div>
            )}
            {attachment.isPaint && attachment.status === 'ready' && onEdit && (
              <button
                type="button"
                onClick={() => onEdit(attachment)}
                className={cn(
                  'absolute inset-0 flex items-center justify-center bg-foreground/40 text-background',
                  'opacity-0 transition-opacity group-hover/thumb:opacity-100',
                )}
                aria-label={`Edit ${attachment.file.name}`}>
                <Pencil className="h-4 w-4" />
              </button>
            )}
          </div>
          <button
            type="button"
            onClick={() => onRemove(attachment.id)}
            className={cn(
              'absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center',
              'rounded-full bg-foreground text-background',
              'opacity-0 transition-opacity group-hover/thumb:opacity-100',
            )}
            aria-label={`Remove ${attachment.file.name}`}>
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  )
}
