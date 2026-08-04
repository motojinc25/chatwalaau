/**
 * Paint editor canvas bounds, presets, and viewport constants
 * (CTR-0160 v3, PRP-0125 / UDR-0108 D2-D5).
 *
 * These are compile-time constants, NOT environment variables (UDR-0108 D2),
 * matching UDR-0078 D8 and CTR-0161's fixed scene-size cap.
 */

/** Smallest artboard edge. Unchanged from CTR-0160 v1. */
export const MIN_CANVAS = 64

/**
 * Largest artboard edge (UDR-0108 D2). Chosen to keep the 4K presets
 * expressible while staying clear of the Safari / iOS canvas area ceiling
 * (~16.7 MPx).
 */
export const MAX_CANVAS_EDGE = 4096

/**
 * Largest artboard AREA (UDR-0108 D2). The binding constraint is our own
 * upload cap -- MAX_FILE_SIZE_IMAGE is 20 MiB (backend upload/validation.py) --
 * not the browser, so the area budget is what actually keeps an attached PNG
 * inside the limit. Enforced when the size is ENTERED, never at export time.
 */
export const MAX_CANVAS_PX = 12_000_000

/**
 * Machine-independent default artboard (UDR-0108 D3).
 *
 * 1280x720 rather than 1920x1080: the point of the decision is that the size is
 * the same on every machine, not that it is large. A 1080p artboard is a
 * high-resolution DOCUMENT to paint on -- brush strokes and text have to be
 * sized up to read, and the exported PNG is heavier than a chat attachment
 * needs. 720p is the friendliest 16:9 starting point and is still one click
 * away from every larger preset.
 */
export const DEFAULT_CANVAS = { w: 1280, h: 720 } as const

/** Zoom bounds and multiplicative step (UDR-0108 D4). */
export const MIN_ZOOM = 0.05
export const MAX_ZOOM = 8
export const ZOOM_STEP = 1.2

/**
 * Endpoint snap radius in SCREEN pixels (UDR-0108 D15). Converted to scene
 * units by dividing by the live zoom, so the snap feels identical at every
 * zoom level -- a scene-unit radius would demand 60 scene px of precision at
 * zoom 0.2 and become grabby when zoomed in.
 */
export const SNAP_RADIUS_SCREEN_PX = 12

/**
 * Fraction of the artboard that must remain inside the viewport when panning
 * (UDR-0108 D5). Without this bound the page can be pushed off-screen with no
 * route back except Fit, which reads as a freeze.
 */
export const PAN_VISIBLE_FRACTION = 0.1

/** Margin left around the artboard by "Fit". */
export const FIT_MARGIN = 0.94

export interface CanvasPreset {
  label: string
  w: number
  h: number
}

export interface CanvasPresetGroup {
  ratio: string
  presets: CanvasPreset[]
}

/** Named ratio presets (UDR-0108 D3). Every entry satisfies D2's bounds. */
export const CANVAS_PRESET_GROUPS: CanvasPresetGroup[] = [
  {
    ratio: '16:9',
    presets: [
      { label: '1280 x 720', w: 1280, h: 720 },
      { label: '1920 x 1080', w: 1920, h: 1080 },
      { label: '2560 x 1440', w: 2560, h: 1440 },
      { label: '3840 x 2160', w: 3840, h: 2160 },
    ],
  },
  {
    ratio: '4:3',
    presets: [
      { label: '1024 x 768', w: 1024, h: 768 },
      { label: '1600 x 1200', w: 1600, h: 1200 },
      { label: '2048 x 1536', w: 2048, h: 1536 },
      { label: '3200 x 2400', w: 3200, h: 2400 },
    ],
  },
  {
    ratio: '1:1',
    presets: [
      { label: '1024 x 1024', w: 1024, h: 1024 },
      { label: '2048 x 2048', w: 2048, h: 2048 },
    ],
  },
]

/** Clamp a single edge into [MIN_CANVAS, MAX_CANVAS_EDGE]. */
export function clampEdge(value: number): number {
  if (!Number.isFinite(value)) return MIN_CANVAS
  return Math.min(MAX_CANVAS_EDGE, Math.max(MIN_CANVAS, Math.round(value)))
}

/**
 * True when w x h exceeds the area budget. An over-budget size is REJECTED at
 * input with a message naming the budget rather than silently clamped
 * (UDR-0108 D2) -- silently changing the requested aspect ratio is worse than
 * refusing the value.
 */
export function exceedsAreaBudget(w: number, h: number): boolean {
  return clampEdge(w) * clampEdge(h) > MAX_CANVAS_PX
}

/** Megapixels, for the inline over-budget message. */
export function megapixels(w: number, h: number): string {
  return ((w * h) / 1_000_000).toFixed(1)
}
