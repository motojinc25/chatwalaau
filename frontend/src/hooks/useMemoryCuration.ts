import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Per-turn Agent Memory curation (CTR-0165, PRP-0100 / UDR-0079).
 *
 * Owns the "like" state for the current chat: a map of turn_key -> curation
 * status. A turn present in the map is liked; absent is not. Toggling posts to
 * the CTR-0164 trigger API (which persists the state in the session record and
 * dispatches the CTR-0163 background reconcile pass), and a `memory_curated`
 * push over the CTR-0110 notification channel updates the status in real time.
 * The filled/empty state restores from the persisted session record on load.
 *
 * The like is per TURN (one user message + its assistant reply); liking either
 * message toggles the same turn (ChatPanel resolves the pair and the turn_key).
 */
export type MemoryCurationStatus = 'pending' | 'curated' | 'failed'

export interface MemoryToggleArgs {
  userText: string
  assistantText: string
  model?: string
}

// Feature availability is instance-wide; probe once and cache across mounts.
let cachedEnabled: boolean | null = null

export interface MemoryCuration {
  enabled: boolean
  states: Record<string, MemoryCurationStatus>
  toggle: (turnKey: string, args: MemoryToggleArgs) => void
}

export function useMemoryCuration(threadId: string | undefined): MemoryCuration {
  const [enabled, setEnabled] = useState<boolean>(cachedEnabled ?? false)
  const [states, setStates] = useState<Record<string, MemoryCurationStatus>>({})

  const statesRef = useRef(states)
  statesRef.current = states
  const threadIdRef = useRef(threadId)
  threadIdRef.current = threadId

  // Availability probe (once, module-cached).
  useEffect(() => {
    if (cachedEnabled !== null) {
      setEnabled(cachedEnabled)
      return
    }
    let alive = true
    fetch('/api/memory/status')
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((d: { enabled?: boolean }) => {
        cachedEnabled = !!d.enabled
        if (alive) setEnabled(cachedEnabled)
      })
      .catch(() => {
        cachedEnabled = false
      })
    return () => {
      alive = false
    }
  }, [])

  // Hydrate the persisted like state when the thread changes (CTR-0164 read).
  useEffect(() => {
    if (!enabled || !threadId) {
      setStates({})
      return
    }
    let alive = true
    setStates({})
    fetch(`/api/memory/liked/${encodeURIComponent(threadId)}`)
      .then((r) => (r.ok ? r.json() : { liked: [] }))
      .then((d: { liked?: { turn_key: string; status: string }[] }) => {
        if (!alive) return
        const next: Record<string, MemoryCurationStatus> = {}
        for (const e of d.liked ?? []) {
          next[e.turn_key] = (e.status as MemoryCurationStatus) ?? 'curated'
        }
        setStates(next)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [enabled, threadId])

  // Real-time completion via the CTR-0110 notification channel. Bounded reconnect
  // (mirrors useSession's session_title listener); on a deployment where the WS
  // cannot authenticate the status simply stays at its optimistic value.
  useEffect(() => {
    if (!enabled) return
    let socket: WebSocket | null = null
    let closed = false
    let attempts = 0
    let timer: number | undefined

    const connect = () => {
      if (closed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${proto}://${window.location.host}/ws/notifications`)
      socket.onopen = () => {
        attempts = 0
      }
      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as {
            type?: string
            thread_id?: string
            turn_key?: string
            status?: string
          }
          const turnKey = msg.turn_key
          if (msg.type === 'memory_curated' && typeof turnKey === 'string' && msg.thread_id === threadIdRef.current) {
            const status = (msg.status as MemoryCurationStatus) ?? 'curated'
            setStates((prev) => (turnKey in prev ? { ...prev, [turnKey]: status } : prev))
          }
        } catch {
          // ignore malformed frames
        }
      }
      socket.onclose = () => {
        if (closed || attempts >= 5) return
        attempts += 1
        timer = window.setTimeout(connect, 1000 * attempts)
      }
      socket.onerror = () => socket?.close()
    }
    connect()
    return () => {
      closed = true
      if (timer) window.clearTimeout(timer)
      socket?.close()
    }
  }, [enabled])

  const toggle = useCallback(
    (turnKey: string, args: MemoryToggleArgs) => {
      const tid = threadIdRef.current
      if (!enabled || !tid) return
      const liked = turnKey in statesRef.current
      setStates((prev) => {
        const next = { ...prev }
        if (liked) delete next[turnKey]
        else next[turnKey] = 'pending'
        return next
      })
      fetch('/api/memory/curate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          thread_id: tid,
          turn_key: turnKey,
          liked: !liked,
          user_text: args.userText,
          assistant_text: args.assistantText,
          model: args.model ?? '',
        }),
      }).catch(() => {
        // Revert the optimistic change on a transport failure.
        setStates((prev) => {
          const next = { ...prev }
          if (liked) next[turnKey] = 'curated'
          else delete next[turnKey]
          return next
        })
      })
    },
    [enabled],
  )

  return { enabled, states, toggle }
}
