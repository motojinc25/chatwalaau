import { AlertCircle, FileText, Loader2, X } from 'lucide-react'
import { cn } from '@/lib/utils'

interface PdfFileCardProps {
  filename: string
  size: number
  status: 'uploading' | 'ready' | 'error'
  onRemove?: () => void
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1048576).toFixed(1)} MB`
}

export function PdfFileCard({ filename, size, status, onRemove }: PdfFileCardProps) {
  return (
    <div
      className={cn(
        'flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs',
        status === 'error' && 'border-destructive/50 bg-destructive/10',
        status === 'uploading' && 'opacity-70',
      )}>
      {status === 'uploading' ? (
        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
      ) : status === 'error' ? (
        <AlertCircle className="h-4 w-4 shrink-0 text-destructive" />
      ) : (
        <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
      )}
      <span className="truncate font-medium">{filename}</span>
      <span className="shrink-0 text-muted-foreground">{formatSize(size)}</span>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          className="ml-auto shrink-0 rounded-sm p-0.5 text-muted-foreground hover:text-foreground"
          aria-label={`Remove ${filename}`}>
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  )
}
