import type { ReactNode } from 'react'
import { PrivacyScreenContext, usePrivacyScreenState } from '@/hooks/usePrivacyScreen'

/**
 * Mounts the Privacy Screen state at the app root (CTR-0190, PRP-0124 /
 * UDR-0107 D11).
 *
 * The toggle CONTROL is the full-page /chat surface only (matching CTR-0107),
 * but the redaction EFFECT is app-wide: while the flag is on, every covered
 * surface redacts in every scenario, including the compact /popup and /sidebar
 * views. That is why the provider wraps the router rather than living inside
 * ChatPage.
 */
export function PrivacyScreenProvider({ children }: { children: ReactNode }) {
  const api = usePrivacyScreenState()
  return <PrivacyScreenContext.Provider value={api}>{children}</PrivacyScreenContext.Provider>
}
