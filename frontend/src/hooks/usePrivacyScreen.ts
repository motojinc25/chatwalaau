import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import { newRedactionSalt, redactText } from '@/lib/redact'

/**
 * Privacy Screen state (CTR-0190, PRP-0124 / UDR-0107).
 *
 * Holds the on/off flag for presentation-safe redaction and hands callers a
 * `redact(text, key)` that substitutes the string when the mode is on and is the
 * identity function when it is off.
 *
 * The flag IS persisted to localStorage -- deliberately the opposite of
 * Temporary Chat (UDR-0052 D5), because the failure modes run in opposite
 * directions (UDR-0107 D10). For Temporary Chat, persistence is the hazard. For
 * Privacy Screen, FORGETTING the flag on reload un-redacts a screen that is
 * already being shared, which is the exact event the feature exists to prevent.
 * Failing safe here means persisting. The compensating control is the always
 * visible active pill (UDR-0107 D11), so the mode can never be silently on
 * during normal work.
 */

/** localStorage key for the persisted flag (UDR-0107 D10). */
export const PRIVACY_SCREEN_STORAGE_KEY = 'chatwalaau:privacy-screen'

export interface PrivacyScreenApi {
  /** True while Privacy Screen is on. */
  enabled: boolean
  /** Turn on: persists the flag and mints a fresh redaction salt. */
  enable: () => void
  /** Turn off: clears the persisted flag and drops the memo cache. */
  disable: () => void
  /**
   * Redact `text` when the mode is on, otherwise return it unchanged.
   *
   * `key` MUST be a stable per-element identity (thread_id / message.id /
   * folder.id, plus a field discriminator when one element redacts more than one
   * field) -- never the plaintext (UDR-0107 D4).
   */
  redact: (text: string, key: string) => string
}

/**
 * Default value: the mode is off and `redact` is the identity function. A
 * component rendered outside the provider therefore behaves exactly as it did
 * before this feature, rather than throwing.
 */
export const PrivacyScreenContext = createContext<PrivacyScreenApi>({
  enabled: false,
  enable: () => {},
  disable: () => {},
  redact: (text) => text,
})

/** Read the redaction state. Safe outside the provider (reads as off). */
export function usePrivacyScreen(): PrivacyScreenApi {
  return useContext(PrivacyScreenContext)
}

/**
 * Read the persisted flag. A throwing or unavailable localStorage (private
 * mode, quota, blocked storage) MUST read as off rather than propagate -- the
 * app must still mount (CTR-0190 Failure Semantics).
 */
function readStoredFlag(): boolean {
  try {
    return localStorage.getItem(PRIVACY_SCREEN_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

function writeStoredFlag(enabled: boolean): void {
  try {
    if (enabled) localStorage.setItem(PRIVACY_SCREEN_STORAGE_KEY, '1')
    else localStorage.removeItem(PRIVACY_SCREEN_STORAGE_KEY)
  } catch {
    // Storage unavailable: the mode still works for this session but will not
    // survive a reload. Never throw from a toggle click.
  }
}

interface CacheEntry {
  source: string
  salt: string
  out: string
}

/**
 * The state machine behind `PrivacyScreenProvider`. Separated from the provider
 * component so this module stays JSX-free.
 *
 * The flag is restored via a LAZY useState initializer, so a reload while the
 * mode was on renders redacted from the first paint -- there is no frame in
 * which the plaintext is visible.
 */
export function usePrivacyScreenState(): PrivacyScreenApi {
  const [enabled, setEnabled] = useState<boolean>(readStoredFlag)
  // A fresh salt per mount, and a fresh one per enable(): the same plaintext
  // scrambles differently in a later activation (UDR-0107 D4).
  const [salt, setSalt] = useState<string>(newRedactionSalt)
  const cacheRef = useRef<Map<string, CacheEntry>>(new Map())

  const enable = useCallback(() => {
    cacheRef.current.clear()
    setSalt(newRedactionSalt())
    setEnabled(true)
    writeStoredFlag(true)
  }, [])

  const disable = useCallback(() => {
    cacheRef.current.clear()
    setEnabled(false)
    writeStoredFlag(false)
  }, [])

  /**
   * Memoized per key so the rendered text does not change across React
   * re-renders (UDR-0107 D4) -- a scramble recomputed per render flickers and
   * reads as a defect. Recomputed only when the source string or the salt
   * changes.
   */
  const redact = useCallback(
    (text: string, key: string): string => {
      if (!enabled || !text) return text
      const cached = cacheRef.current.get(key)
      if (cached && cached.source === text && cached.salt === salt) return cached.out
      const out = redactText(text, key, salt)
      cacheRef.current.set(key, { source: text, salt, out })
      return out
    },
    [enabled, salt],
  )

  return useMemo(() => ({ enabled, enable, disable, redact }), [enabled, enable, disable, redact])
}
