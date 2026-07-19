import { useAuthedBlobUrl } from '@/hooks/useAuthedBlobUrl'
import { cn } from '@/lib/utils'

/**
 * An `<img>` for an authenticated upload URI (CTR-0051, PRP-0116, UDR-0097 D4).
 *
 * Renders the image through a credentialed fetch -> blob object URL instead of a
 * raw `<img src="/api/uploads/...">`, so the served bytes require auth and the
 * DOM `src` never exposes the shareable server URL. Shows a muted placeholder
 * while the blob loads. Callers keep passing the real server `uri` to any logic
 * (mask edit, paint edit, full-size open) -- this component is display-only.
 */
export function AuthedImage({ uri, alt, className }: { uri: string; alt?: string; className?: string }) {
  const blobUrl = useAuthedBlobUrl(uri)
  if (!blobUrl) {
    return <span className={cn('inline-block animate-pulse rounded-lg bg-muted', className)} aria-hidden />
  }
  return <img src={blobUrl} alt={alt} className={className} />
}
