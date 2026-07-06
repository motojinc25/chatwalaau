import { useEffect, useState } from 'react'

/**
 * Probe whether the Ontology feature is enabled (CTR-0171, PRP-0105).
 *
 * GET /api/ontology/catalog returns 404 when ONTOLOGY_ENABLED is false
 * (UDR-0084 D12), so a successful response means the feature is available and
 * the launcher icon should be shown. Probed once on mount; silent on failure.
 */
export function useOntologyAvailable(): boolean {
  const [available, setAvailable] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/ontology/catalog')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: ontology is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return available
}
