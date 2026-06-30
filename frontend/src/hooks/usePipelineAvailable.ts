import { useEffect, useState } from 'react'

/**
 * Probe whether the Pipeline Job subsystem is enabled (CTR-0146, PRP-0096).
 *
 * GET /api/pipeline/jobs returns 404 when PIPELINE_ENABLED is false (UDR-0074 D5), so a
 * successful response means the feature is available and the launcher icon should be
 * shown. Probed once on mount; silent on failure.
 */
export function usePipelineAvailable(): boolean {
  const [available, setAvailable] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/pipeline/jobs')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: the pipeline subsystem is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return available
}
