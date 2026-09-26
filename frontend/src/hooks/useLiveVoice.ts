import { useCallback, useEffect, useRef, useState } from 'react'
import type { LiveMessageUpdate } from '@/hooks/useChat'
import { useMicLevel } from '@/hooks/useMicLevel'

/**
 * Live voice conversation over WebRTC (CTR-0228, PRP-0188, UDR-0170).
 *
 * Audio goes browser <-> GPT-Live directly on the WebRTC media track. The session is
 * created by the backend (CTR-0223: SDP offer in, answer out -- GPT-Live has no
 * ephemeral client keys), and EVERYTHING else comes from the backend's event stream
 * (CTR-0224): the sideband there is the only controller (UDR-0170 D3). The data
 * channel is opened because the transport requires it, and its events are ignored on
 * purpose -- acting on them would run delegations twice.
 */
export type LiveState = 'idle' | 'connecting' | 'live' | 'closing'

export interface LiveLimits {
  max_session_seconds: number
  idle_timeout_seconds: number
}

/**
 * A delegation's progress (step 3): `live.delegation` state changes and `live.progress`
 * snapshots, keyed by the chat message id the final answer will carry.
 */
export interface LiveDelegationEvent {
  delegation_id: string
  message_id: string
  state?: 'queued' | 'started' | 'cancelling' | 'completed' | 'failed' | 'cancelled'
  text?: string
  tools?: Array<{ id: string; name: string; status: string }>
}

interface UseLiveVoiceOptions {
  threadId?: string
  onMessage: (update: LiveMessageUpdate) => void
  /** Step 3: drives the in-chat marker of each delegation. */
  onDelegation?: (event: LiveDelegationEvent) => void
  /** The session is up (the chat now exists on the server). */
  onStarted?: () => void
  /** The session ended, for whatever reason. */
  onEnded?: (reason: string) => void
}

export interface UseLiveVoiceReturn {
  state: LiveState
  levels: number[]
  muted: boolean
  working: boolean
  /** Step 3: delegations queued or running. */
  workingCount: number
  remainingSeconds: number | null
  error: string | null
  notice: string | null
  start: () => Promise<void>
  stop: () => void
  toggleMute: () => void
  /** Step 3: a message typed during Live. Resolves false when it was not accepted. */
  sendText: (text: string) => Promise<boolean>
  /** Step 3: cancel a queued or running delegation. */
  cancelDelegation: (delegationId: string) => void
}

const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled'])

const CLOSE_FALLBACK_MS = 12_000

function describeClose(reason: string, limits: LiveLimits | null): string | null {
  switch (reason) {
    case 'max_duration':
      return `Live ended after ${Math.round((limits?.max_session_seconds ?? 600) / 60)} minutes.`
    case 'idle_timeout':
      return `Live ended after ${limits?.idle_timeout_seconds ?? 60} s of silence.`
    case 'expired':
      return 'Live ended: the session expired.'
    case 'content':
      return 'Live ended by the service content policy.'
    case 'connection_lost':
    case 'remote_hangup':
      return 'Live ended: the connection was lost.'
    default:
      return null
  }
}

async function readError(res: Response): Promise<string> {
  try {
    const body = await res.json()
    const detail = body?.detail
    if (typeof detail === 'string') return detail
    if (detail?.code === 'live_busy') return 'A Live conversation is already running for this chat.'
    if (detail?.code === 'live_unavailable') return 'Live conversation is not available on this server.'
    if (detail?.message) return String(detail.message)
    if (detail?.code) return String(detail.code)
  } catch {
    // fall through
  }
  return `Live could not start (HTTP ${res.status}).`
}

export function useLiveVoice({
  threadId,
  onMessage,
  onDelegation,
  onStarted,
  onEnded,
}: UseLiveVoiceOptions): UseLiveVoiceReturn {
  const [state, setState] = useState<LiveState>('idle')
  const [stream, setStream] = useState<MediaStream | null>(null)
  const [muted, setMuted] = useState(false)
  const [workingCount, setWorkingCount] = useState(0)
  const working = workingCount > 0
  const [remainingSeconds, setRemainingSeconds] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const levels = useMicLevel(stream)

  const pcRef = useRef<RTCPeerConnection | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const sseAbortRef = useRef<AbortController | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  const limitsRef = useRef<LiveLimits | null>(null)
  const deadlineRef = useRef<number | null>(null)
  const fallbackRef = useRef<number | null>(null)
  const delegationsRef = useRef(new Set<string>())
  const endedRef = useRef(true)
  const callbacks = useRef({ onMessage, onDelegation, onStarted, onEnded })
  callbacks.current = { onMessage, onDelegation, onStarted, onEnded }

  const teardown = useCallback((reason: string) => {
    if (endedRef.current) return
    endedRef.current = true
    if (fallbackRef.current !== null) {
      window.clearTimeout(fallbackRef.current)
      fallbackRef.current = null
    }
    sseAbortRef.current?.abort()
    sseAbortRef.current = null
    pcRef.current?.close()
    pcRef.current = null
    for (const track of streamRef.current?.getTracks() ?? []) track.stop()
    streamRef.current = null
    if (audioRef.current) {
      audioRef.current.srcObject = null
      audioRef.current = null
    }
    sessionIdRef.current = null
    deadlineRef.current = null
    delegationsRef.current.clear()
    setStream(null)
    setMuted(false)
    setWorkingCount(0)
    setRemainingSeconds(null)
    setState('idle')
    setNotice(describeClose(reason, limitsRef.current))
    callbacks.current.onEnded?.(reason)
  }, [])

  const handleEvent = useCallback(
    (event: string, data: Record<string, unknown>) => {
      switch (event) {
        case 'live.ready': {
          const limits = data.limits as LiveLimits | undefined
          if (limits) limitsRef.current = limits
          if (deadlineRef.current === null && limits) {
            deadlineRef.current = Date.now() + limits.max_session_seconds * 1000
          }
          setState((s) => (s === 'connecting' ? 'live' : s))
          break
        }
        case 'live.message':
          callbacks.current.onMessage(data as unknown as LiveMessageUpdate)
          break
        case 'live.delegation': {
          // Step 3: queued AND running count, so "Working (n)" is the whole backlog.
          const id = String(data.delegation_id ?? '')
          if (TERMINAL_STATES.has(String(data.state))) delegationsRef.current.delete(id)
          else delegationsRef.current.add(id)
          setWorkingCount(delegationsRef.current.size)
          callbacks.current.onDelegation?.(data as unknown as LiveDelegationEvent)
          break
        }
        case 'live.progress':
          callbacks.current.onDelegation?.(data as unknown as LiveDelegationEvent)
          break
        case 'live.mute':
          setMuted(Boolean(data.muted))
          break
        case 'live.warning':
          if (data.kind === 'time') setNotice('Live ends in one minute.')
          else if (data.kind === 'context') setNotice('The Live conversation is close to its context limit.')
          break
        case 'live.error':
          setError(String(data.message ?? data.code ?? 'Live error'))
          break
        case 'live.closed':
          teardown(String(data.reason ?? 'closed'))
          break
      }
    },
    [teardown],
  )

  const subscribe = useCallback(
    async (liveSessionId: string) => {
      const controller = new AbortController()
      sseAbortRef.current = controller
      try {
        const res = await fetch(`/api/live/sessions/${encodeURIComponent(liveSessionId)}/events`, {
          signal: controller.signal,
          headers: { Accept: 'text/event-stream' },
        })
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let boundary = buffer.indexOf('\n\n')
          while (boundary !== -1) {
            const block = buffer.slice(0, boundary)
            buffer = buffer.slice(boundary + 2)
            boundary = buffer.indexOf('\n\n')
            let event = 'message'
            let payload = ''
            for (const line of block.split('\n')) {
              if (line.startsWith('event:')) event = line.slice(6).trim()
              else if (line.startsWith('data:')) payload += line.slice(5).trim()
            }
            if (!payload) continue
            try {
              handleEvent(event, JSON.parse(payload))
            } catch {
              // a malformed frame is skipped, not fatal
            }
          }
        }
      } catch {
        // aborted by teardown, or the stream failed
      }
      // The stream ended without live.closed: the session is gone either way.
      teardown('connection_lost')
    },
    [handleEvent, teardown],
  )

  const start = useCallback(async () => {
    if (!threadId || !endedRef.current) return
    endedRef.current = false
    setError(null)
    setNotice(null)
    setState('connecting')
    try {
      // Echo cancellation matters doubly here: the assistant's own voice must not be
      // heard back as the user talking over it (full duplex).
      const media = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
      if (endedRef.current) {
        // Stopped while the permission prompt was open.
        for (const track of media.getTracks()) track.stop()
        return
      }
      streamRef.current = media
      setStream(media)

      const pc = new RTCPeerConnection()
      pcRef.current = pc
      for (const track of media.getAudioTracks()) pc.addTrack(track, media)
      const audio = new Audio()
      audio.autoplay = true
      audioRef.current = audio
      pc.ontrack = (e) => {
        audio.srcObject = e.streams[0] ?? new MediaStream([e.track])
        void audio.play().catch(() => undefined)
      }
      // Required by the transport; deliberately unused (UDR-0170 D3).
      pc.createDataChannel('oai-events')
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === 'failed') {
          setError('The audio connection failed.')
          const id = sessionIdRef.current
          if (id) void fetch(`/api/live/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
          teardown('connection_lost')
        }
      }

      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      const res = await fetch('/api/live/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_id: threadId, sdp_offer: offer.sdp }),
      })
      if (!res.ok) throw new Error(await readError(res))
      const body = (await res.json()) as { live_session_id: string; sdp_answer: string; limits: LiveLimits }
      if (endedRef.current) {
        // Stopped while connecting: close the session that was just created.
        void fetch(`/api/live/sessions/${encodeURIComponent(body.live_session_id)}`, { method: 'DELETE' })
        return
      }
      sessionIdRef.current = body.live_session_id
      limitsRef.current = body.limits
      deadlineRef.current = Date.now() + body.limits.max_session_seconds * 1000
      await pc.setRemoteDescription({ type: 'answer', sdp: body.sdp_answer })
      callbacks.current.onStarted?.()
      void subscribe(body.live_session_id)
    } catch (err) {
      const message =
        err instanceof DOMException && err.name === 'NotAllowedError'
          ? 'Microphone access denied'
          : err instanceof Error
            ? err.message
            : 'Live could not start'
      const id = sessionIdRef.current
      if (id) void fetch(`/api/live/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
      teardown('start_failed')
      setNotice(null)
      setError(message)
    }
  }, [threadId, subscribe, teardown])

  const stop = useCallback(() => {
    if (endedRef.current) return
    const id = sessionIdRef.current
    if (!id) {
      teardown('user_stop')
      return
    }
    setState('closing')
    void fetch(`/api/live/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => undefined)
    // live.closed normally ends it; this only covers a stream that never says so.
    fallbackRef.current = window.setTimeout(() => teardown('user_stop'), CLOSE_FALLBACK_MS)
  }, [teardown])

  const toggleMute = useCallback(() => {
    const id = sessionIdRef.current
    if (!id) return
    setMuted((prev) => {
      const next = !prev
      // Locally first: a muted microphone sends nothing at all, whatever the service
      // does with the mute command.
      for (const track of streamRef.current?.getAudioTracks() ?? []) track.enabled = !next
      void fetch(`/api/live/sessions/${encodeURIComponent(id)}/mute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ muted: next }),
      }).catch(() => undefined)
      return next
    })
  }, [])

  const sendText = useCallback(async (text: string): Promise<boolean> => {
    const id = sessionIdRef.current
    if (!id || !text.trim()) return false
    try {
      const res = await fetch(`/api/live/sessions/${encodeURIComponent(id)}/text`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      })
      if (!res.ok) {
        setError(await readError(res))
        return false
      }
      return true
    } catch {
      setError('The message could not be sent to the Live conversation.')
      return false
    }
  }, [])

  const cancelDelegation = useCallback((delegationId: string) => {
    const id = sessionIdRef.current
    if (!id) return
    void fetch(`/api/live/sessions/${encodeURIComponent(id)}/delegations/${encodeURIComponent(delegationId)}/cancel`, {
      method: 'POST',
    }).catch(() => undefined)
  }, [])

  // The countdown the Live row shows.
  useEffect(() => {
    if (state !== 'live' && state !== 'closing') return
    const update = () => {
      const deadline = deadlineRef.current
      setRemainingSeconds(deadline === null ? null : Math.max(0, Math.round((deadline - Date.now()) / 1000)))
    }
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [state])

  // Leaving the page or the chat ends the session (the backend would otherwise close
  // it as subscriber_gone after its grace period).
  useEffect(() => {
    return () => {
      const id = sessionIdRef.current
      if (id) void fetch(`/api/live/sessions/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive: true })
      teardown('user_stop')
    }
  }, [teardown])

  // Switching to another chat ends a running session.
  // biome-ignore lint/correctness/useExhaustiveDependencies: threadId is the trigger
  useEffect(() => {
    return () => {
      if (!endedRef.current) {
        const id = sessionIdRef.current
        if (id) void fetch(`/api/live/sessions/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive: true })
        teardown('user_stop')
      }
    }
  }, [threadId, teardown])

  return {
    state,
    levels,
    muted,
    working,
    workingCount,
    remainingSeconds,
    error,
    notice,
    start,
    stop,
    toggleMute,
    sendText,
    cancelDelegation,
  }
}
