import { Bot, Hammer, ImageIcon, Loader2, Workflow as WorkflowIcon } from 'lucide-react'
import { type DragEvent, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChatInput, type ChatInputHandle } from '@/components/ChatInput'
import { ChatMessageItem } from '@/components/ChatMessageItem'
import { ACTIVE_AGENT_CHANGED_EVENT } from '@/components/DeclarativeAgentManager'
import { HelpPortal } from '@/components/HelpPortal'
import { type ImageEditSubmission, MaskEditorDialog } from '@/components/MaskEditorDialog'
import { MessageNavigator } from '@/components/MessageNavigator'
import { MessageStepButton } from '@/components/MessageStepButton'
import { OPEN_RUN_TARGET_PICKER_EVENT } from '@/components/RunTargetSheet'
import { ScrollToBottomButton } from '@/components/ScrollToBottomButton'
import { PromptTemplatesModal } from '@/components/templates/PromptTemplatesModal'
import { SaveAsTemplateDialog } from '@/components/templates/SaveAsTemplateDialog'
import { EMPTY_WORKFLOW_RUN, reduceWorkflowEvent, type WorkflowRunState } from '@/components/WorkflowProgressPanel'
import { WorkflowRunCanvas } from '@/components/WorkflowRunCanvas'
import { useActiveModel } from '@/hooks/useActiveModel'
import { useChat } from '@/hooks/useChat'
import { useChatScroll } from '@/hooks/useChatScroll'
import { type ImageAttachment, useImageAttachment } from '@/hooks/useImageAttachment'
import { useMemoryCuration } from '@/hooks/useMemoryCuration'
import { useMessageNavigator } from '@/hooks/useMessageNavigator'
import { useMessageStepNav } from '@/hooks/useMessageStepNav'
import { useTemplates } from '@/hooks/useTemplates'
import { useTTS } from '@/hooks/useTTS'
import { useWorkflowRunCanvas } from '@/hooks/useWorkflowRunCanvas'
import { resolveContextOccupancy } from '@/lib/contextOccupancy'
import { IMAGE_EDIT_TOOL } from '@/lib/imageTools'
import { lazyWithReload } from '@/lib/lazy-with-reload'
import { requestImageEdit, uploadSessionImage } from '@/lib/maskApi'
import { type EntryId, isEntryVisible, useChatSurfaceTier } from '@/lib/narrowSurface'
import { getHarnessRunTarget, getWorkflowRunTarget, RUN_TARGET_CHANGED_EVENT } from '@/lib/runTarget'
import { cn } from '@/lib/utils'
import type { ChatMessage, ImageRef, PersistedWorkflowRun, UsageInfo } from '@/types/chat'

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
  onStreamComplete?: () => void
  /** New-session created (PRP-0077, CTR-0016): show it in the sidebar immediately. */
  onSessionCreated?: (info: { threadId: string; title: string }) => void
  onBranchFromMessage?: (messageIndex: number) => void
  /** Slash command /cron (CTR-0135, PRP-0089): open the Cron scheduler portal. */
  onSlashCron?: () => void
  /** Slash command /files (CTR-0137, PRP-0091): open the File Explorer overlay. */
  onSlashFiles?: () => void
  /** File Explorer attach bridge (CTR-0137, PRP-0116): a File to add to the composer. */
  attachFile?: File | null
  /** Signals the attachFile was consumed so the parent can clear it. */
  onAttachConsumed?: () => void
  /** Temporary Chat mode (CTR-0107, PRP-0076): dark input, no history. */
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
  onStreamComplete,
  onSessionCreated,
  onBranchFromMessage,
  onSlashCron,
  onSlashFiles,
  attachFile,
  onAttachConsumed,
  temporary = false,
}: ChatPanelProps) {
  const [notification, setNotification] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null)
  // Which model answers is the RUN-TARGET's business since PRP-0184 (UDR-0166 D1):
  // the panel reads it, it does not choose it. `maxContextTokens` still feeds the
  // context-window indicator (CTR-0092).
  const { model: selectedModel, maxContextTokens: modelMaxTokens } = useActiveModel()
  // Run-target (CTR-0185, PRP-0118, UDR-0101 D5 amended). Decided from the unified
  // Declarative Agents modal, not a composer picker: a selected Workflow (from the
  // run-target store) streams the compiled workflow (state.workflow_id) and hides the
  // per-message model / options controls; otherwise the active Prompt agent runs.
  const [wfTarget, setWfTarget] = useState(getWorkflowRunTarget)
  // Harness Agent run-target (CTR-0197, PRP-0135, UDR-0119 D3). Mutually exclusive
  // with the workflow run-target -- the store enforces one effective axis.
  const [hTarget, setHTarget] = useState(getHarnessRunTarget)
  const [activeAgent, setActiveAgent] = useState<{ id: string; name: string }>({ id: '', name: '' })
  const [workflowRun, setWorkflowRun] = useState<WorkflowRunState>(EMPTY_WORKFLOW_RUN)
  // Detached per-run canvases (CTR-0187, PRP-0123). Ephemeral; never persisted.
  const canvas = useWorkflowRunCanvas()
  const selectedWorkflowId = wfTarget?.id ?? ''
  const selectedHarnessId = hTarget?.id ?? ''
  // Label stamped on the assistant message: the workflow / harness name, or the active
  // agent's name -- including the Built-in agent (v0.112.2), so a reloaded chat can
  // always say which Built-in / Prompt / Workflow / Harness agent answered.
  const runTargetLabel = wfTarget ? `⧉ ${wfTarget.name}` : hTarget ? `⚙ ${hTarget.name}` : activeAgent.name || undefined
  const runTargetName = selectedWorkflowId ? wfTarget?.name : selectedHarnessId ? hTarget?.name : activeAgent.name
  // On a phone the button exists before /api/model has answered (D9), so it never renders
  // an empty label.
  const runTargetText = runTargetName || 'Agent'
  // Chat surface tier (PRP-0171, UDR-0153). Only the full-page /chat surface provides
  // one; the compact /popup and /sidebar panels read the wide default.
  const surface = useChatSurfaceTier()
  // PRP-0186 (UDR-0168 D7): ONE switching surface on EVERY tier. The tier no longer
  // chooses the destination.
  //
  // UDR-0158 D1 sent the wide tier to the CTR-0144 manager and the narrow tier to the
  // CTR-0216 picker. That was right when the phone had no switching surface at all and
  // the picker was built to give it one. With the picker in place the split inverted
  // the cost: the wide viewport spent a ~90% authoring modal (create / edit / delete /
  // YAML) on the FREQUENT action, switching, while the phone got the light one.
  // Authoring stays wide-only and stays CTR-0144's (UDR-0158 D5 is unchanged); it is
  // reached from the picker's "Manage agents" entry, one click further, which is the
  // right trade for an action taken far less often than switching.
  const openRunTargetSurface = useCallback(() => {
    window.dispatchEvent(new Event(OPEN_RUN_TARGET_PICKER_EVENT))
  }, [])
  // PRP-0184 (UDR-0166 D1): `/model` opens the run-target surface. The argument is
  // accepted and ignored, and `true` means "the command was handled" -- so `/model` is
  // never sent to the model as text.
  const handleSlashModel = useCallback(() => {
    openRunTargetSurface()
    return true
  }, [openRunTargetSurface])
  const show = (id: EntryId) => isEntryVisible(surface, id)
  // PRP-0186 (CTR-0221, UDR-0168 D4): the run-target is an entry in the composer's
  // control row now, not a labelled chip above it. The icon carries the KIND; the NAME
  // travels with it and the composer states it in `title` and `aria-label`, because an
  // icon cannot distinguish one Prompt agent from another and a Prompt activation is
  // server-wide (UDR-0158 D3).
  const runTargetIcon = selectedWorkflowId ? (
    <WorkflowIcon className="h-4 w-4" />
  ) : selectedHarnessId ? (
    <Hammer className="h-4 w-4" />
  ) : (
    <Bot className="h-4 w-4" />
  )
  const composerRunTarget = show('toolbar.runTargetAction')
    ? { name: runTargetText, icon: runTargetIcon, onOpen: openRunTargetSurface }
    : undefined

  // Keep the run-target in sync with the modal (workflow selection + agent activation).
  useEffect(() => {
    const onRt = () => {
      setWfTarget(getWorkflowRunTarget())
      setHTarget(getHarnessRunTarget())
    }
    const onAgent = () => {
      fetch('/api/model')
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          const a = d?.active_agent as { id?: string; name?: string } | undefined
          setActiveAgent({ id: a?.id ?? '', name: a?.name ?? '' })
        })
        .catch(() => setActiveAgent({ id: '', name: '' }))
    }
    onAgent()
    window.addEventListener(RUN_TARGET_CHANGED_EVENT, onRt)
    window.addEventListener(ACTIVE_AGENT_CHANGED_EVENT, onAgent)
    return () => {
      window.removeEventListener(RUN_TARGET_CHANGED_EVENT, onRt)
      window.removeEventListener(ACTIVE_AGENT_CHANGED_EVENT, onAgent)
    }
  }, [])
  // PRP-0184 (UDR-0166 D1/D10): the per-message generation-option state is GONE.
  // Model, reasoning effort and structured output are configured on the run-target
  // -- the Built-in agent card, an agent / harness detail screen, or the narrow
  // run-target picker -- and travel to the provider as the Agent's default_options.
  // PRP-0185 (UDR-0167 D11): image output options have followed them. They are a
  // tool-argument default rather than a generation option, which is why PRP-0184 left
  // them behind -- but they are still the BUILT-IN AGENT's configuration, not a
  // property of one chat session, so they now live on the Core agent card and are read
  // server-side. The composer holds no state for them.

  // Auto-dismiss notification
  useEffect(() => {
    if (notification) {
      const t = setTimeout(() => setNotification(null), 5000)
      return () => clearTimeout(t)
    }
  }, [notification])

  const {
    messages,
    isLoading,
    saveRetry,
    sendMessage,
    runDirectTurn,
    retryTurn,
    stopGeneration,
    editUserMessage,
    regenerateAssistantMessage,
    editAssistantMessage,
    deleteMessage,
  } = useChat({
    threadId,
    initialMessages,
    onStreamComplete,
    onSessionCreated,
    temporary,
    selectedWorkflowId,
    selectedHarnessId,
    runTargetLabel,
    // Fold the additive workflow_* progress events into the live workflow run state
    // (PRP-0118, CTR-0185). The tool-approval consumer that used to share this fan-out
    // was removed with the approval flow (PRP-0179, UDR-0161).
    onCustomEvent: useCallback((name: string | undefined, value: Record<string, unknown> | undefined) => {
      if (name?.startsWith('workflow_')) setWorkflowRun((s) => reduceWorkflowEvent(s, name, value))
    }, []),
    // Standard AG-UI workflow-run events (PRP-0123, CTR-0009 v19). Workflow branch only;
    // a Prompt-agent turn never emits them, so this is inert for every other run.
    onWorkflowEvent: canvas.ingest,
    // v0.117.1: save the run with the message so a reloaded chat can rebuild it.
    getWorkflowRunSnapshot: canvas.snapshotCurrent,
    // v0.77.1: transient upstream 5xx auto-retry status (CTR-0009). Shown as a
    // brief amber banner so the user knows the run is being resent, not stalled.
    onNotice: useCallback((message: string) => setNotification({ type: 'info', message }), []),
    // PRP-0110 / UDR-0088 D7: a send succeeded after a pre-commit failure -- the
    // server is back. Reassure the user through the existing notification surface;
    // there is no proactive liveness monitor (UDR-0088 D5).
    onConnectionRecovered: useCallback(() => setNotification({ type: 'success', message: 'Connection recovered' }), []),
    // PRP-0174 / UDR-0156 D4: a reply / edit / delete that could not be saved.
    onPersistError: useCallback((message: string) => setNotification({ type: 'error', message }), []),
  })

  const { attachments, addFiles, attachPaintImage, removeAttachment, clearAttachments, getImageRefs, isUploading } =
    useImageAttachment()

  // File Explorer attach bridge (CTR-0137, PRP-0116): the parent hands a File
  // sourced from an image/PDF preview; upload it to the active thread via the
  // existing composer attach path, then signal it consumed so it is not re-added.
  useEffect(() => {
    if (!attachFile || !threadId) return
    void addFiles([attachFile], threadId)
    onAttachConsumed?.()
  }, [attachFile, threadId, addFiles, onAttachConsumed])

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

  // Slash commands (CTR-0128, PRP-0088): /help opens the Help Portal. Since
  // PRP-0184 (UDR-0166 D1) /model has no per-message selector to drive, so it OPENS
  // the run-target surface -- where the model is chosen and persisted -- instead of
  // being removed outright.
  const [helpOpen, setHelpOpen] = useState(false)
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

  // PRP-0187 / UDR-0169 D8: the editor's Generate calls the Images API DIRECTLY
  // (CTR-0053 v2) instead of asking the agent, so it works whatever run-target is
  // selected and the 16 inputs cannot be reordered or the prompt paraphrased. Both
  // bubbles render at once (D13: preview -> annotated -> references); the server
  // persists the turn itself, idempotently by the ids minted here.
  const handleMaskGenerate = useCallback(
    async (edit: ImageEditSubmission) => {
      setMaskEditorState(null)
      if (!threadId) return
      const editId = crypto.randomUUID().slice(0, 12)
      const userId = crypto.randomUUID()
      const assistantId = crypto.randomUUID()
      const toolCallId = `edit_${editId}`
      const now = new Date().toISOString()

      const localUrls: string[] = []
      const local = (blob: Blob) => {
        const url = URL.createObjectURL(blob)
        localUrls.push(url)
        return url
      }
      // Without a mask the first image is the source itself: the image being edited.
      const firstDisplay = edit.previewBlob ? local(edit.previewBlob) : edit.sourceUrl
      const displayImages: ImageRef[] = [
        { uri: firstDisplay, media_type: 'image/png' },
        ...(edit.annotatedBlob ? [{ uri: local(edit.annotatedBlob), media_type: 'image/png' }] : []),
        ...edit.references.map((r) => ({ uri: r.uri, media_type: 'image/png' })),
      ]
      const displayText = [
        `Change: ${edit.change}`,
        ...edit.annotations.filter((a) => a.note).map((a) => `${a.label}: ${a.note}`),
        ...(edit.preserve ? [`Preserve: ${edit.preserve}`] : []),
      ].join('\n')

      const user: ChatMessage = {
        id: userId,
        role: 'user',
        content: displayText,
        createdAt: now,
        images: displayImages,
      }
      const assistant: ChatMessage = {
        id: assistantId,
        role: 'assistant',
        content: '',
        createdAt: now,
        toolCalls: [{ id: toolCallId, name: IMAGE_EDIT_TOOL, status: 'running' }],
      }

      await runDirectTurn(user, assistant, async () => {
        try {
          // Every upload carries a unique name (the v0.121.1 collision lesson).
          const [src, mask, annotated, preview] = await Promise.all([
            edit.sourceBlob ? uploadSessionImage(threadId, edit.sourceBlob, `imgedit_src_${editId}.png`) : null,
            edit.maskBlob ? uploadSessionImage(threadId, edit.maskBlob, `imgedit_mask_${editId}.png`) : null,
            edit.annotatedBlob ? uploadSessionImage(threadId, edit.annotatedBlob, `imgedit_annot_${editId}.png`) : null,
            edit.previewBlob ? uploadSessionImage(threadId, edit.previewBlob, `imgedit_preview_${editId}.png`) : null,
          ])
          const storedImages: ImageRef[] = [
            { uri: preview?.uri ?? edit.sourceUrl, media_type: 'image/png' },
            ...(annotated ? [{ uri: annotated.uri, media_type: 'image/png' }] : []),
            ...edit.references.map((r) => ({ uri: r.uri, media_type: 'image/png' })),
          ]
          const result = await requestImageEdit({
            thread_id: threadId,
            source: src?.filename ?? edit.sourceName,
            mask: mask?.filename ?? null,
            annotated: annotated?.filename ?? null,
            references: edit.references.map((r) => r.filename),
            change: edit.change,
            preserve: edit.preserve,
            annotations: edit.annotations,
            quality: edit.quality,
            background: edit.background,
            n: edit.n,
            user_message_id: userId,
            assistant_message_id: assistantId,
            display_text: displayText,
            display_images: storedImages,
          })
          const names = result.images.map((img) => img.filename).join(', ')
          return {
            user: { images: storedImages },
            assistant: {
              content: names ? `Edited image saved as ${names}.` : '',
              toolCalls: [
                { id: toolCallId, name: IMAGE_EDIT_TOOL, status: 'completed', result: JSON.stringify(result) },
              ],
            },
          }
        } catch (err) {
          const message = err instanceof Error ? err.message : 'Image editing failed'
          setNotification({ type: 'error', message })
          return {
            assistant: {
              content: `Image editing failed: ${message}`,
              toolCalls: [
                {
                  id: toolCallId,
                  name: IMAGE_EDIT_TOOL,
                  status: 'completed',
                  result: JSON.stringify({ error: message, no_file_created: true }),
                },
              ],
            },
          }
        } finally {
          // The bubbles keep showing the object URLs until the uploaded URIs arrive.
          setTimeout(() => {
            for (const url of localUrls) URL.revokeObjectURL(url)
          }, 60_000)
        }
      })
    },
    [threadId, runDirectTurn],
  )

  const [isDragging, setIsDragging] = useState(false)
  const dragCountRef = useRef(0)
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
    if (!assistant.content?.trim()) return null
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

  // Context indicator input (CTR-0041, UDR-0152 D8). A Prompt / Harness turn MEASURES what
  // the next message carries: its input already holds the whole session history. A
  // workflow turn measures nothing of the kind -- its nodes see only the latest user
  // message -- yet its reply is saved with the session and becomes history for the next
  // Prompt turn. So after workflow turns the indicator shows an ESTIMATE: the latest
  // measured Prompt / Harness context plus the chat output of every workflow turn since.
  const latestUsage = useMemo((): UsageInfo | undefined => {
    let workflowTokens = 0
    let sawWorkflow = false
    for (let i = messages.length - 1; i >= 0; i--) {
      const usage = messages[i].usage
      if (!usage) continue
      if (usage.workflow_nodes) {
        const rows = usage.workflow_nodes
        const flagged = rows.some((n) => n.sent_to_chat !== undefined)
        for (const n of rows) {
          if (flagged && !n.sent_to_chat) continue
          const out = n.turn?.output_token_count ?? 0
          workflowTokens += Math.max(out - (n.turn?.reasoning_output_token_count ?? 0), 0)
        }
        sawWorkflow = true
        continue
      }
      if (usage.context_base_tokens === undefined && usage.input_token_count === undefined) continue
      if (!sawWorkflow) return usage
      const base = usage.context_base_tokens ?? (usage.input_token_count ?? 0) + (usage.output_token_count ?? 0)
      return {
        max_context_tokens: usage.max_context_tokens,
        context_base_tokens: base + workflowTokens,
        context_estimated: true,
      }
    }
    return sawWorkflow ? { context_base_tokens: workflowTokens, context_estimated: true } : undefined
  }, [messages])

  // PRP-0186 (CTR-0041 v5, UDR-0168 D5/D6): occupancy resolves to a LEVEL and a
  // sentence, not a bar. `null` below the warning threshold -- and for a chat that has
  // run no turn -- so the composer is quiet until something follows from the number.
  // The thresholds and the arithmetic are unchanged from the indicator this replaced.
  const contextOccupancy = useMemo(
    () => resolveContextOccupancy(latestUsage, modelMaxTokens),
    [latestUsage, modelMaxTokens],
  )

  const handleSend = useCallback(
    async (content: string, images?: ImageRef[]) => {
      // Kick off the send, then clear/scroll immediately -- the commit flag is
      // awaited afterwards so ChatInput can restore the text on a pre-commit
      // failure (CTR-0004 v2, PRP-0110).
      // A workflow send opens its own run canvas (CTR-0187, UDR-0106 D12); the graph is
      // fetched from the document so it renders before the first node event arrives.
      if (selectedWorkflowId) canvas.beginRun(selectedWorkflowId, wfTarget?.name ?? selectedWorkflowId)
      const pending = sendMessage(content, images)
      clearAttachments()
      // PRP-0058 UX-2: user-send is the strongest "follow output" intent.
      // Force-resume autoscroll so the new user message + assistant stream
      // re-anchor at the bottom even if the operator had previously
      // scrolled up to read earlier text (autoscrollRef was false).
      scrollToBottom()
      return await pending
    },
    [sendMessage, clearAttachments, scrollToBottom, selectedWorkflowId, wfTarget?.name, canvas],
  )

  // Human-in-the-loop answers (CTR-0187 -> CTR-0009 v19, UDR-0106 D5). The paused run
  // continues on a NEW turn carrying state.workflow_resume and no user message; the
  // canvas keeps painting because the run id is unchanged.
  const handleWorkflowInput = useCallback(
    (runId: string, answers: Record<string, { user_input: string; value: unknown }>) => {
      // Close the request FIRST so the input form disappears on Submit rather than staying
      // on screen for the whole resumed run, inviting an answer to a closed question.
      canvas.submitInput(runId)
      void sendMessage('', undefined, { skipUserMessage: true, workflowResume: answers })
    },
    [sendMessage, canvas],
  )

  // PRP-0110 / UDR-0088 D3: re-send a user turn that failed before committing.
  const handleRetryTurn = useCallback(
    (messageId: string) => {
      void retryTurn(messageId)
      scrollToBottom()
    },
    [retryTurn, scrollToBottom],
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
                onRetryTurn={handleRetryTurn}
                onEditAssistant={editAssistantMessage}
                onRegenerateAssistant={regenerateAssistantMessage}
                onDelete={deleteMessage}
                onBranch={onBranchFromMessage}
                onSaveAsTemplate={show('message.saveAsTemplate') ? handleSaveAsTemplate : undefined}
                onMaskEdit={show('message.maskEdit') ? handleMaskEdit : undefined}
                onPaintEdit={show('message.paintEdit') ? handlePaintEditFromHistory : undefined}
                onToggleMemoryLike={turn ? handleToggleMemoryLike : undefined}
                memoryLikeStatus={turn ? memory.states[turn.turnKey] : undefined}
                workflowRun={
                  selectedWorkflowId && i === messages.length - 1 && msg.role === 'assistant' ? workflowRun : undefined
                }
                onOpenWorkflowCanvas={
                  // Live run: re-open the canvas for the latest run (closing only hides it).
                  // Reloaded chat: rebuild the canvas from the run persisted with THAT message,
                  // so any past workflow turn can be inspected again (v0.117.1).
                  // Narrow viewport: no "Diagram" entry; the text progress stays (PRP-0171).
                  !show('message.workflowDiagram')
                    ? undefined
                    : selectedWorkflowId && i === messages.length - 1 && msg.role === 'assistant' && canvas.hasCurrent()
                      ? canvas.openCurrent
                      : msg.workflowRun
                        ? () => canvas.openRestored(msg.workflowRun as PersistedWorkflowRun)
                        : undefined
                }
              />
            )
          })}
          {/* CTR-0092 bottom spacer: keeps the final message visible above the floating ChatInput. */}
          {!compact && <div aria-hidden="true" style={{ height: bottomSpacerHeightPx }} />}
        </div>
      </div>

      {/* CTR-0103 Message Navigator: floating right-gutter rail + popover.
          Overlay only; absent in compact mode and on constrained viewports. */}
      {messageNav.isAvailable && (
        <MessageNavigator turns={messageNav.turns} activeId={messageNav.activeId} onJump={messageNav.scrollToTurn} />
      )}

      {/* PRP-0174 / UDR-0156 D4: a failed save is being retried. The chat is locked
          (covered, inert to pointer and keyboard) until the save lands or gives up. */}
      {saveRetry && (
        <div
          role="alertdialog"
          aria-modal="true"
          aria-live="assertive"
          aria-label="Saving the reply"
          className="absolute inset-0 z-40 flex items-center justify-center bg-background/70 backdrop-blur-[1px]">
          <div className="flex items-center gap-3 rounded-lg border bg-card px-5 py-4 text-sm shadow-lg">
            <Loader2 className="h-5 w-5 animate-spin text-primary" aria-hidden="true" />
            <div>
              <p className="font-medium">Saving the reply...</p>
              <p className="text-xs text-muted-foreground">
                Retry {saveRetry.attempt} of {saveRetry.total}. Please wait.
              </p>
            </div>
          </div>
        </div>
      )}

      {notification && (
        <div
          className={cn(
            'absolute right-3 top-3 z-50 rounded-md px-4 py-2 text-sm shadow-md',
            notification.type === 'success' && 'bg-green-500/10 text-green-600 border border-green-500/20',
            notification.type === 'error' && 'bg-red-500/10 text-red-600 border border-red-500/20',
            notification.type === 'info' && 'bg-amber-500/10 text-amber-600 border border-amber-500/20',
          )}>
          {notification.message}
        </div>
      )}

      {compact ? (
        <div ref={inputRef}>
          {/* PRP-0186 (CTR-0221, UDR-0168 D1): the strip that stood here is GONE.
              PRP-0184 took the model / options / structured controls off the composer
              and PRP-0185 took the image options and the two managers, leaving a row
              whose only remaining job was to hold two indicators -- and that row cost
              real height, because useChatScroll observes this subtree and ChatPanel
              reserves a spacer of its measured height (CTR-0092). Both survivors moved
              INTO the composer: who answers as an icon in its control row, how full the
              window is as its outline. */}
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
            onOpenTemplates={handleOpenTemplates}
            onOpenPaint={handleOpenPaint}
            onEditAttachment={handleEditAttachment}
            onSlashModel={handleSlashModel}
            onSlashHelp={handleSlashHelp}
            onSlashCron={onSlashCron}
            onSlashFiles={onSlashFiles}
            temporary={temporary}
            runTarget={composerRunTarget}
            context={contextOccupancy ?? undefined}
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
          {/* No safe-area padding here (v0.155.2): the surface is sized to the visible
              viewport, which already ends above Safari's toolbar, so an inset added the
              home-indicator height a second time (most visibly on iPad). */}
          <div className="relative bg-background">
            {/* PRP-0186 (CTR-0221, UDR-0168 D1): the strip that stood here is GONE; see
                the compact branch for why. Both of its survivors moved into the
                composer, so this wrapper holds the composer alone and `inputRef`'s
                measured height -- the chat body's bottom spacer (CTR-0092) -- is the
                composer's height and nothing else. */}
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
              onOpenTemplates={show('attach.templates') ? handleOpenTemplates : undefined}
              onOpenPaint={show('attach.paint') ? handleOpenPaint : undefined}
              onEditAttachment={handleEditAttachment}
              onSlashModel={handleSlashModel}
              onSlashHelp={show('slash.help') ? handleSlashHelp : undefined}
              onSlashCron={onSlashCron}
              onSlashFiles={onSlashFiles}
              temporary={temporary}
              runTarget={composerRunTarget}
              context={contextOccupancy ?? undefined}
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
      {maskEditorState && threadId && (
        <MaskEditorDialog
          open={!!maskEditorState}
          onOpenChange={(open) => !open && setMaskEditorState(null)}
          imageUrl={maskEditorState.imageUrl}
          threadId={threadId}
          onGenerate={handleMaskGenerate}
          onNotify={(message) => setNotification({ type: 'error', message })}
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

      {/* Detached workflow run canvases (CTR-0187, PRP-0123, UDR-0106 D12). One per run,
          movable and resizable. Closing one HIDES it -- the run's state is retained so the
          in-message indicator's "Diagram" control can re-open it -- and never affects the
          run itself. */}
      {canvas.instances
        .filter((i) => i.open)
        .map((instance, idx) => (
          <WorkflowRunCanvas
            key={instance.run.runId}
            run={instance.run}
            actions={instance.actions}
            index={idx}
            onClose={() => canvas.close(instance.run.runId)}
            onSubmitInput={(answers) => handleWorkflowInput(instance.run.runId, answers)}
          />
        ))}
    </div>
  )
}
