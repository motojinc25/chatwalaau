import { Check, ImagePlus } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
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
 * Compression is offered only for jpeg. When image generation is not
 * configured or in DEMO_MODE the selection is simply ignored on the backend
 * (no-op, UDR-0063 D6).
 *
 * v0.117.6: the values are GATED by the configured image model. These options are
 * not uniformly supported, and the control used to offer every value
 * unconditionally, so a user could pick one that failed the turn. `transparent` and
 * `webp` are withdrawn outright (gpt-image-2 rejects both); for anything else, the
 * backend reports what the deployment has been observed to reject
 * (GET /api/model `image_output`, learned from the provider's own 400s, never
 * guessed); an unsupported value is disabled here with the reason shown, and a value
 * already selected when it turns out to be unsupported is cleared.
 */

export type ImageOptions = Record<string, string>

interface ImageOutputOptionsProps {
  threadId: string
  onChange: (opts: ImageOptions) => void
}

const STORAGE_PREFIX = 'chatwalaau-image-'

// v0.117.6: 2K / 4K sizes were missing entirely, so they could not be chosen.
// Mirrors app/image_gen/capabilities.py OPTION_VALUES -- the backend validates
// against the same surface, so the two must not drift.
const SIZE_CHOICES = ['auto', '1024x1024', '1536x1024', '1024x1536', '2048x2048', '2048x1152', '3840x2160', '2160x3840']
const QUALITY_CHOICES = ['auto', 'low', 'medium', 'high']
const FORMAT_CHOICES = ['png', 'jpeg']
const BACKGROUND_CHOICES = ['auto', 'opaque']
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

/** Image output capabilities advertised by GET /api/model (CTR-0069, v0.117.6). */
interface ImageOutputCapability {
  deployment: string
  values: Record<string, string[]>
  /** option key -> values this deployment has been observed to reject. */
  unsupported: Record<string, string[]>
}

export function ImageOutputOptions({ threadId, onChange }: ImageOutputOptionsProps) {
  const [opts, setOpts] = useState<ImageOptions>({})
  const [open, setOpen] = useState(false)
  const [capability, setCapability] = useState<ImageOutputCapability | null>(null)

  // Restore the per-session selection on mount / thread change.
  useEffect(() => {
    setOpts(load(threadId))
  }, [threadId])

  // Which values the configured image model has been observed to reject. Re-read
  // when the panel is opened so a restriction learned during this session (the
  // backend records the first rejection) is reflected without a page reload.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    fetch('/api/model')
      .then((res) => res.json())
      .then((data: { image_output?: ImageOutputCapability | null }) => {
        if (!cancelled) setCapability(data?.image_output ?? null)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [open])

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
        // Compression only applies to the lossy format (jpeg).
        if (key === 'format' && value !== 'jpeg') delete next.compression
        localStorage.setItem(storageKey(threadId), JSON.stringify(next))
        return next
      })
    },
    [threadId],
  )

  const unsupported = useMemo(() => capability?.unsupported ?? {}, [capability])
  const isUnsupported = useCallback(
    (field: string, value: string) => (unsupported[field] ?? []).includes(value),
    [unsupported],
  )

  // A value the model turns out to reject must not stay selected: it would fail the
  // next turn exactly as before. Clearing it falls back to that model's own default.
  useEffect(() => {
    for (const field of FIELDS) {
      const value = opts[field]
      if (value && (unsupported[field] ?? []).includes(value)) {
        setField(field, '')
        return
      }
    }
  }, [unsupported, opts, setField])

  const activeCount = FIELDS.filter((k) => opts[k]).length
  const compressionEligible = opts.format === 'jpeg'
  const anyUnsupported = Object.values(unsupported).some((values) => values.length > 0)
  // v0.117.6: the compression box accepted anything the browser would let through
  // (a non-integer, out of range), which reached the API as an opaque 400. The
  // backend validates the same rule; this just makes it visible before sending.
  const compressionInvalid = (() => {
    const raw = opts.compression
    if (!raw) return false
    const value = Number(raw)
    return !Number.isInteger(value) || value < 0 || value > 100
  })()

  const selectClass = 'h-6 rounded-md border bg-background px-1 text-[11px] outline-none focus:ring-1 focus:ring-ring'

  /** Options for one field, disabling what the deployed model cannot produce. */
  const renderChoices = (field: string, choices: string[]) =>
    choices.map((c) => (
      <option key={c} value={c} disabled={isUnsupported(field, c)}>
        {c}
        {isUnsupported(field, c) ? ' — not supported' : ''}
      </option>
    ))

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
                  {renderChoices('size', SIZE_CHOICES)}
                </select>
              </label>
              <label className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted-foreground">Quality</span>
                <select
                  value={opts.quality ?? ''}
                  onChange={(e) => setField('quality', e.target.value)}
                  className={selectClass}>
                  <option value="">Default</option>
                  {renderChoices('quality', QUALITY_CHOICES)}
                </select>
              </label>
              <label className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted-foreground">Format</span>
                <select
                  value={opts.format ?? ''}
                  onChange={(e) => setField('format', e.target.value)}
                  className={selectClass}>
                  <option value="">Default</option>
                  {renderChoices('format', FORMAT_CHOICES)}
                </select>
              </label>
              {compressionEligible && (
                <label className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-muted-foreground">Compression</span>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={opts.compression ?? ''}
                    placeholder="0-100"
                    onChange={(e) => setField('compression', e.target.value)}
                    aria-invalid={compressionInvalid || undefined}
                    className={cn(
                      'h-6 w-[72px] rounded-md border bg-background px-1 text-[11px] outline-none focus:ring-1 focus:ring-ring',
                      compressionInvalid && 'border-destructive focus:ring-destructive',
                    )}
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
                  {renderChoices('background', BACKGROUND_CHOICES)}
                </select>
              </label>
            </div>
            <p className="mt-1.5 text-[10px] text-muted-foreground">
              Defaults apply when image generation runs. The model may override a field when needed.
            </p>
            {compressionInvalid && (
              <p className="mt-1 text-[10px] leading-snug text-destructive">
                Compression must be a whole number between 0 and 100.
              </p>
            )}
            {anyUnsupported && (
              <p className="mt-1 text-[10px] leading-snug text-amber-700 dark:text-amber-500">
                Values marked "not supported" were rejected by the configured image model
                {capability?.deployment ? ` (${capability.deployment})` : ''} and are disabled.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
