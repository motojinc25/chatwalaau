// AI Discussion state for one chat (CTR-0276, PRP-0232, UDR-0215).
//
// The run lives on the server; this hook mirrors it. A phase streams over `streamAdvance`;
// each speaker's message is shown as it streams and marked completed only when the server says
// it was saved (D4). Leaving the chat aborts the stream, which pauses the run on the server
// (D6) -- nothing continues in the background and nothing is retried automatically.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  type ContinueDiscussionRequest,
  DiscussionApiError,
  type DiscussionStreamEvent,
  discussionApi,
  type StoredDiscussionMessage,
  streamAdvance,
} from '@/lib/discussionApi'
import type {
  ChatMessage,
  DiscussionCitation,
  DiscussionCode,
  DiscussionMessageMeta,
  DiscussionRun,
} from '@/types/chat'

const TERMINAL = new Set(['completed', 'cancelled', 'failed'])

export function isDiscussionOpen(run: DiscussionRun | undefined): boolean {
  return Boolean(run && !TERMINAL.has(run.status))
}

function storedToChat(m: StoredDiscussionMessage): ChatMessage {
  return {
    id: m.message_id,
    role: m.role,
    content: m.contents.map((c) => c.text ?? '').join(''),
    createdAt: new Date().toISOString(),
    discussion: { ...m.discussion, status: 'completed' },
  }
}

interface UseDiscussionOptions {
  threadId?: string
  /** Only the full-page chat drives a discussion (PRP-0232 2.7); elsewhere it is read-only. */
  enabled: boolean
  /** The chat's loaded messages: a discussion chat is recognised by its brief message. */
  messages: ChatMessage[]
  upsertMessage: (id: string, update: (current: ChatMessage | undefined) => ChatMessage | null) => void
  /** A message was saved or the run changed: refresh the sidebar (title order, tag). */
  onChanged?: () => void
}

export function useDiscussion({ threadId, enabled, messages, upsertMessage, onChanged }: UseDiscussionOptions) {
  const [runs, setRuns] = useState<DiscussionRun[]>([])
  const [loaded, setLoaded] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const autoStartedRef = useRef<string | null>(null)
  const onChangedRef = useRef(onChanged)
  onChangedRef.current = onChanged

  const isDiscussionChat = useMemo(() => messages.some((m) => m.discussion), [messages])
  const latest = runs.at(-1)
  // The person's own choice of the next round's speakers (decision mode, PRP-0235), kept until
  // the waiting request is answered -- by a button or by the composer.
  const [nextSpeakers, setNextSpeakers] = useState<string[] | null>(null)
  const pendingId = latest?.pending_request?.id
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new waiting request starts from the computed selection
  useEffect(() => setNextSpeakers(null), [pendingId])

  /** Per seat of the latest run: rounds spoken / held, messages (code-computed, PRP-0235). */
  const participation = useMemo(() => {
    if (!latest) return []
    const held = new Set<string>()
    const spoken = new Map<string, Set<string>>()
    const counts = new Map<string, number>()
    for (const m of messages) {
      const meta = m.discussion
      if (!meta || (meta.run_id && meta.run_id !== latest.id)) continue
      if (meta.phase.startsWith('round:')) held.add(meta.phase)
      if (meta.kind !== 'participant') continue
      counts.set(meta.speaker_id, (counts.get(meta.speaker_id) ?? 0) + 1)
      if (meta.phase.startsWith('round:')) {
        const set = spoken.get(meta.speaker_id) ?? new Set<string>()
        set.add(meta.phase)
        spoken.set(meta.speaker_id, set)
      }
    }
    return latest.participants.map((p) => ({
      id: p.id,
      display_name: p.display_name,
      rounds_spoken: spoken.get(p.id)?.size ?? 0,
      rounds_held: held.size,
      messages: counts.get(p.id) ?? 0,
    }))
  }, [latest, messages])

  const replaceRun = useCallback((run: DiscussionRun) => {
    setRuns((prev) => {
      const index = prev.findIndex((r) => r.id === run.id)
      if (index === -1) return [...prev, run]
      const copy = prev.slice()
      copy[index] = run
      return copy
    })
  }, [])

  const reload = useCallback(async () => {
    if (!threadId) return
    try {
      const { runs: fresh } = await discussionApi.runs(threadId)
      setRuns(fresh)
    } catch (err) {
      if (!(err instanceof DiscussionApiError && err.status === 404)) {
        setError(err instanceof Error ? err.message : 'Could not load the discussion')
      }
    } finally {
      setLoaded(true)
    }
  }, [threadId])

  // A discussion chat loads its runs once; any other chat never calls the API.
  useEffect(() => {
    if (!threadId || !isDiscussionChat || loaded) return
    void reload()
  }, [threadId, isDiscussionChat, loaded, reload])

  // Leaving the chat closes a running phase's stream: the server pauses the run (D6).
  useEffect(() => () => abortRef.current?.abort(), [])

  const handleEvent = useCallback(
    (event: DiscussionStreamEvent) => {
      const value = (event.value ?? {}) as Record<string, unknown>
      switch (event.type) {
        case 'CUSTOM':
          if (event.name === 'discussion_state') {
            replaceRun(value as unknown as DiscussionRun)
          } else if (event.name === 'discussion_speaker') {
            const id = String(value.message_id)
            const meta: DiscussionMessageMeta = {
              run_id: '',
              speaker_id: String(value.speaker_id),
              role: String(value.role),
              display_name: String(value.display_name),
              phase: String(value.phase),
              round: Number(value.round ?? 0),
              kind: value.kind === 'moderator' ? 'moderator' : value.kind === 'report' ? 'report' : 'participant',
              color: typeof value.color === 'string' ? value.color : undefined,
              status: 'generating',
            }
            upsertMessage(id, (current) => ({
              id,
              role: 'assistant',
              content: current?.content ?? '',
              createdAt: current?.createdAt ?? new Date().toISOString(),
              discussion: { ...meta, run_id: current?.discussion?.run_id ?? '' },
            }))
          } else if (event.name === 'discussion_message_committed') {
            const id = String(value.message_id)
            // The research tools the turn used (PRP-0234): name and query only.
            const tools = Array.isArray(value.tools) ? (value.tools as { name: string; query: string }[]) : undefined
            // The reply's checked code references (PRP-0236, UDR-0219 D6).
            const citations = Array.isArray(value.citations) ? (value.citations as DiscussionCitation[]) : undefined
            upsertMessage(id, (current) =>
              current?.discussion
                ? {
                    ...current,
                    discussion: {
                      ...current.discussion,
                      status: 'completed',
                      ...(tools ? { tools } : {}),
                      ...(citations ? { citations } : {}),
                    },
                  }
                : (current ?? null),
            )
            const budget = value.budget as DiscussionRun['budget'] | undefined
            const readChars = typeof value.code_read_chars === 'number' ? value.code_read_chars : undefined
            if (budget || readChars !== undefined)
              setRuns((prev) =>
                prev.map((r, i) =>
                  i === prev.length - 1
                    ? {
                        ...r,
                        ...(budget ? { budget } : {}),
                        ...(readChars !== undefined && r.code ? { code: { ...r.code, read_chars: readChars } } : {}),
                      }
                    : r,
                ),
              )
          } else if (event.name === 'discussion_code_map') {
            // The code map of the run's folder (PRP-0236, UDR-0219 D7): building, then its counts.
            const code = value as unknown as DiscussionCode
            setRuns((prev) => prev.map((r, i) => (i === prev.length - 1 ? { ...r, code } : r)))
          } else if (event.name === 'discussion_selection') {
            // Who speaks in the round and why (PRP-0235, UDR-0218 D5).
            const record = value as unknown as NonNullable<DiscussionRun['selections']>[string]
            setRuns((prev) =>
              prev.map((r, i) =>
                i === prev.length - 1
                  ? { ...r, selections: { ...(r.selections ?? {}), [String(record.round)]: record } }
                  : r,
              ),
            )
          } else if (event.name === 'discussion_advisory') {
            setRuns((prev) =>
              prev.map((r, i) => (i === prev.length - 1 ? { ...r, advisory: value as DiscussionRun['advisory'] } : r)),
            )
          }
          break
        case 'TEXT_MESSAGE_CONTENT':
          if (event.messageId && event.delta) {
            const id = event.messageId
            const delta = event.delta
            upsertMessage(id, (current) => (current ? { ...current, content: current.content + delta } : null))
          }
          break
        case 'RUN_ERROR':
          setError(event.message ?? 'The discussion stopped.')
          break
      }
    },
    [replaceRun, upsertMessage],
  )

  /** Mark speakers that were still streaming as interrupted (never saved, D4). */
  const settleInterrupted = useCallback(
    (generatingIds: string[]) => {
      for (const id of generatingIds) {
        upsertMessage(id, (current) =>
          current?.discussion?.status === 'generating'
            ? { ...current, discussion: { ...current.discussion, status: 'interrupted' } }
            : (current ?? null),
        )
      }
    },
    [upsertMessage],
  )

  const generatingRef = useRef<Set<string>>(new Set())

  const advance = useCallback(
    async (runId: string) => {
      if (!threadId || abortRef.current) return
      const controller = new AbortController()
      abortRef.current = controller
      setStreaming(true)
      setError(null)
      generatingRef.current = new Set()
      try {
        const outcome = await streamAdvance(
          threadId,
          runId,
          (event) => {
            if (event.type === 'CUSTOM' && event.name === 'discussion_speaker') {
              generatingRef.current.add(String((event.value as { message_id?: string })?.message_id))
            }
            if (event.type === 'CUSTOM' && event.name === 'discussion_message_committed') {
              generatingRef.current.delete(String((event.value as { message_id?: string })?.message_id))
            }
            handleEvent(event)
          },
          controller.signal,
        )
        if (outcome === null && !controller.signal.aborted) {
          setError('The connection was lost. The discussion is paused; continue it when you are ready.')
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : 'The discussion could not continue.')
        }
      } finally {
        settleInterrupted([...generatingRef.current])
        abortRef.current = null
        setStreaming(false)
        if (!controller.signal.aborted) {
          await reload()
          onChangedRef.current?.()
        }
      }
    },
    [threadId, handleEvent, reload, settleInterrupted],
  )

  // A just-created run starts by itself, once (PRP-0232 2.7: Start creates and advances).
  useEffect(() => {
    if (!enabled || !latest || latest.status !== 'created' || streaming) return
    if (autoStartedRef.current === latest.id) return
    autoStartedRef.current = latest.id
    void advance(latest.id)
  }, [enabled, latest, streaming, advance])

  /** Run one request; false when it failed (the error is shown). */
  const act = useCallback(async (fn: () => Promise<void>): Promise<boolean> => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      return true
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The request failed.')
      return false
    } finally {
      setBusy(false)
    }
  }, [])

  const respond = useCallback(
    async (
      action: 'answer' | 'continue' | 'ask' | 'finish' | 'auto',
      text = '',
      speakerId?: string,
      speakersNext?: string[],
    ) => {
      if (!threadId || !latest?.pending_request) return false
      const runId = latest.id
      const requestId = latest.pending_request.id
      const ok = await act(async () => {
        const res = await discussionApi.input(threadId, runId, {
          request_id: requestId,
          action: action === 'answer' && !text.trim() ? 'continue' : action,
          text,
          ...(speakerId ? { speaker_id: speakerId } : {}),
          ...((speakersNext ?? nextSpeakers) && action !== 'ask' && action !== 'finish'
            ? { speakers: speakersNext ?? nextSpeakers ?? undefined }
            : {}),
        })
        replaceRun(res.run)
        for (const message of res.messages) upsertMessage(message.message_id, () => storedToChat(message))
      })
      if (ok) void advance(runId)
      return ok
    },
    [act, threadId, latest, replaceRun, upsertMessage, advance, nextSpeakers],
  )

  const pause = useCallback(
    () =>
      act(async () => {
        if (!threadId || !latest) return
        const res = await discussionApi.pause(threadId, latest.id)
        if (!abortRef.current) replaceRun(res.run)
      }),
    [act, threadId, latest, replaceRun],
  )

  const resume = useCallback(() => (latest ? advance(latest.id) : Promise.resolve()), [latest, advance])

  const cancel = useCallback(
    () =>
      act(async () => {
        if (!threadId || !latest) return
        const res = await discussionApi.cancel(threadId, latest.id)
        if (!abortRef.current) replaceRun(res.run)
      }),
    [act, threadId, latest, replaceRun],
  )

  const decide = useCallback(
    (verdict: 'adopt' | 'hold' | 'rediscuss', note = '') =>
      act(async () => {
        if (!threadId || !latest) return
        const res = await discussionApi.decision(threadId, latest.id, verdict, note)
        replaceRun(res.run)
        onChangedRef.current?.()
      }),
    [act, threadId, latest, replaceRun],
  )

  const continueDiscussion = useCallback(
    (body: ContinueDiscussionRequest) =>
      act(async () => {
        if (!threadId || !latest) return
        const res = await discussionApi.continueRun(threadId, latest.id, body)
        replaceRun(res.run)
        // The brief of the new run is a saved message; show it, then the run starts itself.
        for (const message of res.messages) upsertMessage(message.message_id, () => storedToChat(message))
      }),
    [act, threadId, latest, replaceRun, upsertMessage],
  )

  return {
    /** The chat holds a discussion (its brief is in the messages, or a run was loaded). */
    active: isDiscussionChat || runs.length > 0,
    runs,
    latest,
    open: isDiscussionOpen(latest),
    waiting: latest?.status === 'waiting_human' && !streaming,
    streaming,
    busy,
    error,
    dismissError: () => setError(null),
    respond,
    pause,
    resume,
    cancel,
    decide,
    continueDiscussion,
    /** The next round's speakers as the person changed them, or null for the computed selection. */
    nextSpeakers,
    setNextSpeakers,
    participation,
    threadId,
  }
}

export type DiscussionController = ReturnType<typeof useDiscussion>
