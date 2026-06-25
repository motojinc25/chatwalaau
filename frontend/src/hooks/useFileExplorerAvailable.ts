import { useEffect, useState } from 'react'

/**
 * Probe whether the File Explorer is enabled (CTR-0136, PRP-0091).
 *
 * GET /api/workspace/tree returns 404 unless FILE_EXPLORER_ENABLED and CODING_ENABLED
 * (UDR-0069 D3), so a successful response means the feature is available and the
 * launcher icon / /files command should be shown. Probed once on mount; silent on
 * failure (mirrors useCronAvailable).
 */
export function useFileExplorerAvailable(): boolean {
  const [available, setAvailable] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/workspace/tree')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: the File Explorer is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return available
}
