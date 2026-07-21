import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChatMessage, ImageRef, McpAppEvent, UsageInfo } from '@/types/chat'

/**
 * AG-UI protocol event types (CTR-0009).
 * @see https://docs.ag-ui.com/concepts/events
 */
interface AguiEvent {
  type: string
  messageId?: string
  delta?: string
  message?: string
  content?: string
  role?: string
  toolCallId?: string
  toolCallName?: string
  name?: string
  value?: Record<string, unknown>
}

interface UseChatOptions {
  threadId?: string
  initialMessages?: ChatMessage[]
  onStreamComplete?: () => void
  /**
   * Fired right after a NEW session is created server-side (the init call
   * returned status "created"), before the agent stream begins, so the SPA can
   * show the new chat in the sidebar immediately instead of waiting for the AI
   * answer (PRP-0077, CTR-0016). Not fired for resumed / regenerate sends or
   * temporary chats.
   */
  onSessionCreated?: (info: { threadId: string; title: string }) => void
  bgEnabled?: boolean
  selectedModel?: string
  /**
   * Selected per-message generation options (effort + verbosity) sent as AG-UI
   * state.model_options (CTR-0009 v13, PRP-0081). Supersedes the single
   * selectedReasoning string; the backend still accepts the legacy field.
   */
  selectedModelOptions?: Record<string, string>
  /**
   * Structured output (CTR-0118 / CTR-0009 v14, PRP-0082). `selectedOutputFormat`
   * is 'none' (off, default), 'json_object' (generic), or 'json_schema' (explicit
   * schema). For 'json_schema' the schema is sent as AG-UI state.output_schema; a
   * null/empty schema degrades to state.output_format='json_object'.
   */
  selectedOutputFormat?: string
  selectedOutputSchema?: Record<string, unknown> | null
  /**
   * Per-session image output options (CTR-0120 / CTR-0049, PRP-0085). Sent as AG-UI
   * state.image_options {size, quality, format, compression, background}; only
   * non-default fields are present. Becomes the generate_image / edit_image default
   * (an explicit LLM tool argument still wins).
   */
  selectedImageOptions?: Record<string, string>
  /**
   * Temporary Chat (CTR-0107 / CTR-0106, PRP-0076). When true the run is sent
   * with AG-UI state.temporary=true (de-personalized, quarantine-routed) and the
   * sidebar-creating session init call is skipped so it never appears in history.
   */
  temporary?: boolean
  /**
   * Declarative Workflow run-target (CTR-0185, PRP-0118, UDR-0101 D3/D5). When set, the
   * run is sent with AG-UI state.workflow_id so the endpoint streams the compiled
   * workflow (its own RUN_STARTED/RUN_FINISHED + additive workflow_* CUSTOM events)
   * instead of the active Prompt agent. Per-conversation, not a persona.
   */
  selectedWorkflowId?: string
  /**
   * PRP-0118: label of the run-target (a workflow name, or a non-default active agent
   * name) stamped onto the assistant message so the action bar shows which agent /
   * workflow produced the turn.
   */
  runTargetLabel?: string
  /**
   * PRP-0067 / CTR-0100. Receives AG-UI CUSTOM events that useChat does
   * not itself act on (e.g., tool_approval_request /
   * tool_approval_response). useToolApproval supplies this callback.
   */
  onCustomEvent?: (name: string | undefined, value: Record<string, unknown> | undefined) => void
  /**
   * v0.77.1 (CTR-0009): transient/informational status during a run, e.g. a
   * `run_retry` event when the backend auto-resends after a temporary upstream
   * 5xx. Shown to the user as a brief notice; not persisted.
   */
  onNotice?: (message: string) => void
  /**
   * PRP-0110 / UDR-0088 D7: fired once when a send succeeds after a previous send
   * failed before committing -- i.e. the server came back. Rendered as a transient
   * "connection recovered" notice. There is NO proactive liveness monitor: the
   * signal is the user-initiated request itself (UDR-0088 D5).
   */
  onConnectionRecovered?: () => void
}

/**
 * Hook that communicates with the AG-UI endpoint directly via SSE.
 * When threadId is provided, sends only the new message (provider loads history).
 * When threadId is not provided, sends full message history (ephemeral mode).
 */
export function useChat(options?: UseChatOptions) {
  const [messages, setMessages] = useState<ChatMessage[]>(options?.initialMessages ?? [])
  const [isLoading, setIsLoading] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const threadIdRef = useRef(options?.threadId ?? crypto.randomUUID())
  const onStreamCompleteRef = useRef(options?.onStreamComplete)
  const onSessionCreatedRef = useRef(options?.onSessionCreated)
  const bgEnabledRef = useRef(options?.bgEnabled ?? false)
  const selectedModelRef = useRef(options?.selectedModel ?? '')
  const selectedModelOptionsRef = useRef<Record<string, string>>(options?.selectedModelOptions ?? {})
  const selectedOutputFormatRef = useRef(options?.selectedOutputFormat ?? 'none')
  const selectedOutputSchemaRef = useRef<Record<string, unknown> | null>(options?.selectedOutputSchema ?? null)
  const selectedImageOptionsRef = useRef<Record<string, string>>(options?.selectedImageOptions ?? {})
  const temporaryRef = useRef(options?.temporary ?? false)
  const selectedWorkflowIdRef = useRef(options?.selectedWorkflowId ?? '')
  const runTargetLabelRef = useRef(options?.runTargetLabel ?? '')
  const onCustomEventRef = useRef(options?.onCustomEvent)
  const onNoticeRef = useRef(options?.onNotice)
  const onConnectionRecoveredRef = useRef(options?.onConnectionRecovered)

  useEffect(() => {
    onConnectionRecoveredRef.current = options?.onConnectionRecovered
  }, [options?.onConnectionRecovered])

  useEffect(() => {
    if (options?.threadId) {
      threadIdRef.current = options.threadId
    }
  }, [options?.threadId])

  // Accept initial messages only when conversation is empty (async session load on page visit).
  // ThreadId changes cause ChatPanel remount via key prop, so this only handles
  // the case where initialMessages arrive after mount (e.g., /chat?session=xxx).
  useEffect(() => {
    if (options?.initialMessages && options.initialMessages.length > 0) {
      setMessages((prev) => (prev.length === 0 ? (options.initialMessages ?? []) : prev))
    }
  }, [options?.initialMessages])

  useEffect(() => {
    onStreamCompleteRef.current = options?.onStreamComplete
  }, [options?.onStreamComplete])

  useEffect(() => {
    onSessionCreatedRef.current = options?.onSessionCreated
  }, [options?.onSessionCreated])

  useEffect(() => {
    bgEnabledRef.current = options?.bgEnabled ?? false
  }, [options?.bgEnabled])

  useEffect(() => {
    selectedModelRef.current = options?.selectedModel ?? ''
  }, [options?.selectedModel])

  useEffect(() => {
    selectedModelOptionsRef.current = options?.selectedModelOptions ?? {}
  }, [options?.selectedModelOptions])

  useEffect(() => {
    selectedOutputFormatRef.current = options?.selectedOutputFormat ?? 'none'
  }, [options?.selectedOutputFormat])

  useEffect(() => {
    selectedOutputSchemaRef.current = options?.selectedOutputSchema ?? null
  }, [options?.selectedOutputSchema])

  useEffect(() => {
    selectedImageOptionsRef.current = options?.selectedImageOptions ?? {}
  }, [options?.selectedImageOptions])

  useEffect(() => {
    temporaryRef.current = options?.temporary ?? false
  }, [options?.temporary])

  useEffect(() => {
    selectedWorkflowIdRef.current = options?.selectedWorkflowId ?? ''
  }, [options?.selectedWorkflowId])

  useEffect(() => {
    runTargetLabelRef.current = options?.runTargetLabel ?? ''
  }, [options?.runTargetLabel])

  useEffect(() => {
    onCustomEventRef.current = options?.onCustomEvent
  }, [options?.onCustomEvent])

  useEffect(() => {
    onNoticeRef.current = options?.onNotice
  }, [options?.onNotice])

  const streamResponse = useCallback(
    async (
      userContent: string,
      currentMessages: ChatMessage[],
      options?: {
        skipUserMessage?: boolean
        images?: ImageRef[]
        resumeToken?: Record<string, unknown>
        modelOverride?: string
        // PRP-0073: async preparation that runs AFTER the optimistic user
        // bubble + assistant placeholder render (so the user sees their
        // message instantly) but BEFORE the agent dispatch. Used by mask-edit
        // to upload images in the background. Returns the durable image refs
        // to swap into the optimistic message (replacing local object URLs),
        // or null to abort the send with an error.
        prepare?: () => Promise<{ images?: ImageRef[] } | null>
      },
      // `committed` is true once the AG-UI stream emitted its first event (the turn
      // is live server-side); `success` is the pre-PRP-0110 stream-completed flag.
      // Only `committed` decides whether the composer keeps or restores the text.
    ): Promise<{ committed: boolean; success: boolean }> => {
      const userMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'user',
        content: userContent,
        createdAt: new Date().toISOString(),
        ...(options?.images && options.images.length > 0 ? { images: options.images } : {}),
      }

      const assistantId = crypto.randomUUID()
      const assistantMessage: ChatMessage = {
        id: assistantId,
        role: 'assistant',
        content: '',
        createdAt: new Date().toISOString(),
        // Run-target label (PRP-0118): which agent / workflow produced this turn. Known
        // client-side at send time (the modal decides it); shown in the action bar.
        ...(runTargetLabelRef.current ? { runTarget: runTargetLabelRef.current } : {}),
      }

      // PRP-0110 / UDR-0088 D3: the send is COMMITTED once the AG-UI SSE stream
      // has emitted its first event. Before that the turn is not durable
      // server-side, so a failure must hand the text back to the composer
      // instead of leaving it destroyed.
      let committed = false

      if (options?.skipUserMessage) {
        setMessages([...currentMessages, assistantMessage])
      } else {
        setMessages([...currentMessages, userMessage, assistantMessage])
      }
      setIsLoading(true)

      abortRef.current = new AbortController()
      let continuationTokenReceived = false
      let streamSuccess = true
      // Images dispatched to the agent + persisted. Starts as the optimistic
      // images and is replaced by prepare()'s durable refs when provided.
      let dispatchImages = options?.images

      try {
        // PRP-0073: run async preparation (e.g. mask-edit image uploads) now
        // that the optimistic user bubble is already on screen. On success,
        // swap the optimistic (local object URL) images for the durable
        // uploaded refs so dispatch, persistence, and reload all agree.
        if (options?.prepare) {
          const prepared = await options.prepare()
          if (!prepared) throw new Error('Failed to prepare message')
          dispatchImages = prepared.images
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === userMessage.id
                ? { ...msg, images: prepared.images && prepared.images.length > 0 ? prepared.images : undefined }
                : msg,
            ),
          )
        }

        // Initialize session file before agent processing (PRP-0025)
        // Creates the JSON file so the session ID is persisted early.
        // Temporary Chat (CTR-0107) skips init: init creates a sidebar-visible
        // .sessions entry, which a temporary chat must never have. The temp_
        // session is created lazily in the .temporary/ quarantine by save_messages.
        if (!options?.skipUserMessage && !options?.resumeToken && !temporaryRef.current) {
          const initTitle = userContent.slice(0, 100)
          const initStatus = await fetch(`/api/sessions/${threadIdRef.current}/init`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: initTitle }),
          })
            .then((r) => (r.ok ? (r.json() as Promise<{ status?: string }>) : null))
            .catch(() => null)
          // Show the new chat in the sidebar immediately (PRP-0077, CTR-0016),
          // without waiting for the AI answer. Only on first creation.
          if (initStatus?.status === 'created') {
            onSessionCreatedRef.current?.({ threadId: threadIdRef.current, title: initTitle })
          }
        }

        // Build AG-UI request state (CTR-0045 background, CTR-0070 model)
        const aguiState: Record<string, unknown> = {}
        const effectiveModel = options?.modelOverride || selectedModelRef.current
        if (effectiveModel) aguiState.model = effectiveModel
        if (Object.keys(selectedModelOptionsRef.current).length > 0)
          aguiState.model_options = selectedModelOptionsRef.current
        // Structured output (PRP-0082, UDR-0058 D3). An explicit schema rides in
        // state.output_schema; the generic mode (or a json_schema selection whose
        // schema is empty/invalid) rides in state.output_format. 'none' sends
        // nothing so the default path is byte-for-byte (UDR-0058 D7).
        {
          const of = selectedOutputFormatRef.current
          if (of === 'json_schema' && selectedOutputSchemaRef.current) {
            aguiState.output_schema = selectedOutputSchemaRef.current
          } else if (of === 'json_schema' || of === 'json_object') {
            aguiState.output_format = 'json_object'
          }
        }
        // Per-session image output options (PRP-0085, CTR-0120/CTR-0049). Only
        // non-default fields are present; absent = backend settings/API default.
        if (Object.keys(selectedImageOptionsRef.current).length > 0)
          aguiState.image_options = selectedImageOptionsRef.current
        if (bgEnabledRef.current) aguiState.background = true
        if (temporaryRef.current) aguiState.temporary = true
        // Declarative Workflow run-target (PRP-0118, CTR-0009, UDR-0101 D5). When set,
        // the backend streams the compiled workflow instead of the active agent; the
        // model / options / structured-output state above is ignored server-side (each
        // node's model + options come from its referenced Prompt agent, UDR-0101 D7).
        if (selectedWorkflowIdRef.current) aguiState.workflow_id = selectedWorkflowIdRef.current
        if (options?.resumeToken) aguiState.continuation_token = options.resumeToken

        // PRP-0069 follow-up: for regenerate / resume / similar flows
        // (skipUserMessage true), the user message we are responding to is
        // already in the truncated session and will be re-loaded by the
        // backend FileHistoryProvider.before_run on the next agent.run.
        // Sending it AGAIN in the request body causes the agent context to
        // contain the user message twice (history + iteration_messages),
        // which can stall reasoning models (e.g., gpt-5.5 + web_search) that
        // try to reconcile the apparent repetition. Send an empty messages
        // list instead and rely on the session history.
        const aguiMessages =
          options?.resumeToken || options?.skipUserMessage
            ? []
            : [
                {
                  id: userMessage.id,
                  role: 'user',
                  content: userContent,
                  ...(dispatchImages && dispatchImages.length > 0 ? { images: dispatchImages } : {}),
                },
              ]
        const response = await fetch('/ag-ui/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            thread_id: threadIdRef.current,
            run_id: crypto.randomUUID(),
            messages: aguiMessages,
            ...(Object.keys(aguiState).length > 0 ? { state: aguiState } : {}),
          }),
          signal: abortRef.current.signal,
        })

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}: ${response.statusText}`)
        }

        const reader = response.body?.getReader()
        if (!reader) throw new Error('No response body')

        const decoder = new TextDecoder()
        let buffer = ''
        let assistantContent = ''
        const completedReasoning: { id: string; content: string }[] = []
        const completedToolCalls: { id: string; name: string; status: string; args?: string; result?: string }[] = []
        const completedActivityLog: { type: string; id: string }[] = []
        let currentReasoningContent = ''
        let completedUsage: UsageInfo | undefined
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() ?? ''

          let eventType = ''

          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim()
              continue
            }

            if (!line.startsWith('data: ')) continue
            const data = line.slice(6).trim()
            if (!data) continue

            try {
              const event = JSON.parse(data) as AguiEvent
              // First event observed -> the turn is live server-side (UDR-0088 D3).
              committed = true

              switch (eventType || event.type) {
                case 'TEXT_MESSAGE_CONTENT': {
                  const delta = event.delta ?? ''
                  assistantContent += delta
                  setMessages((prev) =>
                    prev.map((msg) => (msg.id === assistantId ? { ...msg, content: msg.content + delta } : msg)),
                  )
                  break
                }
                case 'REASONING_MESSAGE_START': {
                  currentReasoningContent = ''
                  const reasoningBlock = {
                    id: event.messageId ?? crypto.randomUUID(),
                    content: '',
                    status: 'thinking' as const,
                  }
                  completedActivityLog.push({ type: 'reasoning', id: reasoningBlock.id })
                  setMessages((prev) =>
                    prev.map((msg) =>
                      msg.id === assistantId
                        ? {
                            ...msg,
                            reasoningBlocks: [...(msg.reasoningBlocks ?? []), reasoningBlock],
                            activityLog: [
                              ...(msg.activityLog ?? []),
                              { type: 'reasoning' as const, id: reasoningBlock.id },
                            ],
                          }
                        : msg,
                    ),
                  )
                  break
                }
                case 'REASONING_MESSAGE_CONTENT': {
                  const rId = event.messageId
                  const rDelta = event.delta ?? ''
                  currentReasoningContent += rDelta
                  setMessages((prev) =>
                    prev.map((msg) =>
                      msg.id === assistantId
                        ? {
                            ...msg,
                            reasoningBlocks: msg.reasoningBlocks?.map((rb) =>
                              rb.id === rId ? { ...rb, content: rb.content + rDelta } : rb,
                            ),
                          }
                        : msg,
                    ),
                  )
                  break
                }
                case 'REASONING_MESSAGE_END': {
                  const rEndId = event.messageId
                  completedReasoning.push({ id: rEndId ?? '', content: currentReasoningContent })
                  currentReasoningContent = ''
                  setMessages((prev) =>
                    prev.map((msg) =>
                      msg.id === assistantId
                        ? {
                            ...msg,
                            reasoningBlocks: msg.reasoningBlocks?.map((rb) =>
                              rb.id === rEndId ? { ...rb, status: 'done' as const } : rb,
                            ),
                          }
                        : msg,
                    ),
                  )
                  break
                }
                case 'TOOL_CALL_START': {
                  const tcId = event.toolCallId ?? crypto.randomUUID()
                  const tcName = event.toolCallName ?? 'unknown'
                  const toolCall = {
                    id: tcId,
                    name: tcName,
                    status: 'running' as const,
                    args: '',
                  }
                  completedToolCalls.push({ id: tcId, name: tcName, status: 'completed' })
                  completedActivityLog.push({ type: 'toolCall', id: tcId })
                  setMessages((prev) =>
                    prev.map((msg) =>
                      msg.id === assistantId
                        ? {
                            ...msg,
                            toolCalls: [...(msg.toolCalls ?? []), toolCall],
                            activityLog: [...(msg.activityLog ?? []), { type: 'toolCall' as const, id: tcId }],
                          }
                        : msg,
                    ),
                  )
                  break
                }
                case 'TOOL_CALL_ARGS': {
                  const argsId = event.toolCallId
                  const argsDelta = event.delta ?? ''
                  if (argsId) {
                    const entry = completedToolCalls.find((tc) => tc.id === argsId)
                    if (entry) entry.args = (entry.args ?? '') + argsDelta
                    setMessages((prev) =>
                      prev.map((msg) =>
                        msg.id === assistantId
                          ? {
                              ...msg,
                              toolCalls: msg.toolCalls?.map((tc) =>
                                tc.id === argsId ? { ...tc, args: (tc.args ?? '') + argsDelta } : tc,
                              ),
                            }
                          : msg,
                      ),
                    )
                  }
                  break
                }
                case 'TOOL_CALL_END': {
                  const endId = event.toolCallId
                  setMessages((prev) =>
                    prev.map((msg) =>
                      msg.id === assistantId
                        ? {
                            ...msg,
                            toolCalls: msg.toolCalls?.map((tc) =>
                              tc.id === endId ? { ...tc, status: 'completed' as const } : tc,
                            ),
                          }
                        : msg,
                    ),
                  )
                  break
                }
                case 'TOOL_CALL_RESULT': {
                  const resultTcId = event.toolCallId
                  const resultContent = event.content ?? ''
                  if (resultTcId) {
                    const entry = completedToolCalls.find((tc) => tc.id === resultTcId)
                    if (entry) entry.result = resultContent
                    setMessages((prev) =>
                      prev.map((msg) =>
                        msg.id === assistantId
                          ? {
                              ...msg,
                              toolCalls: msg.toolCalls?.map((tc) =>
                                tc.id === resultTcId ? { ...tc, result: resultContent } : tc,
                              ),
                            }
                          : msg,
                      ),
                    )
                  }
                  break
                }
                case 'RUN_ERROR': {
                  streamSuccess = false
                  const errorMsg = event.message ?? 'An error occurred'
                  setMessages((prev) =>
                    prev.map((msg) => (msg.id === assistantId ? { ...msg, content: `Error: ${errorMsg}` } : msg)),
                  )
                  break
                }
                case 'CUSTOM': {
                  // PRP-0067 / CTR-0100: forward every CUSTOM event to
                  // the optional handler before useChat acts on the ones
                  // it owns. This lets useToolApproval react to the
                  // tool_approval_request / tool_approval_response pair
                  // without duplicating SSE parsing.
                  onCustomEventRef.current?.(event.name, event.value)
                  if (event.name === 'run_retry' && event.value) {
                    // v0.77.1 (CTR-0009): the backend hit a transient upstream
                    // 5xx before any output and is auto-resending. Surface a
                    // brief status so the user knows the run is retrying, not
                    // stuck. The stream continues on the same assistant message.
                    const v = event.value as Record<string, unknown>
                    const attempt = typeof v.attempt === 'number' ? v.attempt : undefined
                    const max = typeof v.max_attempts === 'number' ? v.max_attempts : undefined
                    const counter = attempt && max ? ` (${attempt}/${max})` : ''
                    onNoticeRef.current?.(`Temporary server error -- retrying${counter}...`)
                  }
                  if (event.name === 'structured_output' && event.value) {
                    // PRP-0082 (CTR-0009 v14): this turn is structured -> render the
                    // answer as a JSON code block as it streams (UDR-0058 D5).
                    setMessages((prev) =>
                      prev.map((msg) => (msg.id === assistantId ? { ...msg, structured: true } : msg)),
                    )
                  }
                  if (event.name === 'usage' && event.value) {
                    completedUsage = event.value as UsageInfo
                    const usageModel = (event.value as Record<string, unknown>).model as string | undefined
                    const usageReasoning = (event.value as Record<string, unknown>).reasoning as string | undefined
                    const usageVerbosity = (event.value as Record<string, unknown>).verbosity as string | undefined
                    const usageStructured = (event.value as Record<string, unknown>).structured === true
                    setMessages((prev) =>
                      prev.map((msg) =>
                        msg.id === assistantId
                          ? {
                              ...msg,
                              usage: event.value as UsageInfo,
                              ...(usageModel ? { model: usageModel } : {}),
                              ...(usageReasoning ? { reasoning: usageReasoning } : {}),
                              ...(usageVerbosity ? { verbosity: usageVerbosity } : {}),
                              ...(usageStructured ? { structured: true } : {}),
                            }
                          : msg,
                      ),
                    )
                  }
                  if (event.name === 'continuation_token' && event.value) {
                    continuationTokenReceived = true
                    // Save continuation_token immediately for mid-stream resilience (PRP-0025)
                    fetch(`/api/sessions/${threadIdRef.current}/continuation-token`, {
                      method: 'PATCH',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ continuation_token: event.value }),
                    }).catch(() => {})
                  } else if (event.name === 'mcp_app' && event.value) {
                    // MCP Apps: associate UI metadata with the current assistant message (CTR-0068)
                    const mcpAppEvent = event.value as unknown as McpAppEvent
                    setMessages((prev) => {
                      const updated = [...prev]
                      const lastAssistant = [...updated].reverse().find((m: ChatMessage) => m.role === 'assistant')
                      if (lastAssistant) {
                        lastAssistant.mcpApp = mcpAppEvent
                      }
                      return updated
                    })
                  }
                  break
                }
              }
            } catch {
              // skip malformed JSON
            }
          }
        }

        // Save messages to session after stream completes
        if (assistantContent) {
          // Persist the message ids so identity is STABLE across reload (the
          // backend stores them as `message_id`; the loader restores them). Keeps
          // id-keyed state such as the Agent Memory per-turn like (CTR-0165) intact.
          const assistantMsg: Record<string, unknown> = {
            role: 'assistant',
            content: assistantContent,
            id: assistantId,
          }
          if (completedReasoning.length > 0) {
            assistantMsg.reasoning = completedReasoning
          }
          if (completedToolCalls.length > 0) {
            assistantMsg.tool_calls = completedToolCalls
          }
          if (completedActivityLog.length > 0) {
            assistantMsg.activity_log = completedActivityLog
          }
          if (completedUsage) {
            assistantMsg.usage = completedUsage
          }
          const userMsg: Record<string, unknown> = { role: 'user', content: userContent, id: userMessage.id }
          if (dispatchImages && dispatchImages.length > 0) {
            userMsg.images = dispatchImages
          }
          const saveMessages: Record<string, unknown>[] = options?.skipUserMessage
            ? [assistantMsg]
            : [userMsg, assistantMsg]
          fetch(`/api/sessions/${threadIdRef.current}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages: saveMessages }),
          })
            .then(() => onStreamCompleteRef.current?.())
            .catch(() => {})
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') {
          // The user pressed Stop. Treat as committed so the composer does not
          // resurrect a message the user deliberately cancelled (UDR-0088 D3).
          committed = true
        } else {
          streamSuccess = false
          const errorContent = error instanceof Error ? error.message : 'An unexpected error occurred'
          if (!committed && !options?.skipUserMessage) {
            // Pre-commit failure (server down / restarting / 401 before the first
            // event). The turn never reached the agent, so drop the empty assistant
            // placeholder and flag the USER turn instead: it carries the text, an
            // inline error, and a Retry affordance (CTR-0004 v2, UDR-0088 D3).
            // ChatInput restores the same text into the composer.
            setMessages((prev) =>
              prev
                .filter((msg) => msg.id !== assistantId)
                .map((msg) => (msg.id === userMessage.id ? { ...msg, failed: true } : msg)),
            )
          } else {
            setMessages((prev) =>
              prev.map((msg) => (msg.id === assistantId ? { ...msg, content: `Error: ${errorContent}` } : msg)),
            )
          }
        }
      } finally {
        // Close any reasoning blocks still in 'thinking' state (defensive:
        // handles abort/stop, stream errors, and missing REASONING_MESSAGE_END)
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantId && msg.reasoningBlocks?.some((rb) => rb.status === 'thinking')
              ? {
                  ...msg,
                  reasoningBlocks: msg.reasoningBlocks?.map((rb) =>
                    rb.status === 'thinking' ? { ...rb, status: 'done' as const } : rb,
                  ),
                }
              : msg,
          ),
        )

        setIsLoading(false)
        abortRef.current = null

        // Always clear continuation_token on completion (CTR-0045, PRP-0025)
        // Both success and error: token is no longer valid after stream ends
        if (continuationTokenReceived || options?.resumeToken) {
          fetch(`/api/sessions/${threadIdRef.current}/continuation-token`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ continuation_token: null }),
          }).catch(() => {})
        }
      }

      return { committed, success: streamSuccess }
    },
    [],
  )

  const messagesRef = useRef<ChatMessage[]>(options?.initialMessages ?? [])
  useEffect(() => {
    messagesRef.current = messages
  }, [messages])

  /**
   * PRP-0110 / UDR-0088 D3+D7: true while the last send failed before it
   * committed, so a subsequent success can announce "connection recovered".
   */
  const hadPrecommitFailureRef = useRef(false)

  const sendMessage = useCallback(
    async (
      content: string,
      images?: ImageRef[],
      opts?: { prepare?: () => Promise<{ images?: ImageRef[] } | null> },
    ): Promise<boolean> => {
      // Nothing to send -> report "committed" so the composer does not try to
      // restore an empty string.
      if (!content.trim() && (!images || images.length === 0) && !opts?.prepare) return true
      const { committed } = await streamResponse(content.trim(), messagesRef.current, {
        images,
        prepare: opts?.prepare,
      })
      if (!committed) {
        hadPrecommitFailureRef.current = true
      } else if (hadPrecommitFailureRef.current) {
        hadPrecommitFailureRef.current = false
        onConnectionRecoveredRef.current?.()
      }
      return committed
    },
    [streamResponse],
  )

  /**
   * Re-send a user turn that failed before it committed (CTR-0004 v2). The turn
   * was never persisted server-side, so we simply drop the failed bubble and
   * stream it again -- no truncate call, no replay of a request the server may
   * have already accepted (UDR-0088 D6).
   */
  const retryTurn = useCallback(
    async (messageId: string) => {
      const current = messagesRef.current
      const idx = current.findIndex((m) => m.id === messageId)
      if (idx === -1) return
      const failed = current[idx]
      if (failed.role !== 'user' || !failed.failed) return
      const truncated = current.slice(0, idx)
      setMessages(truncated)
      const { committed } = await streamResponse(failed.content, truncated, {
        images: failed.images,
      })
      if (!committed) {
        hadPrecommitFailureRef.current = true
      } else if (hadPrecommitFailureRef.current) {
        hadPrecommitFailureRef.current = false
        onConnectionRecoveredRef.current?.()
      }
    },
    [streamResponse],
  )

  const editUserMessage = useCallback(
    async (messageId: string, newContent: string) => {
      const current = messagesRef.current
      const idx = current.findIndex((m) => m.id === messageId)
      if (idx === -1) return

      const truncated = current.slice(0, idx)
      // Preserve any images attached to the original user message so editing the
      // text does not drop the attachments from the re-sent turn.
      const originalImages = current[idx].images

      // Truncate backend session
      // Await the truncate so the backend session file is persisted BEFORE
      // streamResponse triggers POST /ag-ui/ -> before_run reads the file.
      // Otherwise the two requests race and before_run can load stale,
      // un-truncated history (duplicate / out-of-order turns), which the
      // Azure OpenAI Responses API can reject mid-stream.
      await fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ after_index: idx > 0 ? idx - 1 : 0, delete_from: idx }),
      }).catch(() => {})

      await streamResponse(
        newContent,
        truncated,
        originalImages && originalImages.length > 0 ? { images: originalImages } : undefined,
      )
    },
    [streamResponse],
  )

  const regenerateAssistantMessage = useCallback(
    async (messageId: string) => {
      const current = messagesRef.current
      const idx = current.findIndex((m) => m.id === messageId)
      if (idx === -1) return

      // Find the preceding user message
      let userContent = ''
      for (let i = idx - 1; i >= 0; i--) {
        if (current[i].role === 'user') {
          userContent = current[i].content
          break
        }
      }
      if (!userContent) return

      // Keep messages up to (but not including) this assistant message
      const truncated = current.slice(0, idx)

      // Truncate backend session (remove only this assistant message)
      // Await the truncate so the backend session file is persisted BEFORE
      // streamResponse triggers POST /ag-ui/ -> before_run reads the file.
      // Otherwise the two requests race and before_run can load stale,
      // un-truncated history (duplicate / out-of-order turns), which the
      // Azure OpenAI Responses API can reject mid-stream.
      await fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ after_index: idx > 0 ? idx - 1 : 0, delete_from: idx }),
      }).catch(() => {})

      // Re-stream without adding a new user message (user message already in truncated)
      await streamResponse(userContent, truncated, { skipUserMessage: true })
    },
    [streamResponse],
  )

  /** Regenerate with a specific model (CTR-0071, PRP-0035). */
  const regenerateWithModel = useCallback(
    async (messageId: string, model: string) => {
      const current = messagesRef.current
      const idx = current.findIndex((m) => m.id === messageId)
      if (idx === -1) return

      let userContent = ''
      for (let i = idx - 1; i >= 0; i--) {
        if (current[i].role === 'user') {
          userContent = current[i].content
          break
        }
      }
      if (!userContent) return

      const truncated = current.slice(0, idx)

      // Await the truncate so the backend session file is persisted BEFORE
      // streamResponse triggers POST /ag-ui/ -> before_run reads the file.
      // Otherwise the two requests race and before_run can load stale,
      // un-truncated history (duplicate / out-of-order turns), which the
      // Azure OpenAI Responses API can reject mid-stream.
      await fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ after_index: idx > 0 ? idx - 1 : 0, delete_from: idx }),
      }).catch(() => {})

      await streamResponse(userContent, truncated, { skipUserMessage: true, modelOverride: model })
    },
    [streamResponse],
  )

  const deleteMessage = useCallback((messageId: string) => {
    const current = messagesRef.current
    const idx = current.findIndex((m) => m.id === messageId)
    if (idx === -1) return

    setMessages((prev) => prev.filter((m) => m.id !== messageId))

    fetch(`/api/sessions/${threadIdRef.current}/messages/${idx}`, {
      method: 'DELETE',
    })
      .then(() => onStreamCompleteRef.current?.())
      .catch(() => {})
  }, [])

  const editAssistantMessage = useCallback((messageId: string, newContent: string) => {
    setMessages((prev) => prev.map((msg) => (msg.id === messageId ? { ...msg, content: newContent } : msg)))

    // Update backend session - we need to find the index and rewrite
    // For simplicity, save the updated content by truncating and re-saving
    const current = messagesRef.current
    const idx = current.findIndex((m) => m.id === messageId)
    if (idx === -1) return

    // Preserve everything except the text (in-place edit): generated images and
    // tool results (toolCalls), attached images, reasoning, the activity log
    // ordering, and usage. Re-saving content alone previously dropped tool_calls
    // (generated images) and attachments, so they vanished on reload.
    const original = current[idx]
    const assistantMsg: Record<string, unknown> = { role: 'assistant', content: newContent, id: original.id }
    if (original.reasoningBlocks && original.reasoningBlocks.length > 0) {
      assistantMsg.reasoning = original.reasoningBlocks.map((r) => ({ id: r.id, content: r.content }))
    }
    if (original.toolCalls && original.toolCalls.length > 0) {
      assistantMsg.tool_calls = original.toolCalls.map((t) => ({
        id: t.id,
        name: t.name,
        status: t.status,
        ...(t.args != null ? { args: t.args } : {}),
        ...(t.result != null ? { result: t.result } : {}),
      }))
    }
    if (original.activityLog && original.activityLog.length > 0) {
      assistantMsg.activity_log = original.activityLog.map((a) => ({ type: a.type, id: a.id }))
    }
    if (original.images && original.images.length > 0) {
      assistantMsg.images = original.images.map((im) => ({ uri: im.uri, media_type: im.media_type }))
    }
    if (original.usage) {
      assistantMsg.usage = original.usage
    }

    fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ after_index: idx > 0 ? idx - 1 : 0, delete_from: idx }),
    })
      .then(() =>
        fetch(`/api/sessions/${threadIdRef.current}/messages`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: [assistantMsg] }),
        }),
      )
      .catch(() => {})
  }, [])

  // Resume from continuation_token (CTR-0044, PRP-0025)
  // Token clearing and result notification are handled in streamResponse's finally block
  const resumeFromToken = useCallback(
    async (token: Record<string, unknown>): Promise<boolean> => {
      // Callers use this to distinguish "background response resumed" from
      // "expired", i.e. whether the STREAM completed -- not whether it committed.
      const { success } = await streamResponse('', messagesRef.current, {
        skipUserMessage: true,
        resumeToken: token,
      })
      return success
    },
    [streamResponse],
  )

  const stopGeneration = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  const clearMessages = useCallback(() => {
    setMessages([])
  }, [])

  return {
    messages,
    isLoading,
    sendMessage,
    retryTurn,
    stopGeneration,
    clearMessages,
    editUserMessage,
    regenerateAssistantMessage,
    regenerateWithModel,
    editAssistantMessage,
    deleteMessage,
    resumeFromToken,
  }
}
