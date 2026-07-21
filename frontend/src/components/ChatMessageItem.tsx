import {
  AlertTriangle,
  Bot,
  Check,
  ChevronDown,
  Copy,
  Download,
  FileText as FileTextIcon,
  GitBranch,
  Loader2,
  Pencil,
  RefreshCw,
  Square,
  ThumbsUp,
  Trash2,
  User,
  Volume2,
} from 'lucide-react'
import { type KeyboardEvent, memo, useCallback, useEffect, useRef, useState } from 'react'
import { AuthedImage } from '@/components/AuthedImage'
import { ImageGenerationResults } from '@/components/ImageGenerationResult'
import { MarkdownRenderer } from '@/components/MarkdownRenderer'
import { McpAppView } from '@/components/mcp-apps/McpAppView'
import { ReasoningIndicator, ThinkingBlock } from '@/components/ReasoningIndicator'
import { ToolCallBlock, ToolCallIndicator } from '@/components/ToolCallIndicator'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { WeatherToolResults } from '@/components/WeatherCard'
import { openUploadFullSize } from '@/lib/uploads'
import { cn } from '@/lib/utils'
import type { ChatMessage } from '@/types/chat'

/**
 * Wrap a structured (JSON) answer in a ```json code fence so MarkdownRenderer
 * renders it through CodeBlock (CTR-0012 v11, PRP-0082). Strips a fence the model
 * may already have emitted to avoid double-fencing. Pretty-prints when the content
 * is complete, valid JSON; otherwise (streaming / truncated) shows it verbatim.
 */
function toJsonCodeFence(content: string): string {
  let body = content.trim()
  if (body.startsWith('```')) {
    body = body
      .replace(/^```[a-zA-Z]*\n?/, '')
      .replace(/```$/, '')
      .trim()
  }
  try {
    body = JSON.stringify(JSON.parse(body), null, 2)
  } catch {
    // streaming / truncated / non-conforming: render the raw text as-is
  }
  return `\`\`\`json\n${body}\n\`\`\``
}

interface TTSControls {
  play: (text: string, messageId: string) => Promise<void>
  stop: () => void
  download: (text: string, messageId: string, filename: string) => Promise<void>
  ttsState: 'idle' | 'loading' | 'playing'
  downloadState: 'idle' | 'downloading'
  playingMessageId: string | null
  downloadingMessageId: string | null
}

interface ChatMessageItemProps {
  message: ChatMessage
  messageIndex?: number
  compact?: boolean
  isLoading?: boolean
  tts?: TTSControls
  onEditUser?: (messageId: string, newContent: string) => void
  /** Re-send a user turn whose send failed before committing (CTR-0004 v2, PRP-0110). */
  onRetryTurn?: (messageId: string) => void
  onEditAssistant?: (messageId: string, newContent: string) => void
  onRegenerateAssistant?: (messageId: string) => void
  onDelete?: (messageId: string) => void
  // PRP-0074: takes the row index so ChatPanel can pass a referentially
  // stable callback (no per-row inline closure) and keep React.memo effective.
  onBranch?: (messageIndex: number) => void
  onSaveAsTemplate?: (content: string) => void
  onMaskEdit?: (imageUrl: string) => void
  /** Re-edit a sent paint image in the Paint editor (CTR-0160/CTR-0161, PRP-0099). */
  onPaintEdit?: (imageUrl: string) => void
  /** Available models for regenerate-with-model dropdown (CTR-0071) */
  availableModels?: string[]
  /** Regenerate with a specific model (CTR-0071) */
  onRegenerateWithModel?: (messageId: string, model: string) => void
  /**
   * Agent Memory curation (CTR-0165, PRP-0100). When set, a thumbs-up "remember
   * this turn" action is shown on both the user and assistant message. Toggling
   * either curates the SAME turn (ChatPanel resolves the pair + turn_key).
   */
  onToggleMemoryLike?: (messageIndex: number) => void
  /** Curation status for this message's turn; undefined = not liked. */
  memoryLikeStatus?: 'pending' | 'curated' | 'failed'
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [text])

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-6 w-6 text-muted-foreground hover:text-foreground"
      onClick={handleCopy}
      title={copied ? 'Copied' : 'Copy message'}
      aria-label="Copy message">
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
    </Button>
  )
}

// Agent Memory "remember this turn" like (CTR-0165, PRP-0100). Placed in the
// message tool-icon row on both the user and assistant message; toggling either
// curates the same turn. Empty thumbs-up = not liked; filled = liked/curated;
// spinner = the background reconcile pass is running; amber = the pass failed.
function MemoryLikeButton({
  messageIndex,
  status,
  onToggle,
}: {
  messageIndex: number
  status?: 'pending' | 'curated' | 'failed'
  onToggle: (messageIndex: number) => void
}) {
  const liked = status !== undefined
  const label =
    status === 'pending'
      ? 'Saving to agent memory...'
      : status === 'failed'
        ? 'Memory update failed - click to retry'
        : liked
          ? 'Remembered - click to remove'
          : 'Remember this turn in agent memory'
  return (
    <Button
      variant="ghost"
      size="icon"
      className={cn(
        'h-6 w-6',
        status === 'failed'
          ? 'text-amber-600/80 hover:text-amber-600 dark:text-amber-500/80'
          : liked
            ? 'text-primary hover:text-primary'
            : 'text-muted-foreground hover:text-foreground',
      )}
      onClick={() => onToggle(messageIndex)}
      title={label}
      aria-label={label}
      aria-pressed={liked}>
      {status === 'pending' ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : (
        <ThumbsUp className={cn('h-3 w-3', liked && status !== 'failed' && 'fill-current')} />
      )}
    </Button>
  )
}

function TTSPlayButton({ message, tts }: { message: ChatMessage; tts: TTSControls }) {
  const isThisPlaying = tts.playingMessageId === message.id && tts.ttsState === 'playing'
  const isThisLoading = tts.playingMessageId === message.id && tts.ttsState === 'loading'
  const [hovered, setHovered] = useState(false)

  const handleClick = useCallback(() => {
    if (isThisPlaying) {
      tts.stop()
    } else {
      tts.play(message.content, message.id)
    }
  }, [isThisPlaying, tts, message.content, message.id])

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-6 w-6 text-muted-foreground hover:text-foreground"
      onClick={handleClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title={isThisPlaying ? 'Stop playback' : 'Play message'}
      aria-label={isThisPlaying ? 'Stop playback' : 'Play message'}>
      {isThisLoading ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : isThisPlaying && hovered ? (
        <Square className="h-3 w-3" />
      ) : (
        <Volume2 className="h-3 w-3" />
      )}
    </Button>
  )
}

function TTSDownloadButton({
  message,
  messageIndex,
  tts,
}: {
  message: ChatMessage
  messageIndex: number
  tts: TTSControls
}) {
  const isThisDownloading = tts.downloadingMessageId === message.id && tts.downloadState === 'downloading'

  const handleClick = useCallback(() => {
    tts.download(message.content, message.id, `message-${messageIndex}.mp3`)
  }, [tts, message.content, message.id, messageIndex])

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-6 w-6 text-muted-foreground hover:text-foreground"
      onClick={handleClick}
      disabled={isThisDownloading}
      title="Download audio (MP3)"
      aria-label="Download audio">
      {isThisDownloading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
    </Button>
  )
}

// Long user-message collapse (PRP-0085, FEAT-0001 / UDR-0063 D2). A user message
// whose RENDERED height exceeds ~5 lines is clamped with a soft fade and a
// Show more / Show less toggle. The threshold is judged by rendered height (so a
// single wrapped long line collapses too), not by counting newlines. Collapse
// applies to user messages only; the expanded/collapsed state is ephemeral
// (not persisted). A ResizeObserver re-measures when the column width changes.
const USER_MSG_COLLAPSED_MAX_PX = 120 // ~5 lines at text-sm leading-relaxed

function CollapsibleUserText({ content }: { content: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)

  // content is intentionally a dependency so the effect re-measures after the message
  // re-renders with new content (content drives the rendered height we measure).
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => setOverflowing(el.scrollHeight > USER_MSG_COLLAPSED_MAX_PX + 4)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [content])

  const collapsed = overflowing && !expanded

  return (
    <div>
      <div className="relative">
        <div
          ref={ref}
          className="overflow-hidden whitespace-pre-wrap"
          style={collapsed ? { maxHeight: USER_MSG_COLLAPSED_MAX_PX } : undefined}>
          {content}
        </div>
        {collapsed && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-background to-transparent" />
        )}
      </div>
      {overflowing && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-xs font-medium text-muted-foreground hover:text-foreground">
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}

function ChatMessageItemImpl({
  message,
  messageIndex = 0,
  compact,
  isLoading,
  tts,
  onEditUser,
  onRetryTurn,
  onEditAssistant,
  onRegenerateAssistant,
  onDelete,
  onBranch,
  onSaveAsTemplate,
  onMaskEdit,
  onPaintEdit,
  availableModels,
  onRegenerateWithModel,
  onToggleMemoryLike,
  memoryLikeStatus,
}: ChatMessageItemProps) {
  const isUser = message.role === 'user'
  const hasTextContent = message.content != null && message.content.trim().length > 0
  const isWaiting = !isUser && isLoading && !hasTextContent
  const [editing, setEditing] = useState(false)
  const [editValue, setEditValue] = useState('')
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [regenModelOpen, setRegenModelOpen] = useState(false)
  const editRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (editing) editRef.current?.focus()
  }, [editing])

  const handleStartEdit = useCallback(() => {
    setEditValue(message.content)
    setEditing(true)
  }, [message.content])

  const handleSubmitEdit = useCallback(() => {
    const trimmed = editValue.trim()
    if (!trimmed || trimmed === message.content) {
      setEditing(false)
      return
    }
    if (isUser && onEditUser) {
      onEditUser(message.id, trimmed)
    } else if (!isUser && onEditAssistant) {
      onEditAssistant(message.id, trimmed)
    }
    setEditing(false)
  }, [editValue, message.content, message.id, isUser, onEditUser, onEditAssistant])

  const handleEditKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Full-screen modal editor (PRP-0084, CTR-0018 v1.3): Enter inserts a
      // newline; Cmd/Ctrl+Enter submits. Skip while an IME composition is in
      // progress (CJK conversion).
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
        e.preventDefault()
        handleSubmitEdit()
      }
      if (e.key === 'Escape') {
        setEditing(false)
      }
    },
    [handleSubmitEdit],
  )

  const handleRegenerate = useCallback(() => {
    onRegenerateAssistant?.(message.id)
  }, [onRegenerateAssistant, message.id])

  // Message edit modal (PRP-0084, CTR-0018 v1.3, UDR-0062 D6). The editor is
  // relocated from a cramped inline textarea into an ~80% modal so long messages
  // are comfortable to edit. The edit DATA FLOW is unchanged: a user-message
  // edit truncates + re-requests; an assistant edit updates in place.
  const renderEditForm = () => (
    <Dialog open={editing} onOpenChange={(open) => !open && setEditing(false)}>
      <DialogContent className="flex h-[80vh] w-[80vw] max-w-[80vw] flex-col gap-3 sm:max-w-[80vw]">
        <DialogHeader>
          <DialogTitle>{isUser ? 'Edit message' : 'Edit response'}</DialogTitle>
        </DialogHeader>
        <textarea
          ref={editRef}
          value={editValue}
          onChange={(e) => setEditValue(e.target.value)}
          onKeyDown={handleEditKeyDown}
          className="min-h-0 flex-1 resize-none rounded-lg border bg-background px-3 py-2 text-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        />
        <DialogFooter>
          <span className="mr-auto self-center text-xs text-muted-foreground">
            Cmd/Ctrl+Enter to save, Esc to cancel
          </span>
          <Button variant="ghost" onClick={() => setEditing(false)}>
            Cancel
          </Button>
          <Button onClick={handleSubmitEdit}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )

  return (
    // CTR-0103: data attributes let the Message Navigator resolve user-turn
    // nodes for scrollspy and jump-to-turn.
    <div
      data-message-id={message.id}
      data-message-role={message.role}
      className={cn('group/msg flex gap-3', compact ? 'px-3 py-1' : 'px-4 py-1')}>
      <Avatar className={cn('mt-0.5 shrink-0', compact ? 'h-6 w-6' : 'h-7 w-7')}>
        <AvatarFallback className={cn(isUser ? 'bg-primary text-primary-foreground' : 'bg-muted')}>
          {isUser ? (
            <User className={cn(compact ? 'h-3 w-3' : 'h-3.5 w-3.5')} />
          ) : (
            <Bot className={cn(compact ? 'h-3 w-3' : 'h-3.5 w-3.5')} />
          )}
        </AvatarFallback>
      </Avatar>
      <div className={cn('min-w-0 flex-1 text-sm leading-relaxed')}>
        {isUser ? (
          <div>
            {message.images && message.images.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-2">
                {message.images.map((img) => {
                  const isPdf = img.uri.endsWith('.pdf') || img.media_type === 'application/pdf'
                  if (isPdf) {
                    const filename = img.uri.split('/').pop() || 'document.pdf'
                    return (
                      <div key={img.uri} className="flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs">
                        <FileTextIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
                        <span className="truncate font-medium">{filename}</span>
                      </div>
                    )
                  }
                  const isGenerated = img.uri.includes('/generated_')
                  // Paint-origin images (CTR-0160/CTR-0161, PRP-0099) carry a
                  // re-edit affordance; a sent image is immutable, so editing
                  // opens the scene and produces a NEW composer attachment.
                  const isPaint = (img.uri.split('/').pop() ?? '').startsWith('paint_')
                  const imgEl = (
                    <AuthedImage
                      uri={img.uri}
                      alt="Attached"
                      className={
                        isGenerated
                          ? 'max-w-full rounded-lg border border-border/50 shadow-xs transition-shadow hover:shadow-md'
                          : 'max-h-48 max-w-xs rounded-lg border object-contain'
                      }
                    />
                  )
                  if (isPaint && onPaintEdit) {
                    return (
                      <div key={img.uri} className="group/paint relative inline-block">
                        <button
                          type="button"
                          onClick={() => openUploadFullSize(img.uri)}
                          className="block cursor-zoom-in"
                          aria-label="Open full size">
                          {imgEl}
                        </button>
                        <button
                          type="button"
                          onClick={() => onPaintEdit(img.uri)}
                          className="absolute right-1.5 top-1.5 flex h-7 items-center gap-1 rounded-md bg-foreground/70 px-2 text-xs text-background opacity-0 transition-opacity group-hover/paint:opacity-100"
                          aria-label="Edit paint">
                          <Pencil className="h-3.5 w-3.5" />
                          Edit
                        </button>
                      </div>
                    )
                  }
                  return (
                    <button
                      key={img.uri}
                      type="button"
                      onClick={() => openUploadFullSize(img.uri)}
                      className="block cursor-zoom-in"
                      aria-label="Open full size">
                      {imgEl}
                    </button>
                  )
                })}
              </div>
            )}
            <CollapsibleUserText content={message.content} />
            {/*
              Pre-commit send failure (CTR-0004 v2, PRP-0110 / UDR-0088 D3). The turn
              never reached the agent, so it is safe to re-send: nothing was persisted
              and nothing can double-execute (UDR-0088 D6). The text also went back
              into the composer, so the user can copy it or retry from here.
            */}
            {message.failed && (
              <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-600">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                <span>Could not reach the server. This message was not sent.</span>
                {onRetryTurn && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={() => onRetryTurn(message.id)}>
                    <RefreshCw className="mr-1 h-3 w-3" />
                    Retry
                  </Button>
                )}
              </div>
            )}
          </div>
        ) : (
          <>
            {message.activityLog && message.activityLog.length > 0 ? (
              message.activityLog.map((entry) => {
                if (entry.type === 'reasoning') {
                  const block = message.reasoningBlocks?.find((rb) => rb.id === entry.id)
                  return block ? <ThinkingBlock key={entry.id} block={block} /> : null
                }
                const tc = message.toolCalls?.find((t) => t.id === entry.id)
                return tc ? <ToolCallBlock key={entry.id} toolCall={tc} /> : null
              })
            ) : (
              <>
                {message.reasoningBlocks && message.reasoningBlocks.length > 0 && (
                  <ReasoningIndicator reasoningBlocks={message.reasoningBlocks} />
                )}
                <ToolCallIndicator toolCalls={message.toolCalls} />
              </>
            )}
            {isWaiting && (
              <div className="mb-2 flex items-center text-sm text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              </div>
            )}
            {/* MCP App View (CTR-0068) */}
            {!isUser && message.mcpApp && (
              <McpAppView
                event={message.mcpApp}
                toolResult={message.toolCalls?.find((tc) => tc.id === message.mcpApp?.call_id)?.result}
                toolArgs={message.toolCalls?.find((tc) => tc.id === message.mcpApp?.call_id)?.args}
              />
            )}
            {message.toolCalls && message.toolCalls.length > 0 && <WeatherToolResults toolCalls={message.toolCalls} />}
            {message.toolCalls && message.toolCalls.length > 0 && (
              <ImageGenerationResults toolCalls={message.toolCalls} onMaskEdit={onMaskEdit} />
            )}
            {message.content ? (
              // Structured output (CTR-0012 v11, PRP-0082, UDR-0058 D5): render the
              // JSON answer as a `json` code block (reuses CodeBlock copy/download)
              // instead of Markdown. Streaming partial JSON shows as it arrives.
              <MarkdownRenderer content={message.structured ? toJsonCodeFence(message.content) : message.content} />
            ) : (
              !isWaiting && <span className="inline-block h-4 w-1 animate-pulse bg-current" />
            )}
            {/* Unified in-progress indicator (PRP-0118 follow-up): while the turn is
                still running -- and text has already arrived, so the top isWaiting
                spinner is gone -- keep an animated "processing" cue at the tail until
                the run fully completes. Common to Prompt agents and Workflows (a
                workflow may pause between nodes / while an agent node thinks, which
                would otherwise look finished). */}
            {!isUser && isLoading && hasTextContent && (
              <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground/70">
                <Loader2 className="h-3 w-3 animate-spin" />
                <span>{message.runTarget?.startsWith('⧉') ? 'Running workflow…' : 'Working…'}</span>
              </div>
            )}
          </>
        )}

        {!isLoading && !editing && message.content && (
          <div className="mt-0.5 flex gap-0.5 opacity-0 transition-opacity group-hover/msg:opacity-100">
            <CopyButton text={message.content} />
            {onToggleMemoryLike && (
              <MemoryLikeButton messageIndex={messageIndex} status={memoryLikeStatus} onToggle={onToggleMemoryLike} />
            )}
            {tts && <TTSPlayButton message={message} tts={tts} />}
            {tts && <TTSDownloadButton message={message} messageIndex={messageIndex} tts={tts} />}
            {(isUser ? onEditUser : onEditAssistant) && (
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-muted-foreground hover:text-foreground"
                onClick={handleStartEdit}
                title="Edit message"
                aria-label="Edit message">
                <Pencil className="h-3 w-3" />
              </Button>
            )}
            {isUser && onSaveAsTemplate && (
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-muted-foreground hover:text-foreground"
                onClick={() => onSaveAsTemplate(message.content)}
                title="Save as template"
                aria-label="Save as template">
                <FileTextIcon className="h-3 w-3" />
              </Button>
            )}
            {!isUser && onRegenerateAssistant && (
              <div className="relative flex items-center">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 text-muted-foreground hover:text-foreground"
                  onClick={handleRegenerate}
                  title="Regenerate response"
                  aria-label="Regenerate response">
                  <RefreshCw className="h-3 w-3" />
                </Button>
                {availableModels && availableModels.length > 1 && onRegenerateWithModel && (
                  <>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-6 w-4 -ml-1 text-muted-foreground hover:text-foreground"
                      onClick={() => setRegenModelOpen((prev) => !prev)}
                      title="Regenerate with a different model"
                      aria-label="Regenerate with model">
                      <ChevronDown className="h-2.5 w-2.5" />
                    </Button>
                    {regenModelOpen && (
                      <>
                        <button
                          type="button"
                          tabIndex={-1}
                          className="fixed inset-0 z-40 cursor-default bg-transparent border-none"
                          onClick={() => setRegenModelOpen(false)}
                          aria-label="Close model menu"
                        />
                        <div className="absolute top-full mt-1 left-0 z-50 min-w-[240px] rounded-md border bg-popover p-1 shadow-md">
                          {availableModels.map((m) => (
                            <button
                              key={m}
                              type="button"
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-xs hover:bg-accent hover:text-accent-foreground"
                              onClick={() => {
                                setRegenModelOpen(false)
                                onRegenerateWithModel(message.id, m)
                              }}>
                              <RefreshCw className="h-3 w-3" />
                              <span>Regenerate with {m}</span>
                            </button>
                          ))}
                        </div>
                      </>
                    )}
                  </>
                )}
              </div>
            )}
            {!isUser && onBranch && (
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-muted-foreground hover:text-foreground"
                onClick={() => onBranch(messageIndex)}
                title="Branch into a new chat from here"
                aria-label="Branch in new chat">
                <GitBranch className="h-3 w-3" />
              </Button>
            )}
            {onDelete && (
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-muted-foreground hover:text-destructive"
                onClick={() => setDeleteConfirmOpen(true)}
                title="Delete message"
                aria-label="Delete message">
                <Trash2 className="h-3 w-3" />
              </Button>
            )}
            {!isUser && message.runTarget && (
              <span
                className="ml-1 rounded bg-primary/10 px-1 text-[11px] text-primary"
                title="Agent / workflow that produced this turn">
                {message.runTarget}
              </span>
            )}
            {!isUser && message.model && (
              <span className="ml-1 text-[11px] text-muted-foreground/50">{message.model}</span>
            )}
            {!isUser && message.reasoning && (
              <span className="ml-1 text-[11px] text-muted-foreground/50 capitalize" title="Reasoning effort">
                {message.reasoning}
              </span>
            )}
            {!isUser && message.verbosity && (
              <span className="ml-1 text-[11px] text-muted-foreground/50 capitalize" title="Verbosity">
                {message.verbosity}
              </span>
            )}
            {!isUser && message.structured && (
              <span
                className={cn(
                  'ml-1 text-[11px]',
                  message.usage?.output_status && !message.usage.output_status.parsed
                    ? 'text-amber-600/70 dark:text-amber-500/70'
                    : 'text-muted-foreground/50',
                )}
                title={
                  message.usage?.output_status && !message.usage.output_status.parsed
                    ? `Structured output (${message.usage.output_status.reason ?? 'not valid JSON'})`
                    : 'Structured output (JSON)'
                }>
                JSON
              </span>
            )}
            {!isUser && message.usage && (
              <span className="ml-1 text-[11px] tabular-nums text-muted-foreground/60">
                {message.usage.input_token_count?.toLocaleString() ?? '?'}in /{' '}
                {message.usage.output_token_count?.toLocaleString() ?? '?'}out
              </span>
            )}
          </div>
        )}

        <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete this message?</AlertDialogTitle>
              <AlertDialogDescription>This action cannot be undone.</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                onClick={() => onDelete?.(message.id)}>
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {/* Full-screen message edit modal (PRP-0084, CTR-0018 v1.3). Portaled,
            so its placement here is cosmetic; open is driven by `editing`. */}
        {renderEditForm()}
      </div>
    </div>
  )
}

// PRP-0074 (UDR-0050, Tier 1): memoize each row so a streaming tail-message
// delta -- which recreates the messages array on every CTR-0009 token -- only
// re-renders the changed message instead of all messages. Every prop passed by
// ChatPanel is referentially stable (useCallback-bound handlers + the stable
// onBranch reference), so the default shallow comparison is sufficient.
export const ChatMessageItem = memo(ChatMessageItemImpl)
