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
  bgEnabled?: boolean
  selectedModel?: string
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
  const bgEnabledRef = useRef(options?.bgEnabled ?? false)
  const selectedModelRef = useRef(options?.selectedModel ?? '')

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
    bgEnabledRef.current = options?.bgEnabled ?? false
  }, [options?.bgEnabled])

  useEffect(() => {
    selectedModelRef.current = options?.selectedModel ?? ''
  }, [options?.selectedModel])

  const streamResponse = useCallback(
    async (
      userContent: string,
      currentMessages: ChatMessage[],
      options?: {
        skipUserMessage?: boolean
        images?: ImageRef[]
        resumeToken?: Record<string, unknown>
        modelOverride?: string
      },
    ): Promise<boolean> => {
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
      }

      if (options?.skipUserMessage) {
        setMessages([...currentMessages, assistantMessage])
      } else {
        setMessages([...currentMessages, userMessage, assistantMessage])
      }
      setIsLoading(true)

      abortRef.current = new AbortController()
      let continuationTokenReceived = false
      let streamSuccess = true

      try {
        // Initialize session file before agent processing (PRP-0025)
        // Creates the JSON file so the session ID is persisted early
        if (!options?.skipUserMessage && !options?.resumeToken) {
          await fetch(`/api/sessions/${threadIdRef.current}/init`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: userContent.slice(0, 100) }),
          }).catch(() => {})
        }

        // Build AG-UI request state (CTR-0045 background, CTR-0070 model)
        const aguiState: Record<string, unknown> = {}
        const effectiveModel = options?.modelOverride || selectedModelRef.current
        if (effectiveModel) aguiState.model = effectiveModel
        if (bgEnabledRef.current) aguiState.background = true
        if (options?.resumeToken) aguiState.continuation_token = options.resumeToken

        const response = await fetch('/ag-ui/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            thread_id: threadIdRef.current,
            run_id: crypto.randomUUID(),
            messages: options?.resumeToken
              ? []
              : [
                  {
                    id: userMessage.id,
                    role: 'user',
                    content: userContent,
                    ...(options?.images && options.images.length > 0 ? { images: options.images } : {}),
                  },
                ],
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
                  if (event.name === 'usage' && event.value) {
                    completedUsage = event.value as UsageInfo
                    const usageModel = (event.value as Record<string, unknown>).model as string | undefined
                    setMessages((prev) =>
                      prev.map((msg) =>
                        msg.id === assistantId
                          ? { ...msg, usage: event.value as UsageInfo, ...(usageModel ? { model: usageModel } : {}) }
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
          const assistantMsg: Record<string, unknown> = { role: 'assistant', content: assistantContent }
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
          const userMsg: Record<string, unknown> = { role: 'user', content: userContent }
          if (options?.images && options.images.length > 0) {
            userMsg.images = options.images
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
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          streamSuccess = false
          const errorContent = error instanceof Error ? error.message : 'An unexpected error occurred'
          setMessages((prev) =>
            prev.map((msg) => (msg.id === assistantId ? { ...msg, content: `Error: ${errorContent}` } : msg)),
          )
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

      return streamSuccess
    },
    [],
  )

  const messagesRef = useRef<ChatMessage[]>(options?.initialMessages ?? [])
  useEffect(() => {
    messagesRef.current = messages
  }, [messages])

  const sendMessage = useCallback(
    async (content: string, images?: ImageRef[]) => {
      if (!content.trim() && (!images || images.length === 0)) return
      await streamResponse(content.trim(), messagesRef.current, { images })
    },
    [streamResponse],
  )

  const editUserMessage = useCallback(
    async (messageId: string, newContent: string) => {
      const current = messagesRef.current
      const idx = current.findIndex((m) => m.id === messageId)
      if (idx === -1) return

      const truncated = current.slice(0, idx)

      // Truncate backend session
      fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ after_index: idx > 0 ? idx - 1 : 0, delete_from: idx }),
      }).catch(() => {})

      await streamResponse(newContent, truncated)
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
      fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
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

      fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
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

    fetch(`/api/sessions/${threadIdRef.current}/truncate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ after_index: idx > 0 ? idx - 1 : 0, delete_from: idx }),
    })
      .then(() =>
        fetch(`/api/sessions/${threadIdRef.current}/messages`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            messages: [{ role: 'assistant', content: newContent }],
          }),
        }),
      )
      .catch(() => {})
  }, [])

  // Resume from continuation_token (CTR-0044, PRP-0025)
  // Token clearing and result notification are handled in streamResponse's finally block
  const resumeFromToken = useCallback(
    async (token: Record<string, unknown>): Promise<boolean> => {
      return streamResponse('', messagesRef.current, { skipUserMessage: true, resumeToken: token })
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
