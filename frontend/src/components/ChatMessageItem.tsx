import {
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
  Trash2,
  User,
  Volume2,
} from 'lucide-react'
import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react'
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
import { WeatherToolResults } from '@/components/WeatherCard'
import { cn } from '@/lib/utils'
import type { ChatMessage } from '@/types/chat'

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
  onEditAssistant?: (messageId: string, newContent: string) => void
  onRegenerateAssistant?: (messageId: string) => void
  onDelete?: (messageId: string) => void
  onBranch?: () => void
  onSaveAsTemplate?: (content: string) => void
  onMaskEdit?: (imageUrl: string) => void
  /** Available models for regenerate-with-model dropdown (CTR-0071) */
  availableModels?: string[]
  /** Regenerate with a specific model (CTR-0071) */
  onRegenerateWithModel?: (messageId: string, model: string) => void
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
      aria-label="Copy message">
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
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
      aria-label="Download audio">
      {isThisDownloading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
    </Button>
  )
}

export function ChatMessageItem({
  message,
  messageIndex = 0,
  compact,
  isLoading,
  tts,
  onEditUser,
  onEditAssistant,
  onRegenerateAssistant,
  onDelete,
  onBranch,
  onSaveAsTemplate,
  onMaskEdit,
  availableModels,
  onRegenerateWithModel,
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
      if (e.key === 'Enter' && !e.shiftKey) {
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

  const renderEditForm = () => (
    <div className="flex flex-col gap-2">
      <textarea
        ref={editRef}
        value={editValue}
        onChange={(e) => setEditValue(e.target.value)}
        onKeyDown={handleEditKeyDown}
        className="w-full resize-none rounded-lg border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        rows={3}
      />
      <div className="flex gap-2">
        <Button size="sm" onClick={handleSubmitEdit}>
          Submit
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
          Cancel
        </Button>
      </div>
    </div>
  )

  return (
    <div className={cn('group/msg flex gap-3', compact ? 'px-3 py-2' : 'px-4 py-2')}>
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
        {editing ? (
          renderEditForm()
        ) : isUser ? (
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
                  return (
                    <a key={img.uri} href={img.uri} target="_blank" rel="noopener noreferrer" className="block">
                      <img
                        src={img.uri}
                        alt="Attached"
                        className={
                          isGenerated
                            ? 'max-w-full rounded-lg border border-border/50 shadow-sm transition-shadow hover:shadow-md'
                            : 'max-h-48 max-w-xs rounded-lg border object-contain'
                        }
                      />
                    </a>
                  )
                })}
              </div>
            )}
            <div className="whitespace-pre-wrap">{message.content}</div>
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
              <MarkdownRenderer content={message.content} />
            ) : (
              !isWaiting && <span className="inline-block h-4 w-1 animate-pulse bg-current" />
            )}
          </>
        )}

        {!isLoading && !editing && message.content && (
          <div className="mt-0.5 flex gap-0.5 opacity-0 transition-opacity group-hover/msg:opacity-100">
            <CopyButton text={message.content} />
            {tts && <TTSPlayButton message={message} tts={tts} />}
            {tts && <TTSDownloadButton message={message} messageIndex={messageIndex} tts={tts} />}
            {(isUser ? onEditUser : onEditAssistant) && (
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-muted-foreground hover:text-foreground"
                onClick={handleStartEdit}
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
                        <div className="absolute top-full mt-1 left-0 z-50 min-w-[180px] rounded-md border bg-popover p-1 shadow-md">
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
                onClick={onBranch}
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
                aria-label="Delete message">
                <Trash2 className="h-3 w-3" />
              </Button>
            )}
            {!isUser && message.model && (
              <span className="ml-1 text-[11px] text-muted-foreground/50">{message.model}</span>
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
      </div>
    </div>
  )
}
