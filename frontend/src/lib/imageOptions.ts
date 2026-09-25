import { useEffect, useState } from 'react'

/**
 * Image output option surface (PRP-0187, UDR-0169 D1/D2/D3/D5).
 *
 * Mirrors app/image_gen/capabilities.py (OPTION_VALUES / SIZE_PRESETS /
 * PRODUCT_DEFAULTS / SIZE_RULE); an invariant test asserts they stay identical. The
 * catalog image card and the image editor render these; the backend validates the
 * same surface.
 *
 * There is no `auto` and no "API default": every call sends an explicit value, so an
 * unset field is shown as `Default (<value>)` with the value that will actually be
 * sent. The output format is fixed to png and is not an option.
 */
export const IMAGE_SIZE_PRESETS = ['2048x1152', '1920x1440', '1024x1024'] as const
export const IMAGE_SIZE_PRESET_LABELS: Record<string, string> = {
  '2048x1152': '2048x1152 (16:9)',
  '1920x1440': '1920x1440 (4:3)',
  '1024x1024': '1024x1024 (1:1)',
}
export const IMAGE_QUALITIES = ['low', 'medium', 'high', 'xhigh', 'max'] as const
export const IMAGE_BACKGROUNDS = ['opaque', 'transparent'] as const
export const IMAGE_OUTPUT_FORMAT = 'png'
export const IMAGE_PRODUCT_DEFAULTS: Record<'size' | 'quality' | 'background', string> = {
  size: '2048x1152',
  quality: 'xhigh',
  background: 'opaque',
}

/** The documented size rule. */
export const IMAGE_SIZE_RULE = {
  multiple: 16,
  maxEdge: 3840,
  maxRatio: 3,
  minPixels: 655_360,
  maxPixels: 8_294_400,
} as const
/** Above this pixel count a size is "experimental" (2560x1440). */
const STABLE_MAX_PIXELS = 2560 * 1440

/** Why a size breaks the rule, or null when it is valid. */
export function imageSizeProblem(width: number, height: number): string | null {
  const r = IMAGE_SIZE_RULE
  if (width % r.multiple || height % r.multiple) return `both edges must be multiples of ${r.multiple} px`
  if (Math.max(width, height) > r.maxEdge) return `neither edge may exceed ${r.maxEdge} px`
  if (Math.max(width, height) > r.maxRatio * Math.min(width, height))
    return `the aspect ratio must be between 1:${r.maxRatio} and ${r.maxRatio}:1`
  const pixels = width * height
  if (pixels < r.minPixels || pixels > r.maxPixels) return 'the total pixel count is out of range'
  return null
}

/**
 * The nearest rule-valid size with about the same shape, capped at the stable
 * (non-experimental) pixel budget. Mirrors capabilities.normalize_size(stable=True):
 * the editor resamples a non-conforming source to this BEFORE the mask is drawn, so
 * the mask and the source match by construction (UDR-0169 D14).
 */
export function normalizeImageSize(width: number, height: number): { width: number; height: number } {
  const r = IMAGE_SIZE_RULE
  if (width <= 0 || height <= 0) return { width: 2048, height: 1152 }
  const ratio = Math.min(Math.max(width / height, 1 / r.maxRatio), r.maxRatio)
  const area = Math.min(Math.max(width * height, r.minPixels), STABLE_MAX_PIXELS)
  let fw = Math.sqrt(area * ratio)
  let fh = fw / ratio
  const scale = Math.min(1, r.maxEdge / Math.max(fw, fh))
  fw *= scale
  fh *= scale
  let w = Math.max(r.multiple, Math.round(fw / r.multiple) * r.multiple)
  let h = Math.max(r.multiple, Math.round(fh / r.multiple) * r.multiple)
  for (let i = 0; i < 2000; i++) {
    if (imageSizeProblem(w, h) === null && w * h <= STABLE_MAX_PIXELS) break
    if (w > r.maxRatio * h) h += r.multiple
    else if (h > r.maxRatio * w) w += r.multiple
    else if (w * h > STABLE_MAX_PIXELS || Math.max(w, h) > r.maxEdge) {
      if (w >= h) w -= r.multiple
      else h -= r.multiple
    } else if (w / h < ratio) w += r.multiple
    else h += r.multiple
  }
  return { width: w, height: h }
}

/** Image output capabilities advertised by GET /api/model `image_output` (CTR-0069). */
export interface ImageOutputCapability {
  deployment: string
  values: Record<string, string[]>
  /** option key -> values this deployment has been observed to reject. */
  unsupported: Record<string, string[]>
  defaults?: Record<string, string>
  /** What an omitted option resolves to now: catalog value, else the product default. */
  effective_defaults?: Record<string, string>
}

let capabilityRequest: Promise<ImageOutputCapability | null> | null = null

/** Fetch GET /api/model `image_output` once per page load (null without an image offering). */
export function fetchImageOutputCapability(refresh = false): Promise<ImageOutputCapability | null> {
  if (!capabilityRequest || refresh) {
    capabilityRequest = fetch('/api/model')
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => (body?.image_output ?? null) as ImageOutputCapability | null)
      .catch(() => null)
  }
  return capabilityRequest
}

/** The image output capability report; `refreshKey` re-reads it (e.g. on dialog open). */
export function useImageOutputCapability(refreshKey?: unknown): ImageOutputCapability | null {
  const [capability, setCapability] = useState<ImageOutputCapability | null>(null)
  useEffect(() => {
    let alive = true
    fetchImageOutputCapability(refreshKey !== undefined).then((c) => {
      if (alive) setCapability(c)
    })
    return () => {
      alive = false
    }
  }, [refreshKey])
  return capability
}

/** True when this deployment has been observed to reject `value` for `option`. */
export function isUnsupportedImageValue(
  capability: ImageOutputCapability | null,
  option: string,
  value: string,
): boolean {
  return (capability?.unsupported?.[option] ?? []).includes(value)
}
