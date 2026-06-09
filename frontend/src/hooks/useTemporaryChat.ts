import { useCallback, useState } from 'react'

/**
 * Temporary Chat state (CTR-0107, PRP-0076, UDR-0052).
 *
 * Holds the ephemeral "incognito-style" mode flag and a fresh `temp_` thread id
 * in React state ONLY -- never localStorage / session JSON / the backend
 * (UDR-0052 D5). Closing / leaving / reloading therefore loses the context
 * irrecoverably; the server-side quarantine copy is unreachable and TTL-swept.
 *
 * Entering always starts a new thread (UDR-0052 D11) -- existing chats are never
 * converted or modified.
 */
export interface TemporaryChatApi {
  /** True while the current view is a temporary chat. */
  isTemporary: boolean
  /** "temp_<uuid>" while active, else null. React state only (never persisted). */
  tempThreadId: string | null
  /** Start a fresh temporary thread. */
  enter: () => void
  /** Discard the temporary thread and return to a normal state. */
  exit: () => void
}

export function useTemporaryChat(): TemporaryChatApi {
  const [tempThreadId, setTempThreadId] = useState<string | null>(null)

  const enter = useCallback(() => {
    setTempThreadId(`temp_${crypto.randomUUID()}`)
  }, [])

  const exit = useCallback(() => {
    setTempThreadId(null)
  }, [])

  return {
    isTemporary: tempThreadId !== null,
    tempThreadId,
    enter,
    exit,
  }
}
