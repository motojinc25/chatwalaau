import { useEffect, useState } from 'react'

/**
 * Fetch a same-origin resource with the SPA's ambient credentials (session
 * cookie / loopback) and expose it as a blob object URL (CTR-0051, PRP-0116,
 * UDR-0097 D4).
 *
 * Uploaded / generated images are now served behind authentication (CTR-0022 v2),
 * so an `<img src="/api/uploads/...">` must not leak the raw server URL and must
 * carry credentials. Fetching to a blob keeps the DOM `src` a local `blob:` URL
 * (no leak-by-copy) and reuses the CTR-0137 ImageViewer pattern. The object URL
 * is revoked on unmount / uri change. Returns `null` while loading or on error.
 */
export function useAuthedBlobUrl(uri: string | null | undefined): string | null {
  const [blobUrl, setBlobUrl] = useState<string | null>(null)

  useEffect(() => {
    if (!uri) {
      setBlobUrl(null)
      return
    }
    let cancelled = false
    let objectUrl: string | null = null
    setBlobUrl(null)
    void (async () => {
      try {
        const res = await fetch(uri) // same-origin -> session cookie / loopback auth
        if (!res.ok) throw new Error(String(res.status))
        const blob = await res.blob()
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setBlobUrl(objectUrl)
      } catch {
        if (!cancelled) setBlobUrl(null)
      }
    })()
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [uri])

  return blobUrl
}
