import { useEffect, useState } from 'react'

/**
 * Probe whether the Cron Scheduler is enabled (CTR-0133, PRP-0089).
 *
 * GET /api/cron/jobs returns 404 when CRON_ENABLED is false (UDR-0067 D10), so a
 * successful response means the feature is available and the launcher icon /
 * /cron command should be shown. Probed once on mount; silent on failure.
 */
export function useCronAvailable(): boolean {
  const [available, setAvailable] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/cron/jobs')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: cron is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return available
}
