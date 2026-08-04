/**
 * Paint artboard / viewport model (CTR-0160 v3, PRP-0125 / UDR-0108 D1-D8).
 *
 * The `<canvas>` element is sized to the STAGE, never to the artboard (D1).
 * The artboard is a logical rectangle in scene coordinates, painted as a white
 * page; pan and zoom are `viewportTransform` operations and the stage is not a
 * scroll container.
 *
 * That separation is what removes the reported pointer drift structurally:
 * Fabric converts pointer events through a CACHED element offset, and the old
 * model made a large artboard the only case that produced scrollbars -- so it
 * was the only case where the cached offset went stale, by exactly the scroll
 * amount.
 */
import type { Canvas, TMat2D } from 'fabric'
import { FIT_MARGIN, MAX_ZOOM, MIN_ZOOM, PAN_VISIBLE_FRACTION } from './constants'

export interface Artboard {
  w: number
  h: number
}

/**
 * Suppresses the page / scrim decoration while the export path renders, so the
 * exported PNG depends on nothing but the artboard and the objects (D8).
 */
let exporting = false

export function isExporting(): boolean {
  return exporting
}

export function clampZoom(z: number): number {
  if (!Number.isFinite(z)) return 1
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z))
}

/**
 * Keep at least PAN_VISIBLE_FRACTION of the artboard inside the viewport (D5).
 * Losing the page entirely, with no route back except Fit, reads as a freeze.
 */
export function clampPan(canvas: Canvas, artboard: Artboard): void {
  const vpt = canvas.viewportTransform
  if (!vpt) return
  const z = vpt[0]
  const W = canvas.getWidth()
  const H = canvas.getHeight()
  const aw = artboard.w * z
  const ah = artboard.h * z
  const next = [...vpt] as TMat2D
  next[4] = Math.min(W - aw * PAN_VISIBLE_FRACTION, Math.max(-aw * (1 - PAN_VISIBLE_FRACTION), vpt[4]))
  next[5] = Math.min(H - ah * PAN_VISIBLE_FRACTION, Math.max(-ah * (1 - PAN_VISIBLE_FRACTION), vpt[5]))
  if (next[4] !== vpt[4] || next[5] !== vpt[5]) canvas.setViewportTransform(next)
}

/** Zoom anchored at a point in CANVAS (viewport) coordinates (D4). */
export function zoomAtPoint(canvas: Canvas, zoom: number, cx: number, cy: number, artboard: Artboard): number {
  const next = clampZoom(zoom)
  canvas.zoomToPoint({ x: cx, y: cy } as never, next)
  clampPan(canvas, artboard)
  return next
}

/** Zoom anchored at the viewport centre (toolbar buttons). */
export function zoomAtCenter(canvas: Canvas, zoom: number, artboard: Artboard): number {
  return zoomAtPoint(canvas, zoom, canvas.getWidth() / 2, canvas.getHeight() / 2, artboard)
}

export function panBy(canvas: Canvas, dx: number, dy: number, artboard: Artboard): void {
  const vpt = canvas.viewportTransform
  if (!vpt) return
  const next = [...vpt] as TMat2D
  next[4] += dx
  next[5] += dy
  canvas.setViewportTransform(next)
  clampPan(canvas, artboard)
}

/** Fit the whole artboard into the viewport and centre it. */
export function fitArtboard(canvas: Canvas, artboard: Artboard): number {
  const W = canvas.getWidth()
  const H = canvas.getHeight()
  if (W <= 0 || H <= 0) return canvas.getZoom()
  const z = clampZoom(Math.min(W / artboard.w, H / artboard.h) * FIT_MARGIN)
  canvas.setViewportTransform([z, 0, 0, z, (W - artboard.w * z) / 2, (H - artboard.h * z) / 2])
  return z
}

/** Zoom so the active selection fills most of the viewport. */
export function zoomToSelection(canvas: Canvas, artboard: Artboard): number | null {
  const active = canvas.getActiveObject()
  if (!active) return null
  const r = active.getBoundingRect()
  if (r.width <= 0 || r.height <= 0) return null
  const W = canvas.getWidth()
  const H = canvas.getHeight()
  const z = clampZoom(Math.min(W / r.width, H / r.height) * 0.6)
  canvas.setViewportTransform([z, 0, 0, z, W / 2 - (r.left + r.width / 2) * z, H / 2 - (r.top + r.height / 2) * z])
  clampPan(canvas, artboard)
  return z
}

/**
 * The white page, drawn in `before:render` -- which fires AFTER Fabric clears
 * the context and BEFORE it renders the background and the objects, so the page
 * lands underneath the artwork.
 *
 * Drawn in SCREEN space (the viewport transform has no rotation or skew here),
 * so the shadow and border keep a constant on-screen weight at every zoom.
 */
export function drawArtboardPage(ctx: CanvasRenderingContext2D, canvas: Canvas, artboard: Artboard): void {
  if (exporting) return
  const vpt = canvas.viewportTransform
  if (!vpt) return
  const z = vpt[0]
  const x = vpt[4]
  const y = vpt[5]
  ctx.save()
  ctx.shadowColor = 'rgba(0, 0, 0, 0.22)'
  ctx.shadowBlur = 16
  ctx.shadowOffsetY = 4
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(x, y, artboard.w * z, artboard.h * z)
  ctx.restore()
}

/**
 * Dim everything outside the page and outline it, drawn in `after:render`.
 *
 * Content drawn beyond the artboard reads as OUTSIDE the page while staying
 * visible and grabbable -- a hard clip would hide a stray object with no way to
 * find it. The authoritative clip is the export, which crops to the artboard
 * rect (D8).
 */
export function drawArtboardScrim(ctx: CanvasRenderingContext2D, canvas: Canvas, artboard: Artboard): void {
  if (exporting) return
  const vpt = canvas.viewportTransform
  if (!vpt) return
  const z = vpt[0]
  const x = vpt[4]
  const y = vpt[5]
  const w = artboard.w * z
  const h = artboard.h * z
  const W = canvas.getWidth()
  const H = canvas.getHeight()
  ctx.save()
  ctx.fillStyle = 'rgba(212, 212, 216, 0.72)'
  // Four bands around the page rect.
  if (y > 0) ctx.fillRect(0, 0, W, Math.min(y, H))
  if (y + h < H) ctx.fillRect(0, Math.max(0, y + h), W, H - Math.max(0, y + h))
  const bandTop = Math.max(0, y)
  const bandH = Math.min(H, y + h) - bandTop
  if (bandH > 0) {
    if (x > 0) ctx.fillRect(0, bandTop, Math.min(x, W), bandH)
    if (x + w < W) ctx.fillRect(Math.max(0, x + w), bandTop, W - Math.max(0, x + w), bandH)
  }
  ctx.strokeStyle = 'rgba(113, 113, 122, 0.9)'
  ctx.lineWidth = 1
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1)
  ctx.restore()
}

/**
 * Render the artboard to a PNG data URL, independent of the current viewport.
 *
 * INVARIANT (D8): the exported PNG is identical regardless of the current zoom
 * level and pan position. The transform is neutralised, the element is resized
 * to the artboard, the page white is applied as the canvas background, and
 * every step is restored in `finally` so a throw cannot leave the editor in the
 * export state.
 */
export function exportArtboardDataUrl(canvas: Canvas, artboard: Artboard): string {
  const vpt = [...(canvas.viewportTransform ?? [1, 0, 0, 1, 0, 0])] as TMat2D
  const w = canvas.getWidth()
  const h = canvas.getHeight()
  const bg = canvas.backgroundColor
  exporting = true
  try {
    canvas.setViewportTransform([1, 0, 0, 1, 0, 0])
    canvas.setDimensions({ width: artboard.w, height: artboard.h })
    canvas.backgroundColor = '#ffffff'
    canvas.renderAll()
    return canvas.toDataURL({ format: 'png', multiplier: 1 })
  } finally {
    exporting = false
    canvas.backgroundColor = bg
    canvas.setDimensions({ width: w, height: h })
    canvas.setViewportTransform(vpt)
    canvas.requestRenderAll()
  }
}
