import { useEffect, useState } from 'react'

/**
 * Probe whether the Inbound Webhook Gateway is enabled (CTR-0154, PRP-0097).
 *
 * GET /api/webhooks/sources returns 404 when WEBHOOK_ENABLED is false (UDR-0075 D11), so a
 * successful response means the gateway is available and the launcher icon should be
 * shown. Probed once on mount; silent on failure.
 */
export function useWebhookAvailable(): boolean {
  const [available, setAvailable] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/webhooks/sources')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: the webhook gateway is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return available
}
