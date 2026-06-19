import { Check, ImagePlus } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { cn } from '@/lib/utils'

/**
 * Image Output Options control (CTR-0120, PRP-0085, FEAT-0044, UDR-0063 D6).
 *
 * A compact per-session control that lets the user choose how generated/edited
 * images are produced -- Size / Quality / Format / Compression / Background. The
 * selection is reported up to ChatPanel, which sends it as AG-UI
 * state.image_options; the backend (CTR-0049) applies it as the generate_image /
 * edit_image DEFAULT (an explicit LLM tool argument still wins). Only non-default
 * fields are sent, so an untouched control changes nothing.
 *
 * Per-session selection persists in localStorage (the CTR-0071 / CTR-0118 pattern).
 * Compression is offered only for jpeg / webp. When image generation is not
 * configured or in DEMO_MODE the selection is simply ignored on the backend
 * (no-op, UDR-0063 D6).
 */

export type ImageOptions = Record<string, string>

interface ImageOutputOptionsProps {
  threadId: string
  onChange: (opts: ImageOptions) => void
}

const STORAGE_PREFIX = 'chatwalaau-image-'

const SIZE_CHOICES = ['auto', '1024x1024', '1024x1536', '1536x1024']
const QUALITY_CHOICES = ['auto', 'low', 'medium', 'high']
const FORMAT_CHOICES = ['png', 'jpeg', 'webp']
const BACKGROUND_CHOICES = ['auto', 'transparent', 'opaque']
const FIELDS = ['size', 'quality', 'format', 'compression', 'background'] as const

function storageKey(threadId: string): string {
  return `${STORAGE_PREFIX}${threadId}`
}

function load(threadId: string): ImageOptions {
  try {
    const raw = localStorage.getItem(storageKey(threadId))
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

export function ImageOutputOptions({ threadId, onChange }: ImageOutputOptionsProps) {
  const [opts, setOpts] = useState<ImageOptions>({})
  const [open, setOpen] = useState(false)

  // Restore the per-session selection on mount / thread change.
  useEffect(() => {
    setOpts(load(threadId))
  }, [threadId])

  // Report only non-empty (chosen) fields to ChatPanel.
  useEffect(() => {
    const chosen: ImageOptions = {}
    for (const k of FIELDS) {
      const v = opts[k]
      if (v) chosen[k] = v
    }
    onChange(chosen)
  }, [opts, onChange])

  const setField = useCallback(
    (key: string, value: string) => {
      setOpts((prev) => {
        const next = { ...prev }
        if (value) next[key] = value
        else delete next[key]
        // Compression only applies to jpeg / webp.
        if (key === 'format' && value !== 'jpeg' && value !== 'webp') delete next.compression
        localStorage.setItem(storageKey(threadId), JSON.stringify(next))
        return next
      })
    },
    [threadId],
  )

  const activeCount = FIELDS.filter((k) => opts[k]).length
  const compressionEligible = opts.format === 'jpeg' || opts.format === 'webp'

  const selectClass = 'h-6 rounded-md border bg-background px-1 text-[11px] outline-none focus:ring-1 focus:ring-ring'

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        title="Image output options (size, quality, format, compression, background)"
        className={cn(
          'flex items-center gap-0.5 rounded-md border px-1.5 h-6 text-xs transition-colors',
          activeCount > 0
            ? 'border-primary/40 bg-primary/10 text-primary'
            : 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
        )}>
        <ImagePlus className="h-3 w-3 shrink-0" />
        <span className="hidden sm:inline">Image{activeCount > 0 ? ` (${activeCount})` : ''}</span>
      </button>

      {open && (
        <>
          <button
            type="button"
            tabIndex={-1}
            className="fixed inset-0 z-40 cursor-default border-none bg-transparent"
            onClick={() => setOpen(false)}
            aria-label="Close image options"
          />
          <div className="absolute bottom-full left-0 z-50 mb-1 w-[240px] rounded-md border bg-popover p-2 shadow-md">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium">Image output</span>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[11px] text-primary hover:bg-accent">
                <Check className="h-3 w-3" /> Done
              </button>
            </div>
            <div className="space-y-1.5">
              <label className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted-foreground">Size</span>
                <select
                  value={opts.size ?? ''}
                  onChange={(e) => setField('size', e.target.value)}
                  className={selectClass}>
                  <option value="">Default</option>
                  {SIZE_CHOICES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted-foreground">Quality</span>
                <select
                  value={opts.quality ?? ''}
                  onChange={(e) => setField('quality', e.target.value)}
                  className={selectClass}>
                  <option value="">Default</option>
                  {QUALITY_CHOICES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted-foreground">Format</span>
                <select
                  value={opts.format ?? ''}
                  onChange={(e) => setField('format', e.target.value)}
                  className={selectClass}>
                  <option value="">Default</option>
                  {FORMAT_CHOICES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
              {compressionEligible && (
                <label className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-muted-foreground">Compression</span>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={opts.compression ?? ''}
                    placeholder="0-100"
                    onChange={(e) => setField('compression', e.target.value)}
                    className="h-6 w-[72px] rounded-md border bg-background px-1 text-[11px] outline-none focus:ring-1 focus:ring-ring"
                  />
                </label>
              )}
              <label className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted-foreground">Background</span>
                <select
                  value={opts.background ?? ''}
                  onChange={(e) => setField('background', e.target.value)}
                  className={selectClass}>
                  <option value="">Default</option>
                  {BACKGROUND_CHOICES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="mt-1.5 text-[10px] text-muted-foreground">
              Defaults apply when image generation runs. The model may override a field when needed.
            </p>
          </div>
        </>
      )}
    </div>
  )
}
