import { ImageIcon } from 'lucide-react'
import { type DragEvent, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BackgroundResponsesToggle } from '@/components/BackgroundResponsesToggle'
import { ChatInput, type ChatInputHandle } from '@/components/ChatInput'
import { ChatMessageItem } from '@/components/ChatMessageItem'
import { ContextWindowIndicator } from '@/components/ContextWindowIndicator'
import { HelpPortal } from '@/components/HelpPortal'
import { ImageOutputOptions } from '@/components/ImageOutputOptions'
import { MaskEditorDialog } from '@/components/MaskEditorDialog'
import { McpToolManager } from '@/components/McpToolManager'
import { MessageNavigator } from '@/components/MessageNavigator'
import { MessageStepButton } from '@/components/MessageStepButton'
import { ModelOptionsSelector } from '@/components/ModelOptionsSelector'
import { ModelSelector, type ModelSelectorHandle } from '@/components/ModelSelector'
import { ScrollToBottomButton } from '@/components/ScrollToBottomButton'
import { SkillsManager } from '@/components/SkillsManager'
import { StructuredOutputControl, type StructuredSelection } from '@/components/StructuredOutputControl'
import { ToolApprovalList } from '@/components/ToolApprovalCard'
import { PromptTemplatesModal } from '@/components/templates/PromptTemplatesModal'
import { SaveAsTemplateDialog } from '@/components/templates/SaveAsTemplateDialog'
import { useChat } from '@/hooks/useChat'
import { useChatScroll } from '@/hooks/useChatScroll'
import { type ImageAttachment, useImageAttachment } from '@/hooks/useImageAttachment'
import { useMemoryCuration } from '@/hooks/useMemoryCuration'
import { useMessageNavigator } from '@/hooks/useMessageNavigator'
import { useMessageStepNav } from '@/hooks/useMessageStepNav'
import { useTemplates } from '@/hooks/useTemplates'
import { useToolApproval } from '@/hooks/useToolApproval'
import { useTTS } from '@/hooks/useTTS'
import { lazyWithReload } from '@/lib/lazy-with-reload'
import { cn } from '@/lib/utils'
import type { ChatMessage, ImageRef } from '@/types/chat'

const BG_STORAGE_KEY = 'chatwalaau-bg-enabled'

// Lazy-loaded so the fabric.js bundle is fetched only when the Paint editor is
// first opened (CTR-0160, UDR-0078 D1). lazyWithReload recovers from a stale
// chunk hash after a rebuild/redeploy.
const PaintEditor = lazyWithReload(() => import('@/components/PaintEditor').then((m) => ({ default: m.PaintEditor })))

interface ChatPanelProps {
  compact?: boolean
  emptyMessage?: string
  className?: string
  threadId?: string
  initialMessages?: ChatMessage[]
  continuationToken?: Record<string, unknown> | null
  onStreamComplete?: () => void
  /** New-session created (PRP-0077, CTR-0016): show it in the sidebar immediately. */
  onSessionCreated?: (info: { threadId: string; title: string }) => void
  onBranchFromMessage?: (messageIndex: number) => void
  /** Slash command /cron (CTR-0135, PRP-0089): open the Cron scheduler portal. */
  onSlashCron?: () => void
  /** Slash command /files (CTR-0137, PRP-0091): open the File Explorer overlay. */
  onSlashFiles?: () => void
  /** Temporary Chat mode (CTR-0107, PRP-0076): dark input, no BG toggle, no history. */
  temporary?: boolean
}

function buildStreamingKey(messages: ChatMessage[], isLoading: boolean): string {
  const lastMsg = messages.at(-1)
  const toolCallCount = lastMsg?.toolCalls?.length ?? 0
  const lastToolStatus = lastMsg?.toolCalls?.at(-1)?.status ?? ''
  return `${messages.length}:${lastMsg?.content?.length ?? 0}:${toolCallCount}:${lastToolStatus}:${lastMsg?.reasoningBlocks?.length ?? 0}:${isLoading}`
}

/**
 * Stable per-turn key for the Agent Memory like (CTR-0165), derived from the
 * turn's CONTENT (user + assistant text) via FNV-1a, NOT the ephemeral message id.
 * A content hash is identical at like-time and after reloading a past chat, so the
 * liked state matches reliably. The length prefix reduces the (already tiny)
 * collision chance between short identical turns.
 */
function fnv1a(str: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16)
}
function turnKeyFromContent(userText: string, assistantText: string): string {
  const u = userText.trim()
  const a = assistantText.trim()
  return `t_${u.length}_${a.length}_${fnv1a(`${u}␟${a}`)}`
}

export function ChatPanel({
  compact,
  emptyMessage = 'How can I help you today?',
  className,
  threadId,
  initialMessages,
  continuationToken,
  onStreamComplete,
  onSessionCreated,
  onBranchFromMessage,
  onSlashCron,
  onSlashFiles,
  temporary = false,
}: ChatPanelProps) {
  const [bgEnabled, setBgEnabled] = useState(() => localStorage.getItem(BG_STORAGE_KEY) === 'true')
  const [notification, setNotification] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null)
  const [selectedModel, setSelectedModel] = useState('')
  // Per-message generation options (effort + verbosity), catalog-driven
  // (CTR-0071, PRP-0081). Sent as AG-UI state.model_options.
  const [selectedModelOptions, setSelectedModelOptions] = useState<Record<string, string>>({})
  // Structured output selection (CTR-0118, PRP-0082). Sent as AG-UI
  // state.output_schema / state.output_format.
  const [structured, setStructured] = useState<StructuredSelection>({ format: 'none', schema: null })
  // Image output options (CTR-0120, PRP-0085). Sent as AG-UI state.image_options.
  const [selectedImageOptions, setSelectedImageOptions] = useState<Record<string, string>>({})
  const [modelMaxTokens, setModelMaxTokens] = useState(128000)
  const [availableModels, setAvailableModels] = useState<string[]>([])
  // CTR-0045 / PRP-0073: per-model background-response capability. The toggle
  // is disabled for models whose provider does not support background runs
  // (e.g. Anthropic Opus 4.7/4.8); GPT / Azure OpenAI models support it.
  const [bgSupportedMap, setBgSupportedMap] = useState<Record<string, boolean>>({})

  // Default to supported when the map has no entry for the model (keeps GPT
  // working before the map loads and avoids hiding the feature on unknowns).
  const bgSupported = selectedModel ? (bgSupportedMap[selectedModel] ?? true) : true
  // Never send background=true for an unsupported model even if localStorage
  // had it enabled from a previous GPT session.
  const effectiveBgEnabled = bgEnabled && bgSupported

  const handleBgToggle = useCallback((enabled: boolean) => {
    setBgEnabled(enabled)
    localStorage.setItem(BG_STORAGE_KEY, String(enabled))
  }, [])

  const handleModelChange = useCallback((model: string, maxTokens: number) => {
    setSelectedModel(model)
    setModelMaxTokens(maxTokens)
  }, [])

  const handleModelOptionsChange = useCallback((opts: Record<string, string>) => {
    setSelectedModelOptions(opts)
  }, [])

  const handleStructuredChange = useCallback((selection: StructuredSelection) => {
    setStructured(selection)
  }, [])

  const handleImageOptionsChange = useCallback((opts: Record<string, string>) => {
    setSelectedImageOptions(opts)
  }, [])

  // Auto-dismiss notification
  useEffect(() => {
    if (notification) {
      const t = setTimeout(() => setNotification(null), 5000)
      return () => clearTimeout(t)
    }
  }, [notification])

  const handleResumeResult = useCallback((success: boolean) => {
    if (success) {
      setNotification({ type: 'success', message: 'Background response resumed' })
    } else {
      setNotification({ type: 'error', message: 'Background response expired. Please resend your message.' })
    }
  }, [])

  // PRP-0067 / CTR-0100: tool approval state lives outside useChat so
  // both ChatPanel and the SSE handler can read it. The hook is also
  // responsible for resetting state on session switch (the parent key={threadId}
  // remount handles that automatically here).
  const approvalApi = useToolApproval()

  const {
    messages,
    isLoading,
    sendMessage,
    stopGeneration,
    editUserMessage,
    regenerateAssistantMessage,
    regenerateWithModel,
    editAssistantMessage,
    deleteMessage,
    resumeFromToken,
  } = useChat({
    threadId,
    initialMessages,
    onStreamComplete,
    onSessionCreated,
    bgEnabled: effectiveBgEnabled,
    selectedModel,
    selectedModelOptions,
    selectedOutputFormat: structured.format,
    selectedOutputSchema: structured.schema,
    selectedImageOptions,
    temporary,
    onCustomEvent: approvalApi.ingestCustomEvent,
    // v0.77.1: transient upstream 5xx auto-retry status (CTR-0009). Shown as a
    // brief amber banner so the user knows the run is being resent, not stalled.
    onNotice: useCallback((message: string) => setNotification({ type: 'info', message }), []),
  })

  // Auto-resume from continuation_token (page reload or sidebar switch).
  // Uses ref for resume/notify to keep dependency array minimal.
  // No "attempted" flag — React 18 StrictMode double-fires mount effects,
  // so we rely on cleanup (clearTimeout) + re-set pattern instead.
  const resumeRef = useRef({ resume: resumeFromToken, notify: handleResumeResult })
  resumeRef.current.resume = resumeFromToken
  resumeRef.current.notify = handleResumeResult

  useEffect(() => {
    if (!continuationToken) return
    const token = continuationToken
    const timer = setTimeout(async () => {
      const success = await resumeRef.current.resume(token)
      resumeRef.current.notify(success)
    }, 800)
    return () => clearTimeout(timer)
  }, [continuationToken])

  const { attachments, addFiles, attachPaintImage, removeAttachment, clearAttachments, getImageRefs, isUploading } =
    useImageAttachment()

  // Paint editor (CTR-0160 / CTR-0161, PRP-0099). State is lifted here so the
  // Plus-menu entry, the pending-attachment Edit affordance, and the sent-image
  // (history) re-edit all drive ONE editor instance. `scene` seeds a re-edit;
  // `replaceId` (set only for a pending attachment) swaps it in place on attach.
  const [paintState, setPaintState] = useState<{ scene?: unknown; replaceId?: string } | null>(null)

  const handleOpenPaint = useCallback(() => setPaintState({}), [])

  const loadScene = useCallback(
    async (filename: string | undefined): Promise<unknown> => {
      if (!threadId || !filename) return undefined
      try {
        const res = await fetch(`/api/paint/${threadId}/${encodeURIComponent(filename)}`)
        return res.ok ? await res.json() : undefined
      } catch {
        return undefined
      }
    },
    [threadId],
  )

  // Re-edit a pending paint attachment (replaces it in place on attach).
  const handleEditAttachment = useCallback(
    async (attachment: ImageAttachment) => {
      const scene = await loadScene(attachment.filename)
      setPaintState({ scene, replaceId: attachment.id })
    },
    [loadScene],
  )

  // Re-edit a sent (history) paint image. Message history is immutable, so the
  // result becomes a NEW attachment on the composer (UDR-0078 D6).
  const handlePaintEditFromHistory = useCallback(
    async (imageUrl: string) => {
      const filename = imageUrl.split('/').pop()
      const scene = await loadScene(filename)
      setPaintState({ scene })
    },
    [loadScene],
  )

  const handlePaintAttach = useCallback(
    (blob: Blob, scene: unknown) => {
      if (!threadId) return
      void attachPaintImage(blob, scene, threadId, paintState?.replaceId)
      setPaintState(null)
    },
    [threadId, attachPaintImage, paintState],
  )

  const tts = useTTS()

  // Slash commands (CTR-0128, PRP-0088): /model drives the selector via an
  // imperative handle; /help opens the Help Portal.
  const modelSelectorRef = useRef<ModelSelectorHandle>(null)
  const [helpOpen, setHelpOpen] = useState(false)
  const handleSlashModel = useCallback((model: string) => modelSelectorRef.current?.selectModel(model) ?? false, [])
  const handleSlashHelp = useCallback(() => setHelpOpen(true), [])

  // Prompt Templates state (CTR-0048, PRP-0026)
  const chatInputRef = useRef<ChatInputHandle>(null)
  const [templatesModalOpen, setTemplatesModalOpen] = useState(false)
  const [saveAsDialogOpen, setSaveAsDialogOpen] = useState(false)
  const [saveAsBody, setSaveAsBody] = useState('')
  const { createTemplate } = useTemplates()

  const handleOpenTemplates = useCallback(() => setTemplatesModalOpen(true), [])

  const handleInsertTemplate = useCallback((body: string) => {
    chatInputRef.current?.insertText(body)
  }, [])

  const handleSaveAsTemplate = useCallback((content: string) => {
    setSaveAsBody(content)
    setSaveAsDialogOpen(true)
  }, [])

  // Mask Editor state (CTR-0052, PRP-0028)
  const [maskEditorState, setMaskEditorState] = useState<{ imageUrl: string } | null>(null)

  const handleMaskEdit = useCallback((imageUrl: string) => {
    setMaskEditorState({ imageUrl })
  }, [])

  const handleMaskGenerate = useCallback(
    async (compositedBlob: Blob, previewBlob: Blob, prompt: string) => {
      setMaskEditorState(null)
      if (!threadId) return

      // PRP-0073: render the user message immediately, then upload in the
      // background. The composited filename is already disk/URL-safe, so the
      // dispatched instruction can be built up-front (the upload stores it
      // verbatim) -- no need to wait for the upload response before showing
      // the bubble. The preview shows a local object URL instantly and is
      // swapped for the durable uploaded URI by the prepare() hook.
      const compositedFilename = `mask_source_${Date.now()}.png`
      const instruction = `Edit the masked areas of the image "${compositedFilename}": ${prompt}`
      const previewObjectUrl = URL.createObjectURL(previewBlob)

      await sendMessage(instruction, [{ uri: previewObjectUrl, media_type: 'image/png' }], {
        prepare: async () => {
          try {
            // Upload composited source (edit regions) + display preview in
            // parallel so the agent can read the source as soon as possible.
            const compositedForm = new FormData()
            compositedForm.append('file', new File([compositedBlob], compositedFilename, { type: 'image/png' }))
            const previewForm = new FormData()
            previewForm.append('file', new File([previewBlob], 'mask_preview.png', { type: 'image/png' }))

            const [compositedRes, previewRes] = await Promise.all([
              fetch(`/api/upload/${threadId}`, { method: 'POST', body: compositedForm }),
              fetch(`/api/upload/${threadId}`, { method: 'POST', body: previewForm }),
            ])

            const compositedData = compositedRes.ok ? await compositedRes.json() : null
            const previewData = previewRes.ok ? await previewRes.json() : null

            if (!compositedData?.filename) {
              setNotification({ type: 'error', message: 'Failed to upload source image' })
              return null
            }

            const images: ImageRef[] = previewData?.uri ? [{ uri: previewData.uri, media_type: 'image/png' }] : []
            return { images }
          } catch (err) {
            setNotification({
              type: 'error',
              message: err instanceof Error ? err.message : 'Failed to start mask edit',
            })
            return null
          }
        },
      })

      URL.revokeObjectURL(previewObjectUrl)
    },
    [threadId, sendMessage],
  )

  const [isDragging, setIsDragging] = useState(false)
  const dragCountRef = useRef(0)
  // Fetch model info for single-model fallback and available models list
  useEffect(() => {
    fetch('/api/model')
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.max_context_tokens) setModelMaxTokens((prev) => (prev === 128000 ? data.max_context_tokens : prev))
        if (data?.models) setAvailableModels(data.models)
        if (data?.background_supported_map) setBgSupportedMap(data.background_supported_map)
      })
      .catch(() => {})
  }, [])

  // CTR-0092 Chat Scroll Behavior (PRP-0055): autoscroll suspend on user
  // intent, ScrollToBottom affordance, and bottom spacer sized by the
  // observed ChatInput height.
  const streamingKey = buildStreamingKey(messages, isLoading)
  const { scrollRef, inputRef, showScrollToBottomButton, bottomSpacerHeightPx, scrollToBottom } =
    useChatScroll(streamingKey)

  // CTR-0103 Message Navigator (PRP-0072): user-turn index rail + popover.
  // Full-page /chat only (non-compact); availability is gated by the measured
  // right gutter and the user-turn count inside the hook.
  const messageNav = useMessageNavigator(scrollRef, messages, { enabled: !compact })

  // CTR-0168 Message Step Navigation (PRP-0101 / UDR-0081): per-message prev/next
  // buttons flanking the ScrollToBottom button, shown whenever the container
  // overflows (independent of the CTR-0092 near-bottom gate).
  const stepNav = useMessageStepNav(scrollRef, messages)

  // Agent Memory curation (CTR-0165, PRP-0100). The "remember this turn" like on
  // each message toggles the same turn (user + assistant as one set). Resolve a
  // message index to its turn via a messages ref so onToggleMemoryLike stays
  // referentially stable (PRP-0074 memoization); a like is only offered for a
  // COMPLETE turn that has an assistant reply with text.
  //
  // turn_key is derived from the turn's CONTENT (a stable hash of the user +
  // assistant text), NOT the ephemeral message id. Message ids are regenerated
  // per render/reload unless persisted, so a content-derived key is what makes the
  // liked state match reliably after reloading a past chat -- independent of the
  // message-id plumbing and of any timing.
  const memory = useMemoryCuration(threadId)
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const resolveTurn = useCallback((index: number) => {
    const msgs = messagesRef.current
    const msg = msgs[index]
    if (!msg) return null
    let assistantIdx = -1
    if (msg.role === 'assistant') {
      assistantIdx = index
    } else if (msg.role === 'user') {
      for (let j = index + 1; j < msgs.length; j++) {
        if (msgs[j].role === 'assistant') {
          assistantIdx = j
          break
        }
        if (msgs[j].role === 'user') break
      }
    }
    if (assistantIdx < 0) return null
    const assistant = msgs[assistantIdx]
    if (!assistant.content || !assistant.content.trim()) return null
    let userText = ''
    for (let j = assistantIdx - 1; j >= 0; j--) {
      if (msgs[j].role === 'user') {
        userText = msgs[j].content
        break
      }
    }
    return { turnKey: turnKeyFromContent(userText, assistant.content), userText, assistantText: assistant.content }
  }, [])
  const selectedModelRef = useRef(selectedModel)
  selectedModelRef.current = selectedModel
  const handleToggleMemoryLike = useCallback(
    (index: number) => {
      const turn = resolveTurn(index)
      if (!turn) return
      memory.toggle(turn.turnKey, {
        userText: turn.userText,
        assistantText: turn.assistantText,
        model: selectedModelRef.current,
      })
    },
    [resolveTurn, memory],
  )

  const latestUsage = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].usage) return messages[i].usage
    }
    return undefined
  }, [messages])

  const handleSend = useCallback(
    (content: string, images?: ImageRef[]) => {
      sendMessage(content, images)
      clearAttachments()
      // PRP-0058 UX-2: user-send is the strongest "follow output" intent.
      // Force-resume autoscroll so the new user message + assistant stream
      // re-anchor at the bottom even if the operator had previously
      // scrolled up to read earlier text (autoscrollRef was false).
      scrollToBottom()
    },
    [sendMessage, clearAttachments, scrollToBottom],
  )

  const handleAddFiles = useCallback(
    (files: FileList) => {
      if (threadId) addFiles(files, threadId)
    },
    [addFiles, threadId],
  )

  const handleDragEnter = useCallback((e: DragEvent) => {
    e.preventDefault()
    dragCountRef.current++
    if (e.dataTransfer.types.includes('Files')) {
      setIsDragging(true)
    }
  }, [])

  const handleDragLeave = useCallback((e: DragEvent) => {
    e.preventDefault()
    dragCountRef.current--
    if (dragCountRef.current === 0) {
      setIsDragging(false)
    }
  }, [])

  const handleDragOver = useCallback((e: DragEvent) => {
    e.preventDefault()
  }, [])

  const handleDrop = useCallback(
    (e: DragEvent) => {
      e.preventDefault()
      dragCountRef.current = 0
      setIsDragging(false)
      const files = e.dataTransfer.files
      if (files.length > 0 && threadId) {
        addFiles(files, threadId)
      }
    },
    [addFiles, threadId],
  )

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: drag-and-drop drop zone requires drag events on container div
    <div
      className={cn('relative flex flex-1 flex-col overflow-hidden', className)}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}>
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div className={cn('mx-auto px-4 pt-4', compact ? 'max-w-full pb-4' : 'max-w-3xl')}>
          {messages.length === 0 && (
            <div
              className={cn('flex items-center justify-center text-muted-foreground', compact ? 'h-40' : 'h-[60vh]')}>
              <p className={cn(compact ? 'text-xs' : 'text-sm')}>{emptyMessage}</p>
            </div>
          )}
          {messages.map((msg, i) => {
            // CTR-0165: offer the "remember this turn" like only for a complete
            // turn (has an assistant reply) and only when the feature is enabled.
            const turn = memory.enabled ? resolveTurn(i) : null
            return (
              <ChatMessageItem
                key={msg.id}
                message={msg}
                messageIndex={i}
                compact={compact}
                isLoading={isLoading && i === messages.length - 1}
                tts={tts}
                onEditUser={editUserMessage}
                onEditAssistant={editAssistantMessage}
                onRegenerateAssistant={regenerateAssistantMessage}
                onDelete={deleteMessage}
                onBranch={onBranchFromMessage}
                onSaveAsTemplate={handleSaveAsTemplate}
                onMaskEdit={handleMaskEdit}
                onPaintEdit={handlePaintEditFromHistory}
                availableModels={availableModels}
                onRegenerateWithModel={regenerateWithModel}
                onToggleMemoryLike={turn ? handleToggleMemoryLike : undefined}
                memoryLikeStatus={turn ? memory.states[turn.turnKey] : undefined}
              />
            )
          })}
          {/* CTR-0100: approval cards render inline at the tail of the
              message flow (the tool-call position) so they scroll with
              the conversation instead of covering the scroll area. */}
          <ToolApprovalList api={approvalApi} />
          {/* CTR-0092 bottom spacer: keeps the final message visible above the floating ChatInput. */}
          {!compact && <div aria-hidden="true" style={{ height: bottomSpacerHeightPx }} />}
        </div>
      </div>

      {/* CTR-0103 Message Navigator: floating right-gutter rail + popover.
          Overlay only; absent in compact mode and on constrained viewports. */}
      {messageNav.isAvailable && (
        <MessageNavigator turns={messageNav.turns} activeId={messageNav.activeId} onJump={messageNav.scrollToTurn} />
      )}

      {notification && (
        <div
          className={cn(
            'absolute right-3 top-3 z-30 rounded-md px-4 py-2 text-sm shadow-md',
            notification.type === 'success' && 'bg-green-500/10 text-green-600 border border-green-500/20',
            notification.type === 'error' && 'bg-red-500/10 text-red-600 border border-red-500/20',
            notification.type === 'info' && 'bg-amber-500/10 text-amber-600 border border-amber-500/20',
          )}>
          {notification.message}
        </div>
      )}

      {compact ? (
        <div ref={inputRef}>
          <div className="flex items-center justify-end gap-1 px-4">
            <ModelSelector ref={modelSelectorRef} threadId={threadId ?? ''} onModelChange={handleModelChange} />
            <ModelOptionsSelector
              threadId={threadId ?? ''}
              selectedModel={selectedModel}
              onOptionsChange={handleModelOptionsChange}
            />
            <StructuredOutputControl
              threadId={threadId ?? ''}
              selectedModel={selectedModel}
              onChange={handleStructuredChange}
            />
            <ImageOutputOptions threadId={threadId ?? ''} onChange={handleImageOptionsChange} />
            <McpToolManager />
            <SkillsManager />
            {!temporary && (
              <BackgroundResponsesToggle
                enabled={effectiveBgEnabled}
                onToggle={handleBgToggle}
                disabled={!bgSupported}
              />
            )}
            <ContextWindowIndicator usage={latestUsage} maxContextTokens={modelMaxTokens} />
          </div>
          <ChatInput
            ref={chatInputRef}
            onSend={handleSend}
            onStop={stopGeneration}
            isLoading={isLoading}
            attachments={attachments}
            onAddFiles={handleAddFiles}
            onRemoveAttachment={removeAttachment}
            getImageRefs={getImageRefs}
            isUploading={isUploading}
            bgEnabled={bgEnabled}
            onOpenTemplates={handleOpenTemplates}
            onOpenPaint={handleOpenPaint}
            onEditAttachment={handleEditAttachment}
            onSlashModel={handleSlashModel}
            onSlashHelp={handleSlashHelp}
            onSlashCron={onSlashCron}
            onSlashFiles={onSlashFiles}
            availableModels={availableModels}
            temporary={temporary}
          />
        </div>
      ) : (
        // right edge inset by the scrollbar width so the always-visible chat
        // scrollbar is never covered by this floating input overlay (CTR-0092)
        <div ref={inputRef} className="absolute bottom-0 left-0 right-[var(--app-scrollbar-width)] z-20">
          {/* CTR-0092 ScrollToBottom overlay + CTR-0168 per-message step buttons:
              anchored above the ChatInput, horizontally centered. prev / next flank
              the Scroll-to-Bottom button and appear whenever the chat overflows. */}
          <div className="pointer-events-none absolute -top-3 left-0 right-0 z-10 flex items-center justify-center gap-2">
            {stepNav.isAvailable && (
              <div className="pointer-events-auto">
                <MessageStepButton direction="prev" visible={stepNav.canPrev} onClick={stepNav.stepPrev} />
              </div>
            )}
            <div className="pointer-events-auto">
              <ScrollToBottomButton visible={showScrollToBottomButton} onClick={scrollToBottom} />
            </div>
            {stepNav.isAvailable && (
              <div className="pointer-events-auto">
                <MessageStepButton direction="next" visible={stepNav.canNext} onClick={stepNav.stepNext} />
              </div>
            )}
          </div>
          <div className="pointer-events-none bg-linear-to-t from-background from-60% to-transparent pt-6" />
          <div className="relative bg-background">
            <div className="mx-auto flex max-w-3xl items-center justify-end gap-1 px-4">
              <ModelSelector ref={modelSelectorRef} threadId={threadId ?? ''} onModelChange={handleModelChange} />
              <ModelOptionsSelector
                threadId={threadId ?? ''}
                selectedModel={selectedModel}
                onOptionsChange={handleModelOptionsChange}
              />
              <StructuredOutputControl
                threadId={threadId ?? ''}
                selectedModel={selectedModel}
                onChange={handleStructuredChange}
              />
              <ImageOutputOptions threadId={threadId ?? ''} onChange={handleImageOptionsChange} />
              <McpToolManager />
              <SkillsManager />
              {!temporary && (
                <BackgroundResponsesToggle
                  enabled={effectiveBgEnabled}
                  onToggle={handleBgToggle}
                  disabled={!bgSupported}
                />
              )}
              <ContextWindowIndicator usage={latestUsage} maxContextTokens={modelMaxTokens} />
            </div>
            <ChatInput
              ref={chatInputRef}
              onSend={handleSend}
              onStop={stopGeneration}
              isLoading={isLoading}
              attachments={attachments}
              onAddFiles={handleAddFiles}
              onRemoveAttachment={removeAttachment}
              getImageRefs={getImageRefs}
              isUploading={isUploading}
              bgEnabled={effectiveBgEnabled}
              onOpenTemplates={handleOpenTemplates}
              onOpenPaint={handleOpenPaint}
              onEditAttachment={handleEditAttachment}
              onSlashModel={handleSlashModel}
              onSlashHelp={handleSlashHelp}
              onSlashCron={onSlashCron}
              onSlashFiles={onSlashFiles}
              availableModels={availableModels}
              temporary={temporary}
            />
          </div>
        </div>
      )}

      {isDragging && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-xs">
          <div className="flex flex-col items-center gap-3 rounded-xl border-2 border-dashed border-primary p-12">
            <ImageIcon className="h-10 w-10 text-primary" />
            <p className="text-sm font-medium text-primary">Drop images here to attach</p>
          </div>
        </div>
      )}

      <HelpPortal open={helpOpen} onOpenChange={setHelpOpen} />
      <PromptTemplatesModal
        open={templatesModalOpen}
        onOpenChange={setTemplatesModalOpen}
        onInsert={handleInsertTemplate}
        onNotify={(message, type) => setNotification({ type, message })}
      />
      <SaveAsTemplateDialog
        open={saveAsDialogOpen}
        onOpenChange={setSaveAsDialogOpen}
        initialBody={saveAsBody}
        onSave={createTemplate}
        onNotify={(message, type) => setNotification({ type, message })}
      />
      {maskEditorState && (
        <MaskEditorDialog
          open={!!maskEditorState}
          onOpenChange={(open) => !open && setMaskEditorState(null)}
          imageUrl={maskEditorState.imageUrl}
          onGenerate={handleMaskGenerate}
        />
      )}
      {paintState && (
        <Suspense fallback={null}>
          <PaintEditor
            open={!!paintState}
            onOpenChange={(open) => !open && setPaintState(null)}
            initialScene={paintState.scene}
            onAttach={handlePaintAttach}
          />
        </Suspense>
      )}
    </div>
  )
}
