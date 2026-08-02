import { EyeOff } from 'lucide-react'
import { useAuthedBlobUrl } from '@/hooks/useAuthedBlobUrl'
import { usePrivacyScreen } from '@/hooks/usePrivacyScreen'
import { cn } from '@/lib/utils'

/**
 * An `<img>` for an authenticated upload URI (CTR-0051, PRP-0116, UDR-0097 D4).
 *
 * Renders the image through a credentialed fetch -> blob object URL instead of a
 * raw `<img src="/api/uploads/...">`, so the served bytes require auth and the
 * DOM `src` never exposes the shareable server URL. Shows a muted placeholder
 * while the blob loads. Callers keep passing the real server `uri` to any logic
 * (mask edit, paint edit, full-size open) -- this component is display-only.
 *
 * Privacy Screen (CTR-0190, PRP-0124 / UDR-0107 D5): an image cannot be
 * scrambled, so while the mode is on this renders an opaque placeholder AND
 * passes `null` to `useAuthedBlobUrl`, which suppresses the fetch. The bytes
 * therefore never enter the page at all -- not in a blob URL, not in the cache
 * of a hook that already ran.
 */
export function AuthedImage({ uri, alt, className }: { uri: string; alt?: string; className?: string }) {
  const { enabled: redacted } = usePrivacyScreen()
  const blobUrl = useAuthedBlobUrl(redacted ? null : uri)

  if (redacted) {
    return (
      <span
        className={cn(
          // A FIXED size, not the image's own: the natural dimensions of an
          // attachment are themselves a weak signal, and the placeholder must be
          // large enough to read as "deliberately hidden".
          'inline-flex h-24 w-32 items-center justify-center rounded-lg border bg-muted text-muted-foreground',
          className,
        )}
        role="img"
        aria-label="Image hidden by Privacy Screen"
        title="Hidden by Privacy Screen">
        <EyeOff className="h-4 w-4" aria-hidden />
      </span>
    )
  }

  if (!blobUrl) {
    return <span className={cn('inline-block animate-pulse rounded-lg bg-muted', className)} aria-hidden />
  }
  return <img src={blobUrl} alt={alt} className={className} />
}
