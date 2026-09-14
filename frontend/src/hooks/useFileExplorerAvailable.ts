import { useEffect, useState } from 'react'

/**
 * Probe whether the File Explorer is enabled (CTR-0136, PRP-0091).
 *
 * GET /api/workspace/tree returns 404 unless FILE_EXPLORER_ENABLED and CODING_ENABLED
 * (UDR-0069 D3), so a successful response means the feature is available and the
 * launcher icon / /files command should be shown. Probed once on mount; silent on
 * failure (mirrors useCronAvailable).
 *
 * Tri-state (PRP-0166): `undefined` until the probe settles, so a workspace file
 * reference in chat history does not flash "File Explorer is disabled" while the
 * probe is still in flight.
 */
export function useFileExplorerProbe(): boolean | undefined {
  const [available, setAvailable] = useState<boolean | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/workspace/tree')
        if (!cancelled) setAvailable(res.ok)
      } catch {
        // Silent: the File Explorer is simply unavailable.
        if (!cancelled) setAvailable(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return available
}

export function useFileExplorerAvailable(): boolean {
  return useFileExplorerProbe() === true
}
