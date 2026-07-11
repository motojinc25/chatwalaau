import { File, FileText, Folder, Loader2, Mic, Paintbrush, Paperclip, Plus, SendHorizontal, Square } from 'lucide-react'
import { forwardRef, type KeyboardEvent, useCallback, useImperativeHandle, useRef, useState } from 'react'
import { ImageThumbnails } from '@/components/ImageThumbnails'
import { PdfFileCard } from '@/components/PdfFileCard'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { WaveformVisualizer } from '@/components/WaveformVisualizer'
import type { ImageAttachment } from '@/hooks/useImageAttachment'
import { useVoiceInput } from '@/hooks/useVoiceInput'
import {
  type CommandEntry,
  type CommandsInventory,
  type CompletionContext,
  defaultSkillInvocation,
  getCompletionContext,
  parseCommandInput,
  resolveCommand,
  substituteArguments,
} from '@/lib/slashCommands'
import { cn } from '@/lib/utils'
import type { ImageRef } from '@/types/chat'

export interface ChatInputHandle {
  insertText: (text: string) => void
}

interface Suggestion {
  text: string
  hint?: string
  desc?: string
  isDir?: boolean
}

interface ChatInputProps {
  /**
   * Send the composed message. Resolves to `false` when the send failed BEFORE the
   * AG-UI stream committed, which tells the composer to restore the text
   * (CTR-0004 v2, PRP-0110). Any other outcome is treated as committed.
   */
  onSend: (message: string, images?: ImageRef[]) => Promise<boolean>
  onStop: () => void
  isLoading: boolean
  attachments?: ImageAttachment[]
  onAddFiles?: (files: FileList) => void
  onRemoveAttachment?: (id: string) => void
  getImageRefs?: () => ImageRef[]
  isUploading?: boolean
  bgEnabled?: boolean
  onOpenTemplates?: () => void
  /** Open the Paint editor from the Plus menu (CTR-0160, PRP-0099). */
  onOpenPaint?: () => void
  /** Re-edit a paint-origin attachment thumbnail (CTR-0160/CTR-0161, PRP-0099). */
  onEditAttachment?: (attachment: ImageAttachment) => void
  /** Slash commands (CTR-0128, PRP-0088). */
  onSlashModel?: (model: string) => boolean
  onSlashHelp?: () => void
  onSlashCron?: () => void
  onSlashFiles?: () => void
  availableModels?: string[]
  /**
   * Temporary Chat (CTR-0107, PRP-0076). When true the input is rendered with a
   * dark/black treatment and an honest "not saved" notice so the ephemeral mode
   * is unmistakable.
   */
  temporary?: boolean
}

export const ChatInput = forwardRef<ChatInputHandle, ChatInputProps>(function ChatInput(
  {
    onSend,
    onStop,
    isLoading,
    attachments = [],
    onAddFiles,
    onRemoveAttachment,
    getImageRefs,
    isUploading,
    bgEnabled,
    onOpenTemplates,
    onOpenPaint,
    onEditAttachment,
    onSlashModel,
    onSlashHelp,
    onSlashCron,
    onSlashFiles,
    availableModels = [],
    temporary = false,
  },
  ref,
) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const pdfInputRef = useRef<HTMLInputElement>(null)

  // --- Slash command completion state (CTR-0128, PRP-0088) ---
  const invRef = useRef<{ data: CommandsInventory | null; at: number }>({ data: null, at: 0 })
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [activeIdx, setActiveIdx] = useState(0)
  const [ghost, setGhost] = useState('')
  const ctxRef = useRef<CompletionContext | null>(null)
  const fileSeq = useRef(0)
  const menuOpen = suggestions.length > 0

  const resize = useCallback(() => {
    const textarea = textareaRef.current
    if (textarea) {
      textarea.style.height = 'auto'
      textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
    }
  }, [])

  const closeMenu = useCallback(() => {
    setSuggestions([])
    setActiveIdx(0)
    setGhost('')
    ctxRef.current = null
  }, [])

  const ensureInventory = useCallback(async (): Promise<CommandsInventory | null> => {
    const now = Date.now()
    if (invRef.current.data && now - invRef.current.at < 5000) return invRef.current.data
    try {
      const res = await fetch('/api/commands')
      if (res.ok) invRef.current = { data: (await res.json()) as CommandsInventory, at: now }
    } catch {
      // degrade gracefully: keep whatever we had (or null)
    }
    return invRef.current.data
  }, [])

  const applyItems = useCallback((ctx: CompletionContext, items: Suggestion[], caretAtEnd: boolean) => {
    ctxRef.current = ctx
    setSuggestions(items)
    setActiveIdx(0)
    const top = items[0]
    if (
      caretAtEnd &&
      top &&
      top.text.toLowerCase().startsWith(ctx.query.toLowerCase()) &&
      top.text.length > ctx.query.length
    ) {
      setGhost(top.text.slice(ctx.query.length))
    } else {
      setGhost('')
    }
  }, [])

  const recompute = useCallback(async () => {
    const textarea = textareaRef.current
    if (!textarea) return
    const text = textarea.value
    const caret = textarea.selectionStart ?? text.length
    const ctx = getCompletionContext(text, caret)
    if (!ctx) {
      closeMenu()
      return
    }
    const caretAtEnd = caret === text.length

    if (ctx.kind === 'file') {
      const seq = ++fileSeq.current
      try {
        const res = await fetch(`/api/workspace/files?prefix=${encodeURIComponent(ctx.query)}`)
        if (seq !== fileSeq.current) return // stale
        const data = res.ok ? await res.json() : null
        const entries: Suggestion[] = (data?.entries ?? []).map((e: { path: string; is_dir: boolean }) => ({
          text: e.path,
          isDir: e.is_dir,
          hint: e.is_dir ? 'dir' : undefined,
        }))
        if (entries.length === 0) closeMenu()
        else applyItems(ctx, entries, caretAtEnd)
      } catch {
        closeMenu()
      }
      return
    }

    const inv = await ensureInventory()
    const q = ctx.query.toLowerCase()
    let items: Suggestion[] = []
    if (ctx.kind === 'command') {
      items = (inv?.commands ?? [])
        .filter((c) => c.token.toLowerCase().startsWith(q) || c.aliases.some((a) => a.toLowerCase().startsWith(q)))
        .map((c) => ({ text: c.token, hint: c.args_hint, desc: c.description }))
    } else if (ctx.kind === 'value') {
      const cmd = (ctx.command ?? '').toLowerCase()
      if (cmd === 'model' || cmd === 'm') {
        items = availableModels.filter((m) => m.toLowerCase().startsWith(q)).map((m) => ({ text: m }))
      } else if (cmd === 'skill') {
        items = (inv?.commands ?? [])
          .filter((c) => c.source === 'skill' && c.token.toLowerCase().startsWith(q))
          .map((c) => ({ text: c.token, desc: c.description }))
      } else if (cmd === 'prompt' || cmd === 'p') {
        items = (inv?.commands ?? [])
          .filter((c) => c.source === 'prompt' && c.token.toLowerCase().startsWith(q))
          .map((c) => ({ text: c.token, desc: c.description }))
      }
    }
    if (items.length === 0) closeMenu()
    else applyItems(ctx, items.slice(0, 50), caretAtEnd)
  }, [availableModels, applyItems, closeMenu, ensureInventory])

  const setValueAndResize = useCallback(
    (next: string, caret?: number) => {
      setValue(next)
      requestAnimationFrame(() => {
        const textarea = textareaRef.current
        if (textarea) {
          if (caret !== undefined) {
            textarea.selectionStart = caret
            textarea.selectionEnd = caret
          }
          resize()
          textarea.focus()
        }
        void recompute()
      })
    },
    [resize, recompute],
  )

  useImperativeHandle(ref, () => ({
    insertText: (text: string) => {
      setValue(text)
      closeMenu()
      requestAnimationFrame(() => {
        resize()
        textareaRef.current?.focus()
      })
    },
  }))

  const applySuggestion = useCallback(
    (item: Suggestion) => {
      const ctx = ctxRef.current
      const textarea = textareaRef.current
      if (!ctx || !textarea) return
      const text = textarea.value
      const queryEnd = ctx.start + ctx.query.length
      const after = text.slice(queryEnd)
      const trailing = ctx.kind === 'file' ? (item.isDir ? '/' : ' ') : ' '
      const next = text.slice(0, ctx.start) + item.text + trailing + after
      const caret = ctx.start + item.text.length + trailing.length
      setValueAndResize(next, caret)
    },
    [setValueAndResize],
  )

  const acceptTopOrActive = useCallback(() => {
    const item = suggestions[activeIdx] ?? suggestions[0]
    if (item) applySuggestion(item)
  }, [suggestions, activeIdx, applySuggestion])

  const handleTranscribed = useCallback(
    (text: string) => {
      setValue((prev) => {
        const separator = prev && !prev.endsWith(' ') ? ' ' : ''
        return prev + separator + text
      })
      requestAnimationFrame(() => {
        resize()
        textareaRef.current?.focus()
      })
    },
    [resize],
  )

  const {
    voiceState,
    waveformData,
    startRecording,
    stopRecording,
    error: voiceError,
  } = useVoiceInput(handleTranscribed)

  // Dispatch a recognized slash command (UDR-0066 D1). /help and /model are UI
  // actions; /prompt and /skill expand to ORDINARY message text placed in the
  // input for review/send. Returns true if it consumed the input (do not send).
  const dispatchCommand = useCallback(
    async (cmd: CommandEntry, token: string, argStr: string): Promise<boolean> => {
      const inv = invRef.current.data
      const expandPrompt = async (templateId: string, commandWord: string, args: string) => {
        try {
          const res = await fetch(`/api/templates/${templateId}`)
          if (!res.ok) return
          const tpl = await res.json()
          setValueAndResize(substituteArguments(String(tpl.body ?? ''), commandWord, args))
        } catch {
          // leave the input untouched on failure
        }
      }

      if (cmd.source === 'prompt') {
        await expandPrompt(cmd.ref, token, argStr)
        return true
      }
      if (cmd.source === 'skill') {
        setValueAndResize(defaultSkillInvocation(cmd.ref, argStr))
        return true
      }
      // builtin
      switch (cmd.token) {
        case 'help':
          onSlashHelp?.()
          setValue('')
          requestAnimationFrame(resize)
          return true
        case 'cron':
          // Open the Cron scheduler portal (CTR-0135). Consume the input even when
          // no handler is wired (compact scenarios) so "/cron" is never sent as text.
          onSlashCron?.()
          setValue('')
          requestAnimationFrame(resize)
          return true
        case 'files':
          // Open the File Explorer overlay (CTR-0137). Consume the input even when no
          // handler is wired (compact scenarios) so "/files" is never sent as text.
          onSlashFiles?.()
          setValue('')
          requestAnimationFrame(resize)
          return true
        case 'model': {
          const target = argStr.trim().split(/\s+/)[0] ?? ''
          if (target && onSlashModel?.(target)) {
            setValue('')
            requestAnimationFrame(resize)
            return true
          }
          // unknown / empty model: keep the text so the user can correct it
          return true
        }
        case 'prompt': {
          const name = argStr.trim().split(/\s+/)[0] ?? ''
          const rest = argStr.trim().slice(name.length).trim()
          const entry = (inv?.commands ?? []).find(
            (c) => c.source === 'prompt' && c.token.toLowerCase() === name.toLowerCase(),
          )
          if (entry) await expandPrompt(entry.ref, name, rest)
          return true
        }
        case 'skill': {
          const name = argStr.trim().split(/\s+/)[0] ?? ''
          const rest = argStr.trim().slice(name.length).trim()
          if (name) setValueAndResize(defaultSkillInvocation(name, rest))
          return true
        }
        default:
          return false
      }
    },
    [onSlashHelp, onSlashModel, onSlashCron, onSlashFiles, resize, setValueAndResize],
  )

  const handleSend = async () => {
    if ((!value.trim() && attachments.length === 0) || isLoading || isUploading) return
    // Slash command dispatch (UDR-0066 D1/D3): only when the head token resolves
    // in the inventory; otherwise the input is sent as a normal message.
    const parsed = parseCommandInput(value)
    if (parsed) {
      const inv = await ensureInventory()
      const cmd = resolveCommand(inv, parsed.token)
      if (cmd) {
        closeMenu()
        await dispatchCommand(cmd, parsed.token, parsed.argStr)
        return
      }
    }
    const images = getImageRefs?.()
    // PRP-0110 / CTR-0004 v2 / UDR-0088 D3: the composer clears optimistically so
    // the send feels instant, but the text is held until the send COMMITS (the
    // AG-UI stream emitted its first event). On a pre-commit failure -- the server
    // is down, restarting, or the session expired -- we hand the text back instead
    // of destroying it. The restore is conditional on the textarea still being
    // empty, so anything the user typed while the request was in flight wins.
    const pending = value
    setValue('')
    closeMenu()
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    const committed = await onSend(pending, images && images.length > 0 ? images : undefined)
    if (committed === false && textareaRef.current?.value === '') {
      setValueAndResize(pending, pending.length)
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (menuOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActiveIdx((i) => (i + 1) % suggestions.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActiveIdx((i) => (i - 1 + suggestions.length) % suggestions.length)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        closeMenu()
        return
      }
      if ((e.key === 'Enter' || e.key === 'Tab') && !e.nativeEvent.isComposing) {
        e.preventDefault()
        acceptTopOrActive()
        return
      }
    } else if (ghost && (e.key === 'Tab' || (e.key === 'ArrowRight' && !e.shiftKey)) && !e.nativeEvent.isComposing) {
      // Accept inline ghost completion.
      e.preventDefault()
      acceptTopOrActive()
      return
    }
    // Skip while an IME composition is in progress (CJK kanji/pinyin/hangul
    // conversion). Without this, pressing Enter to commit the IME selection
    // would submit the message instead of confirming the conversion.
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void handleSend()
    }
  }

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value)
    void recompute()
  }

  const handleInput = () => {
    resize()
  }

  const handleFileSelect = () => {
    fileInputRef.current?.click()
  }

  const handlePdfSelect = () => {
    pdfInputRef.current?.click()
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files && files.length > 0 && onAddFiles) {
      onAddFiles(files)
    }
    // Reset so the same file can be selected again
    e.target.value = ''
  }

  const isRecording = voiceState === 'recording'
  const isTranscribing = voiceState === 'transcribing'

  return (
    <div className="p-4 pb-5">
      <div className="mx-auto max-w-3xl">
        {isRecording ? (
          <WaveformVisualizer data={waveformData} onStop={stopRecording} />
        ) : (
          <div
            className={cn(
              'relative flex flex-col rounded-lg border',
              // Temporary Chat (CTR-0107): dark/black treatment regardless of
              // theme so the ephemeral mode is unmistakable.
              temporary ? 'border-neutral-700 bg-neutral-900 text-neutral-100' : 'bg-background',
              temporary
                ? 'ring-offset-background focus-within:ring-2 focus-within:ring-neutral-500 focus-within:ring-offset-2'
                : bgEnabled
                  ? 'ring-2 ring-blue-500 ring-offset-2 ring-offset-background'
                  : 'ring-offset-background focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2',
            )}>
            {/* Slash command completion menu (CTR-0128). Anchored above the input. */}
            {menuOpen && (
              <div className="absolute bottom-full left-0 right-0 mb-2 z-50 max-h-64 overflow-y-auto rounded-md border bg-popover p-1 shadow-md">
                {suggestions.map((s, i) => (
                  <button
                    key={s.text}
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault()
                      applySuggestion(s)
                    }}
                    onMouseEnter={() => setActiveIdx(i)}
                    className={cn(
                      'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs',
                      i === activeIdx ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50',
                    )}>
                    {ctxRef.current?.kind === 'file' &&
                      (s.isDir ? (
                        <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      ) : (
                        <File className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      ))}
                    <span className="shrink-0 font-medium">
                      {ctxRef.current?.kind === 'command' ? `/${s.text}` : s.text}
                    </span>
                    {s.hint && <span className="shrink-0 text-muted-foreground">{s.hint}</span>}
                    {s.desc && <span className="ml-auto truncate text-muted-foreground">{s.desc}</span>}
                  </button>
                ))}
              </div>
            )}
            {onRemoveAttachment && (
              <>
                <ImageThumbnails
                  attachments={attachments.filter((a) => a.mediaType !== 'application/pdf')}
                  onRemove={onRemoveAttachment}
                  onEdit={onEditAttachment}
                />
                {attachments.filter((a) => a.mediaType === 'application/pdf').length > 0 && (
                  <div className="flex flex-wrap gap-2 px-3 pt-2">
                    {attachments
                      .filter((a) => a.mediaType === 'application/pdf')
                      .map((a) => (
                        <PdfFileCard
                          key={a.id}
                          filename={a.file.name}
                          size={a.file.size}
                          status={a.status}
                          onRemove={() => onRemoveAttachment(a.id)}
                        />
                      ))}
                  </div>
                )}
              </>
            )}
            <div className="flex items-end">
              {onAddFiles && (
                <>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept="image/jpeg,image/png,image/gif,image/webp"
                    multiple
                    onChange={handleFileChange}
                    className="hidden"
                  />
                  <input
                    ref={pdfInputRef}
                    type="file"
                    accept="application/pdf"
                    multiple
                    onChange={handleFileChange}
                    className="hidden"
                  />
                  <div className="flex shrink-0 items-center p-1">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <button
                          type="button"
                          className={cn(
                            'inline-flex h-8 w-8 items-center justify-center rounded-md',
                            'text-muted-foreground hover:text-foreground',
                            'transition-colors',
                          )}
                          aria-label="Attach file">
                          <Plus className="h-4 w-4" />
                        </button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="start">
                        <DropdownMenuItem onClick={handleFileSelect}>
                          <Paperclip className="mr-2 h-4 w-4" />
                          Attach image
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={handlePdfSelect}>
                          <File className="mr-2 h-4 w-4" />
                          Attach PDF
                        </DropdownMenuItem>
                        {onOpenPaint && (
                          <DropdownMenuItem onClick={onOpenPaint}>
                            <Paintbrush className="mr-2 h-4 w-4" />
                            Paint
                          </DropdownMenuItem>
                        )}
                        {onOpenTemplates && (
                          <DropdownMenuItem onClick={onOpenTemplates}>
                            <FileText className="mr-2 h-4 w-4" />
                            Use template
                          </DropdownMenuItem>
                        )}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </>
              )}
              <div className={cn('relative flex-1', !onAddFiles && 'pl-0')}>
                {/* Ghost completion mirror (CTR-0128): identical text metrics to the
                    textarea; the value copy is invisible so only the ghost suffix shows. */}
                {ghost && (
                  <div
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-3 py-2 text-sm">
                    <span className="invisible">{value}</span>
                    <span className="text-muted-foreground/50">{ghost}</span>
                  </div>
                )}
                <textarea
                  ref={textareaRef}
                  value={value}
                  onChange={handleChange}
                  onKeyDown={handleKeyDown}
                  onInput={handleInput}
                  onKeyUp={() => void recompute()}
                  onClick={() => void recompute()}
                  onBlur={() => closeMenu()}
                  placeholder={isTranscribing ? 'Transcribing...' : 'Type a message...'}
                  rows={1}
                  className={cn(
                    'relative w-full resize-none bg-transparent px-3 py-2 text-sm',
                    temporary ? 'text-neutral-100 placeholder:text-neutral-400' : 'placeholder:text-muted-foreground',
                    'focus-visible:outline-hidden',
                    'disabled:cursor-not-allowed disabled:opacity-50',
                  )}
                  disabled={isLoading || isTranscribing}
                />
              </div>
              <div className="flex shrink-0 items-center gap-0.5 p-1">
                {isLoading ? (
                  <Button
                    variant="destructive"
                    size="icon"
                    className="h-8 w-8"
                    onClick={onStop}
                    aria-label="Stop generation">
                    <Square className="h-4 w-4" />
                  </Button>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={startRecording}
                      disabled={isTranscribing}
                      className={cn(
                        'inline-flex h-8 w-8 items-center justify-center rounded-md',
                        'text-muted-foreground hover:text-foreground',
                        'disabled:pointer-events-none disabled:opacity-50',
                        'transition-colors',
                      )}
                      aria-label={isTranscribing ? 'Transcribing' : 'Voice input'}>
                      {isTranscribing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mic className="h-4 w-4" />}
                    </button>
                    <Button
                      size="icon"
                      className="h-8 w-8"
                      onClick={() => void handleSend()}
                      disabled={(!value.trim() && attachments.length === 0) || isTranscribing || isUploading}
                      aria-label="Send message">
                      <SendHorizontal className="h-4 w-4" />
                    </Button>
                  </>
                )}
              </div>
            </div>
          </div>
        )}
        {voiceError && <p className="mt-1 text-xs text-destructive">{voiceError}</p>}
        {temporary && (
          // UDR-0052: honest wording -- "not in history / not used for
          // personalization", NOT "never stored anywhere" (the conversation is
          // briefly quarantine-retained for safety).
          <p className="mt-1.5 text-center text-xs text-muted-foreground">
            Temporary chat: not saved to your history and not used to personalize future chats.
          </p>
        )}
      </div>
    </div>
  )
})
