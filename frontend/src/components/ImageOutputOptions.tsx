import { useCallback, useEffect, useMemo } from 'react'
import { cn } from '@/lib/utils'

/**
 * Built-in agent image output options (CTR-0120, FEAT-0044, PRP-0185, UDR-0167 D11).
 *
 * Size / Quality / Format / Compression / Background for the images the Built-in
 * ChatWalaʻau Core agent generates. Rendered inside the Core agent card, next to the
 * model / effort / structured-output controls it now belongs with.
 *
 * It used to be a PER-SESSION control in the chat composer: the selection lived in
 * each browser's localStorage, keyed by thread, and travelled as AG-UI
 * state.image_options. That made a piece of the Built-in agent's configuration
 * invisible to everyone but the browser that set it, and invisible to the operator
 * entirely. PRP-0184 moved model / effort / structured output off the same toolbar for
 * the same reason; this is the control it did not reach.
 *
 * The selection is now PERSISTED in the Application Settings store (CTR-0198) and
 * applies SERVER-WIDE, which the Core agent card states (the UDR-0158 rule, as
 * UDR-0166 D9 applied it). It does NOT own its own Apply button: the parent writes
 * every Built-in setting in ONE CTR-0199 PATCH, so a change here is applied with the
 * model and effort rather than through a second, racing write.
 *
 * What did NOT change is the per-value deployment gating. GET /api/model
 * `image_output` reports which values the configured image model has been observed to
 * reject (learned from the provider's own 400s, never guessed); a rejected value is
 * disabled here with the reason shown, and a value already selected when it turns out
 * to be unsupported is cleared. Dropping that in the move would have handed the
 * operator back the ability to pin a value that fails every turn.
 */

export type ImageOptions = Record<string, string>

/** Mirrors app/image_gen/capabilities.py OPTION_VALUES; the backend validates the same surface. */
const SIZE_CHOICES = ['auto', '1024x1024', '1536x1024', '1024x1536', '2048x2048', '2048x1152', '3840x2160', '2160x3840']
const QUALITY_CHOICES = ['auto', 'low', 'medium', 'high']
const FORMAT_CHOICES = ['png', 'jpeg']
const BACKGROUND_CHOICES = ['auto', 'opaque']
export const IMAGE_OPTION_FIELDS = ['size', 'quality', 'format', 'compression', 'background'] as const

/** Image output capabilities advertised by GET /api/model (CTR-0069, v0.117.6). */
export interface ImageOutputCapability {
  deployment: string
  values: Record<string, string[]>
  /** option key -> values this deployment has been observed to reject. */
  unsupported: Record<string, string[]>
}

interface ImageOutputOptionsProps {
  /** Current values, keyed by option name ("" = API default). */
  value: ImageOptions
  /** Report a single field change; the parent owns the draft and the Apply. */
  onFieldChange: (key: string, value: string) => void
  /** Deployment capability report; null when no image offering is configured. */
  capability: ImageOutputCapability | null
  disabled?: boolean
  className?: string
}

export function ImageOutputOptions({
  value,
  onFieldChange,
  capability,
  disabled = false,
  className,
}: ImageOutputOptionsProps) {
  const unsupported = useMemo(() => capability?.unsupported ?? {}, [capability])
  const isUnsupported = useCallback(
    (field: string, choice: string) => (unsupported[field] ?? []).includes(choice),
    [unsupported],
  )

  // A value the model turns out to reject must not stay selected: it would fail the
  // next turn exactly as before. Clearing it falls back to that model's own default.
  useEffect(() => {
    for (const field of IMAGE_OPTION_FIELDS) {
      const current = value[field]
      if (current && (unsupported[field] ?? []).includes(current)) {
        onFieldChange(field, '')
        return
      }
    }
  }, [unsupported, value, onFieldChange])

  const compressionEligible = value.format === 'jpeg'
  const anyUnsupported = Object.values(unsupported).some((values) => values.length > 0)
  // The compression box used to accept anything the browser would let through (a
  // non-integer, out of range), which reached the API as an opaque 400. The backend
  // validates the same rule; this makes it visible before sending.
  const compressionInvalid = (() => {
    const raw = value.compression
    if (!raw) return false
    const parsed = Number(raw)
    return !Number.isInteger(parsed) || parsed < 0 || parsed > 100
  })()

  const selectClass =
    'h-7 rounded-md border bg-background px-2 text-xs focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50'

  const renderChoices = (field: string, choices: string[]) =>
    choices.map((choice) => (
      <option key={choice} value={choice} disabled={isUnsupported(field, choice)}>
        {choice}
        {isUnsupported(field, choice) ? ' — not supported' : ''}
      </option>
    ))

  return (
    <div className={cn('space-y-2', className)}>
      <span className="text-[11px] font-medium text-muted-foreground">Image output</span>
      <div className="flex flex-wrap items-center gap-2">
        <select
          className={cn(selectClass, 'w-36')}
          disabled={disabled}
          value={value.size ?? ''}
          aria-label="Built-in agent image size"
          onChange={(e) => onFieldChange('size', e.target.value)}>
          <option value="">size (default)</option>
          {renderChoices('size', SIZE_CHOICES)}
        </select>
        <select
          className={cn(selectClass, 'w-32')}
          disabled={disabled}
          value={value.quality ?? ''}
          aria-label="Built-in agent image quality"
          onChange={(e) => onFieldChange('quality', e.target.value)}>
          <option value="">quality (default)</option>
          {renderChoices('quality', QUALITY_CHOICES)}
        </select>
        <select
          className={cn(selectClass, 'w-28')}
          disabled={disabled}
          value={value.format ?? ''}
          aria-label="Built-in agent image format"
          onChange={(e) => onFieldChange('format', e.target.value)}>
          <option value="">format (default)</option>
          {renderChoices('format', FORMAT_CHOICES)}
        </select>
        {compressionEligible && (
          <input
            type="number"
            min={0}
            max={100}
            step={1}
            disabled={disabled}
            value={value.compression ?? ''}
            placeholder="compression 0-100"
            aria-label="Built-in agent image compression"
            aria-invalid={compressionInvalid || undefined}
            onChange={(e) => onFieldChange('compression', e.target.value)}
            className={cn(selectClass, 'w-40', compressionInvalid && 'border-destructive focus:ring-destructive')}
          />
        )}
        <select
          className={cn(selectClass, 'w-36')}
          disabled={disabled}
          value={value.background ?? ''}
          aria-label="Built-in agent image background"
          onChange={(e) => onFieldChange('background', e.target.value)}>
          <option value="">background (default)</option>
          {renderChoices('background', BACKGROUND_CHOICES)}
        </select>
      </div>
      {compressionInvalid && (
        <p className="text-[11px] text-red-600 dark:text-red-400">
          Compression must be a whole number between 0 and 100.
        </p>
      )}
      {anyUnsupported && (
        <p className="text-[11px] leading-snug text-amber-700 dark:text-amber-500">
          Values marked "not supported" were rejected by the configured image model
          {capability?.deployment ? ` (${capability.deployment})` : ''} and are disabled.
        </p>
      )}
    </div>
  )
}
