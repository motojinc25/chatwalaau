import { ImageIcon } from 'lucide-react'
import { type DragEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BackgroundResponsesToggle } from '@/components/BackgroundResponsesToggle'
import { ChatInput, type ChatInputHandle } from '@/components/ChatInput'
import { ChatMessageItem } from '@/components/ChatMessageItem'
import { ContextWindowIndicator } from '@/components/ContextWindowIndicator'
import { MaskEditorDialog } from '@/components/MaskEditorDialog'
import { ModelSelector } from '@/components/ModelSelector'
import { ScrollToBottomButton } from '@/components/ScrollToBottomButton'
import { PromptTemplatesModal } from '@/components/templates/PromptTemplatesModal'
import { SaveAsTemplateDialog } from '@/components/templates/SaveAsTemplateDialog'
import { useChat } from '@/hooks/useChat'
import { useChatScroll } from '@/hooks/useChatScroll'
import { useImageAttachment } from '@/hooks/useImageAttachment'
import { useTemplates } from '@/hooks/useTemplates'
import { useTTS } from '@/hooks/useTTS'
import { cn } from '@/lib/utils'
import type { ChatMessage, ImageRef } from '@/types/chat'

const BG_STORAGE_KEY = 'chatwalaau-bg-enabled'

interface ChatPanelProps {
  compact?: boolean
  emptyMessage?: string
  className?: string
  threadId?: string
  initialMessages?: ChatMessage[]
  continuationToken?: Record<string, unknown> | null
  onStreamComplete?: () => void
  onBranchFromMessage?: (messageIndex: number) => void
}

function buildStreamingKey(messages: ChatMessage[], isLoading: boolean): string {
  const lastMsg = messages.at(-1)
  const toolCallCount = lastMsg?.toolCalls?.length ?? 0
  const lastToolStatus = lastMsg?.toolCalls?.at(-1)?.status ?? ''
  return `${messages.length}:${lastMsg?.content?.length ?? 0}:${toolCallCount}:${lastToolStatus}:${lastMsg?.reasoningBlocks?.length ?? 0}:${isLoading}`
}

export function ChatPanel({
  compact,
  emptyMessage = 'How can I help you today?',
  className,
  threadId,
  initialMessages,
  continuationToken,
  onStreamComplete,
  onBranchFromMessage,
}: ChatPanelProps) {
  const [bgEnabled, setBgEnabled] = useState(() => localStorage.getItem(BG_STORAGE_KEY) === 'true')
  const [notification, setNotification] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const [selectedModel, setSelectedModel] = useState('')
  const [modelMaxTokens, setModelMaxTokens] = useState(128000)
  const [availableModels, setAvailableModels] = useState<string[]>([])

  const handleBgToggle = useCallback((enabled: boolean) => {
    setBgEnabled(enabled)
    localStorage.setItem(BG_STORAGE_KEY, String(enabled))
  }, [])

  const handleModelChange = useCallback((model: string, maxTokens: number) => {
    setSelectedModel(model)
    setModelMaxTokens(maxTokens)
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
    bgEnabled,
    selectedModel,
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

  const { attachments, addFiles, removeAttachment, clearAttachments, getImageRefs, isUploading } = useImageAttachment()

  const tts = useTTS()

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
      try {
        // Upload mask preview image (for user message display)
        const previewForm = new FormData()
        previewForm.append('file', new File([previewBlob], 'mask_preview.png', { type: 'image/png' }))
        const previewRes = await fetch(`/api/upload/${threadId}`, { method: 'POST', body: previewForm })
        const previewData = previewRes.ok ? await previewRes.json() : null

        // Upload composited image (source for edit_image tool -- transparent areas = edit regions)
        const compositedForm = new FormData()
        compositedForm.append(
          'file',
          new File([compositedBlob], `mask_source_${Date.now()}.png`, { type: 'image/png' }),
        )
        const compositedRes = await fetch(`/api/upload/${threadId}`, { method: 'POST', body: compositedForm })
        const compositedData = compositedRes.ok ? await compositedRes.json() : null

        if (!compositedData?.filename) {
          setNotification({ type: 'error', message: 'Failed to upload source image' })
          return
        }

        // Send user message: preview image + instruction for agent to call edit_image
        const images: ImageRef[] = []
        if (previewData?.uri) images.push({ uri: previewData.uri, media_type: 'image/png' })

        sendMessage(`Edit the masked areas of the image "${compositedData.filename}": ${prompt}`, images)
      } catch (err) {
        setNotification({ type: 'error', message: err instanceof Error ? err.message : 'Failed to start mask edit' })
      }
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
      })
      .catch(() => {})
  }, [])

  // CTR-0092 Chat Scroll Behavior (PRP-0055): autoscroll suspend on user
  // intent, ScrollToBottom affordance, and bottom spacer sized by the
  // observed ChatInput height.
  const streamingKey = buildStreamingKey(messages, isLoading)
  const { scrollRef, inputRef, showScrollToBottomButton, bottomSpacerHeightPx, scrollToBottom } =
    useChatScroll(streamingKey)

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
          {messages.map((msg, i) => (
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
              onBranch={onBranchFromMessage ? () => onBranchFromMessage(i) : undefined}
              onSaveAsTemplate={handleSaveAsTemplate}
              onMaskEdit={handleMaskEdit}
              availableModels={availableModels}
              onRegenerateWithModel={regenerateWithModel}
            />
          ))}
          {/* CTR-0092 bottom spacer: keeps the final message visible above the floating ChatInput. */}
          {!compact && <div aria-hidden="true" style={{ height: bottomSpacerHeightPx }} />}
        </div>
      </div>

      {notification && (
        <div
          className={cn(
            'absolute right-3 top-3 z-30 rounded-md px-4 py-2 text-sm shadow-md',
            notification.type === 'success'
              ? 'bg-green-500/10 text-green-600 border border-green-500/20'
              : 'bg-red-500/10 text-red-600 border border-red-500/20',
          )}>
          {notification.message}
        </div>
      )}

      {compact ? (
        <div ref={inputRef}>
          <div className="flex items-center justify-end gap-1 px-4">
            <ModelSelector threadId={threadId ?? ''} onModelChange={handleModelChange} />
            <BackgroundResponsesToggle enabled={bgEnabled} onToggle={handleBgToggle} />
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
          />
        </div>
      ) : (
        <div ref={inputRef} className="absolute inset-x-0 bottom-0 z-20">
          {/* CTR-0092 ScrollToBottom overlay: anchored above the ChatInput, horizontally centered. */}
          <div className="pointer-events-none absolute -top-3 left-0 right-0 z-10 flex justify-center">
            <div className="pointer-events-auto">
              <ScrollToBottomButton visible={showScrollToBottomButton} onClick={scrollToBottom} />
            </div>
          </div>
          <div className="pointer-events-none bg-linear-to-t from-background from-60% to-transparent pt-6" />
          <div className="relative bg-background">
            <div className="mx-auto flex max-w-3xl items-center justify-end gap-1 px-4">
              <ModelSelector threadId={threadId ?? ''} onModelChange={handleModelChange} />
              <BackgroundResponsesToggle enabled={bgEnabled} onToggle={handleBgToggle} />
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
    </div>
  )
}
