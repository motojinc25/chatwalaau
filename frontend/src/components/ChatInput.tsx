import {
  AudioLines,
  File,
  FileText,
  Folder,
  Loader2,
  Mic,
  Paintbrush,
  Paperclip,
  Plus,
  SendHorizontal,
  Square,
  TriangleAlert,
} from 'lucide-react'
import {
  forwardRef,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useId,
  useImperativeHandle,
  useRef,
  useState,
} from 'react'
import { ImageThumbnails } from '@/components/ImageThumbnails'
import { LiveConversationBar } from '@/components/LiveConversationBar'
import { PdfFileCard } from '@/components/PdfFileCard'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { WaveformVisualizer } from '@/components/WaveformVisualizer'
import type { ImageAttachment } from '@/hooks/useImageAttachment'
import type { LiveState } from '@/hooks/useLiveVoice'
import { useVoiceInput } from '@/hooks/useVoiceInput'
import type { ContextLevel } from '@/lib/contextOccupancy'
import { isCommandOfferedOnNarrow, useChatSurfaceTier } from '@/lib/narrowSurface'
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

// Keys that only move the completion menu's selection. While the menu is open they
// change neither the text nor the caret (handleKeyDown calls preventDefault), so there
// is nothing to recompute -- and recomputing is exactly what used to reset the highlight
// to 0 on every arrow press, making the arrow keys look dead (PRP-0134).
// preventDefault() stops the caret; it does not stop keyup from firing.
const MENU_NAV_KEYS = new Set(['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'])

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
  onOpenTemplates?: () => void
  /** Open the Paint editor from the Plus menu (CTR-0160, PRP-0099). */
  onOpenPaint?: () => void
  /** Re-edit a paint-origin attachment thumbnail (CTR-0160/CTR-0161, PRP-0099). */
  onEditAttachment?: (attachment: ImageAttachment) => void
  /** Slash commands (CTR-0128, PRP-0088). */
  /**
   * `/model` handler. Since PRP-0184 it OPENS the run-target surface rather than
   * switching a per-message model (UDR-0166 D1); the argument is always ''.
   */
  onSlashModel?: (model: string) => boolean
  onSlashHelp?: () => void
  onSlashCron?: () => void
  onSlashFiles?: () => void
  /**
   * Temporary Chat (CTR-0107, PRP-0076). When true the input is rendered with a
   * dark/black treatment and an honest "not saved" notice so the ephemeral mode
   * is unmistakable.
   */
  temporary?: boolean
  /**
   * Run-target entry in the control row (CTR-0221, PRP-0186, UDR-0168 D4).
   *
   * MOVED here from the strip above the composer, and reduced to an ICON. The icon
   * can only carry the KIND -- every Prompt agent is a Bot -- so `name` is what the
   * trigger states in BOTH its `title` and its `aria-label`. That is not a nicety:
   * activating a Prompt agent is SERVER-WIDE (UDR-0158 D3), so "which agent" is a
   * fact about what the next send does to every client, and an icon that cannot
   * answer it would be the unexplained state UDR-0102 exists to prevent.
   */
  runTarget?: {
    name: string
    icon: ReactNode
    /** Opens the run-target picker. The composer never applies a choice itself. */
    onOpen: () => void
  }
  /**
   * Context window occupancy (CTR-0041 v5, UDR-0168 D5/D6).
   *
   * ABSENT means "nothing to say" -- below the warning threshold, or no turn measured
   * yet -- and the composer renders its normal outline. Present, it tints the outline
   * and states itself in text, because a ring alone is invisible to a colour-blind
   * operator and silent to a screen reader.
   */
  context?: { level: ContextLevel; description: string }
  /**
   * Live voice conversation (CTR-0228 / CTR-0221, PRP-0188, UDR-0170 D9 / D11).
   *
   * ABSENT means Live is not offered here -- the server has it off, the run-target is
   * not a Prompt agent, or this is not the /chat surface -- and the entry is simply
   * not rendered (the caller decides membership, CTR-0221). Present, the entry sits
   * right of Voice Input; while a session runs the whole control row becomes the Live
   * row. Step 3 reverses Q2: typing stays possible and goes to the Live conversation.
   */
  live?: {
    state: LiveState
    levels: number[]
    muted: boolean
    working: boolean
    workingCount?: number
    remainingSeconds: number | null
    error: string | null
    notice: string | null
    onStart: () => void
    onStop: () => void
    onToggleMute: () => void
    /** Step 3: text typed during Live. Resolves false when it was not accepted. */
    onSendText?: (text: string) => Promise<boolean>
  }
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
    onOpenTemplates,
    onOpenPaint,
    onEditAttachment,
    onSlashModel,
    onSlashHelp,
    onSlashCron,
    onSlashFiles,
    temporary = false,
    runTarget,
    context,
    live,
  },
  ref,
) {
  const [value, setValue] = useState('')
  // Chat surface tier (PRP-0171, UDR-0153). Wide / unmanaged (the compact /popup and
  // /sidebar panels) keeps today's behaviour exactly.
  const surface = useChatSurfaceTier()
  // Voice input needs getUserMedia, which exists only in a secure context (UDR-0153 D8).
  // Loopback is a secure context, so a localhost operator never loses the button.
  const voiceOffered = !surface.managed || (typeof window !== 'undefined' && window.isSecureContext)
  const controlSize = surface.narrow ? 'h-10 w-10' : 'h-8 w-8'
  // 16 px on a phone; on the /chat surface also on iOS at any width (v0.155.2), because
  // iPadOS Safari zooms the page into an input smaller than 16 px on focus. The @supports
  // query matches iOS / iPadOS WebKit only, so desktop browsers keep 14 px.
  const textSize = surface.narrow
    ? 'text-base'
    : surface.managed
      ? 'text-sm [@supports(-webkit-touch-callout:none)]:text-base'
      : 'text-sm'
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const pdfInputRef = useRef<HTMLInputElement>(null)
  // The id `aria-describedby` points at. Stable per mount, and unique because /chat,
  // /popup and /sidebar can be open in one browser at once.
  const contextNoteId = useId()

  // --- Slash command completion state (CTR-0128, PRP-0088) ---
  const invRef = useRef<{ data: CommandsInventory | null; at: number }>({ data: null, at: 0 })
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [activeIdx, setActiveIdx] = useState(0)
  const [ghost, setGhost] = useState('')
  const ctxRef = useRef<CompletionContext | null>(null)
  // Keys of the suggestion list last handed to applyItems, so an identical
  // recompute does not disturb the operator's highlight (PRP-0134).
  const appliedKeysRef = useRef('')
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
    appliedKeysRef.current = ''
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
    // PRP-0134: reset the highlight ONLY when the list actually changed. This used to be
    // an unconditional setActiveIdx(0), which made the arrow keys look dead: the keydown
    // handler moved the selection, and the textarea's onKeyUp -> recompute() put it
    // straight back on 0 before it could be seen. Comparing against the last applied
    // list is the load-bearing half of the fix -- it protects the selection from ANY
    // caller of recompute(), not just the one that was found. Compared through a ref,
    // not inside the state updater, because an updater must stay pure.
    const keys = items.map((s) => s.text).join(' ')
    if (keys !== appliedKeysRef.current) {
      appliedKeysRef.current = keys
      setSuggestions(items)
      setActiveIdx(0)
    }
    const top = items[0]
    if (
      caretAtEnd &&
      top?.text.toLowerCase().startsWith(ctx.query.toLowerCase()) &&
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
        // Narrow viewport: builtins are an allowlist (PRP-0171, UDR-0153 D3).
        .filter((c) => !surface.narrow || isCommandOfferedOnNarrow(c))
        .filter((c) => c.token.toLowerCase().startsWith(q) || c.aliases.some((a) => a.toLowerCase().startsWith(q)))
        .map((c) => ({ text: c.token, hint: c.args_hint, desc: c.description }))
    } else if (ctx.kind === 'value') {
      const cmd = (ctx.command ?? '').toLowerCase()
      if (cmd === 'skill') {
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
  }, [applyItems, closeMenu, ensureInventory, surface.narrow])

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
          // PRP-0184 (UDR-0166 D1/D12): the model is a property of the RUN-TARGET, so
          // `/model` no longer switches a per-message model -- it OPENS the run-target
          // surface, where the model is chosen and persisted. An argument is accepted
          // and ignored rather than rejected, so the old muscle memory still lands
          // somewhere useful instead of erroring.
          onSlashModel?.('')
          setValue('')
          requestAnimationFrame(resize)
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
    // Step 3 (UDR-0170 D13): during Live, typed text joins the Live conversation. No
    // slash commands and no attachments there -- text only.
    if (liveActive && live?.onSendText) {
      const text = value.trim()
      if (!text) return
      setValue('')
      if (textareaRef.current) textareaRef.current.style.height = 'auto'
      const ok = await live.onSendText(text)
      if (!ok && textareaRef.current?.value === '') setValueAndResize(text, text.length)
      return
    }
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
    // On a narrow (phone-sized) viewport Enter inserts a newline and the send button
    // sends (PRP-0171, UDR-0153 D7 as amended in v0.155.1). Decided by WIDTH, not by
    // touch: a Windows touchscreen laptop can report `pointer: coarse` while it is used
    // with a keyboard. The IME guard above still applies.
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !surface.narrow) {
      e.preventDefault()
      void handleSend()
    }
  }

  const handleKeyUp = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (menuOpen && MENU_NAV_KEYS.has(e.key)) return
    void recompute()
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
  // A Live session owns the composer until it ends (PRP-0188, Q2).
  const liveActive = live !== undefined && live.state !== 'idle'

  return (
    <div className="p-4 pb-5">
      <div className="mx-auto max-w-3xl">
        {
          /* The composer is rendered in EVERY voice state (operator-reported defect).
             It used to be REPLACED by the waveform while recording: the draft text
             stayed in state and returned afterwards, but the operator watched a
             multi-line message they had just typed vanish the moment they pressed the
             microphone, which is indistinguishable from having lost it. Only the
             control row changes now; row 1 keeps the text, visible and editable. */
          <div
            // Context occupancy is the composer's OUTLINE state (UDR-0168 D5). The
            // `title` / `aria-describedby` pair is the non-colour equivalent and is
            // load-bearing, not decoration: this replaced a bar that carried its
            // percentage as text, so a colour-only successor would be a regression for
            // anyone who cannot perceive the hue or is using a screen reader.
            title={context?.description}
            aria-describedby={context ? contextNoteId : undefined}
            className={cn(
              'relative flex flex-col rounded-lg border',
              // Temporary Chat (CTR-0107): dark/black treatment regardless of
              // theme so the ephemeral mode is unmistakable.
              temporary ? 'border-neutral-700 bg-neutral-900 text-neutral-100' : 'bg-background',
              temporary
                ? 'ring-offset-background focus-within:ring-2 focus-within:ring-neutral-500 focus-within:ring-offset-2'
                : 'ring-offset-background focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2',
              // Composed ON TOP of the temporary treatment rather than replacing it:
              // that one is a background and text colour, this is a border, so a
              // temporary chat near its window shows both facts at once.
              context?.level === 'warning' && 'border-amber-500 ring-1 ring-amber-500/40',
              context?.level === 'critical' && 'border-red-500 ring-1 ring-red-500/40',
            )}>
            {/* The accessible text behind the outline (D5). Visually hidden -- the
                colour already says it to anyone who can see it -- but announced by a
                screen reader and readable by hovering the composer. */}
            {context && (
              <span id={contextNoteId} className="sr-only">
                {context.description}
              </span>
            )}
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
            {/* ROW 1 -- the textarea, at full width (CTR-0221, UDR-0168 D1).
                No control may share this row. The row count is not the point: the
                WIDTH is. Sharing the row cost the textarea roughly 15% of the
                composer, so the same text wrapped more often and the composer grew
                faster as the operator typed. */}
            <div className="relative">
              {/* Ghost completion mirror (CTR-0128): identical text metrics to the
                  textarea; the value copy is invisible so only the ghost suffix shows. */}
              {ghost && (
                <div
                  aria-hidden="true"
                  className={cn(
                    // IDENTICAL text metrics to the textarea, padding included: the mirror
                    // positions the ghost suffix by laying out an invisible copy of the
                    // value, so a padding difference offsets every completion.
                    'pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-3 pb-0 pt-2',
                    textSize,
                  )}>
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
                onKeyUp={handleKeyUp}
                onClick={() => void recompute()}
                onBlur={() => closeMenu()}
                placeholder={
                  liveActive
                    ? 'Type to add to the Live conversation'
                    : isTranscribing
                      ? 'Transcribing...'
                      : 'Type a message...'
                }
                rows={1}
                className={cn(
                  // No BOTTOM padding: row 2 sits directly beneath and supplies the
                  // gap, so the two rows read as one control rather than as a text
                  // box with a toolbar bolted under it.
                  'relative w-full resize-none bg-transparent px-3 pb-0 pt-2',
                  textSize,
                  temporary ? 'text-neutral-100 placeholder:text-neutral-400' : 'placeholder:text-muted-foreground',
                  'focus-visible:outline-hidden',
                  'disabled:cursor-not-allowed disabled:opacity-50',
                )}
                disabled={isLoading || isTranscribing}
              />
            </div>

            {/* ROW 2 -- the control row (CTR-0221, UDR-0168 D2).
                Bare icons only, left cluster then right cluster. Each entry states its
                name ONCE and that string is both its `title` and its `aria-label`
                (the CTR-0220 v2 rule, applied to the second row that needed it).

                NO overflow menu (D3): membership is bounded -- five entries since
                PRP-0188 added Live (UDR-0170 D9 amends the bound of four) -- and every
                entry is caller-gated, so the dynamic probe-gated growth that forced
                CTR-0220's menu cannot happen here. Its absence is a decision.

                No TOP padding: row 1's text area ends flush against this row, so the
                pair reads as one control. */}
            <div className="flex items-center gap-0.5 px-1 pb-1 pt-0">
              {liveActive && live ? (
                /* Live takes the whole row, like a recording (CTR-0228): its mic level in
                   teal, the state, the time left, mute and stop. Same bar ceiling as the
                   recording row, so the composer's height never changes (CTR-0092). */
                <LiveConversationBar
                  state={live.state}
                  levels={live.levels}
                  muted={live.muted}
                  working={live.working}
                  workingCount={live.workingCount}
                  remainingSeconds={live.remainingSeconds}
                  onStop={live.onStop}
                  onToggleMute={live.onToggleMute}
                  canSend={live.state === 'live' && Boolean(value.trim())}
                  onSend={() => void handleSend()}
                  barHeight={surface.narrow ? 24 : 18}
                  controlClassName={controlSize}
                />
              ) : isRecording ? (
                /* Recording takes the whole control row: the attach menu, the
                   run-target and Send have nothing to do until it ends, and a level
                   meter squeezed between them would be unreadable. The bar ceiling is
                   the tier's own control height, so starting a recording never changes
                   the composer's height -- a jump there would move the chat body, which
                   reserves a spacer measured from this block (CTR-0092). */
                <WaveformVisualizer
                  data={waveformData}
                  onStop={stopRecording}
                  barHeight={surface.narrow ? 24 : 18}
                  stopClassName={controlSize}
                />
              ) : (
                <>
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
                      <div className="flex shrink-0 items-center">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <button
                              type="button"
                              className={cn(
                                'inline-flex items-center justify-center rounded-md',
                                controlSize,
                                'text-muted-foreground hover:text-foreground',
                                'transition-colors',
                              )}
                              aria-label="Attach file"
                              title="Attach file">
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
                  {/* Run-target (CTR-0216 / CTR-0221, UDR-0168 D4). ICON ONLY -- the icon
                  carries the KIND, and the NAME is in `title` and `aria-label`, so one
                  hover or one screen-reader announcement recovers it. It opens the
                  picker; it never applies a choice (CTR-0217 owns that). */}
                  {runTarget && (
                    <button
                      type="button"
                      onClick={runTarget.onOpen}
                      aria-label={`Run target: ${runTarget.name}. Choose the agent.`}
                      title={runTarget.name}
                      className={cn(
                        'inline-flex shrink-0 items-center justify-center rounded-md',
                        controlSize,
                        'text-muted-foreground hover:text-foreground',
                        'transition-colors',
                      )}>
                      {runTarget.icon}
                    </button>
                  )}

                  {/* The clusters are split by a flexible gap, so the right cluster stays
                  at the trailing edge whatever the left one holds. */}
                  <div className="flex-1" />

                  {/* The occupancy warning's glyph sits HERE, next to Send, because that is
                  where the operator is already looking when it matters (D5). Critical
                  only: an amber ring is enough of an interruption at 80%, and a glyph
                  that is always present once you pass a threshold stops being a
                  warning. */}
                  {context?.level === 'critical' && (
                    <span
                      className="flex shrink-0 items-center px-1 text-red-500"
                      title={context.description}
                      aria-hidden="true">
                      <TriangleAlert className="h-4 w-4" />
                    </span>
                  )}

                  <div className="flex shrink-0 items-center gap-0.5">
                    {isLoading ? (
                      <Button
                        variant="destructive"
                        size="icon"
                        className={controlSize}
                        onClick={onStop}
                        aria-label="Stop generation"
                        title="Stop generation">
                        <Square className="h-4 w-4" />
                      </Button>
                    ) : (
                      <>
                        {voiceOffered && (
                          <button
                            type="button"
                            onClick={startRecording}
                            disabled={isTranscribing}
                            className={cn(
                              'inline-flex items-center justify-center rounded-md',
                              controlSize,
                              'text-muted-foreground hover:text-foreground',
                              'disabled:pointer-events-none disabled:opacity-50',
                              'transition-colors',
                            )}
                            aria-label={isTranscribing ? 'Transcribing' : 'Voice input'}
                            title={isTranscribing ? 'Transcribing' : 'Voice input'}>
                            {isTranscribing ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <Mic className="h-4 w-4" />
                            )}
                          </button>
                        )}
                        {/* Live conversation (CTR-0228), immediately right of Voice
                            Input. Its name is stated once, as title and aria-label. */}
                        {live && (
                          <button
                            type="button"
                            onClick={live.onStart}
                            disabled={isTranscribing}
                            className={cn(
                              'inline-flex items-center justify-center rounded-md',
                              controlSize,
                              'text-muted-foreground hover:text-cyan-600 dark:hover:text-cyan-400',
                              'disabled:pointer-events-none disabled:opacity-50',
                              'transition-colors',
                            )}
                            aria-label="Live conversation"
                            title="Live conversation">
                            <AudioLines className="h-4 w-4" />
                          </button>
                        )}
                        <Button
                          size="icon"
                          className={controlSize}
                          onClick={() => void handleSend()}
                          disabled={(!value.trim() && attachments.length === 0) || isTranscribing || isUploading}
                          aria-label="Send message"
                          title="Send message">
                          <SendHorizontal className="h-4 w-4" />
                        </Button>
                      </>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        }
        {voiceError && <p className="mt-1 text-xs text-destructive">{voiceError}</p>}
        {live?.error && <p className="mt-1 text-xs text-destructive">{live.error}</p>}
        {live?.notice && <p className="mt-1 text-xs text-muted-foreground">{live.notice}</p>}
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
