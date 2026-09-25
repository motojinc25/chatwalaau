import { useCallback, useRef, useState } from 'react'
import { imageSizeProblem, normalizeImageSize } from '@/lib/imageOptions'

/**
 * Canvas Image Editor state (CTR-0052 v2, PRP-0187, UDR-0169 D10/D14).
 *
 * Three stacked canvases at the SOURCE's pixel size:
 *  - the source (normalized to the size rule BEFORE anything is drawn, so the mask
 *    and the source match by construction -- D14);
 *  - the MASK layer: strokes are drawn fully OPAQUE as continuous round-capped
 *    segments, and the whole canvas is tinted by CSS `opacity` (default 0.4; colour and
 *    opacity are user-adjustable). Stamping
 *    semi-transparent circles (the old approach) compounded alpha wherever strokes
 *    overlapped, so a region painted twice looked darker although the mask sent was
 *    binary all along;
 *  - the ANNOTATION layer: vector shapes (rectangle / freehand) labelled A..Z,
 *    re-rendered from the list, drawn thick with a white halo and a solid label badge
 *    so they read clearly on any image (PRP-0187 Q4).
 *
 * Both layers are always shown; the active tab only decides which one the pointer
 * edits. One undo history (max 30) covers both.
 */

export type EditorTab = 'mask' | 'annotate'
export type MaskTool = 'brush' | 'eraser'
export type AnnotateTool = 'rect' | 'freehand'
export type BrushSize = 10 | 25 | 50

export interface Annotation {
  id: string
  label: string
  kind: 'rect' | 'freehand'
  /** rect: [x0, y0, x1, y1]; freehand: [x, y, x, y, ...] -- source pixel space. */
  points: number[]
  note: string
}

interface Snapshot {
  mask: ImageData
  annotations: Annotation[]
}

/**
 * Default mask display colour and layer opacity (applied by CSS on the canvas element).
 *
 * Both are DISPLAY-ONLY and user-adjustable (PRP-0187 amendment): an image whose colours
 * are close to the tint makes the painted area hard to see, so the editor lets the user
 * pick another colour and opacity. The mask SENT to the API does not depend on either --
 * exportMask only reads where the layer is painted (alpha > 10) and writes alpha 0 there.
 */
export const DEFAULT_MASK_COLOR = '#3b82f6'
export const DEFAULT_MASK_OPACITY = 0.4
/** Quick picks: blue, red, green, magenta, yellow, black, white. */
export const MASK_COLOR_PRESETS = ['#3b82f6', '#ef4444', '#22c55e', '#d946ef', '#facc15', '#000000', '#ffffff'] as const
export const MASK_OPACITY_RANGE = { min: 0.1, max: 0.9, step: 0.05 } as const

export function clampMaskOpacity(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_MASK_OPACITY
  return Math.min(MASK_OPACITY_RANGE.max, Math.max(MASK_OPACITY_RANGE.min, value))
}

export function isHexColor(value: unknown): value is string {
  return typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value)
}
const MAX_HISTORY = 30
const MAX_LABELS = 26
/** High-contrast colours, one per label in order (cycled). */
const ANNOTATION_COLORS = ['#ef4444', '#f59e0b', '#10b981', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16', '#f97316']

export function labelFor(index: number): string {
  return String.fromCharCode(65 + index)
}

function annotationColor(index: number): string {
  return ANNOTATION_COLORS[index % ANNOTATION_COLORS.length]
}

/** Stroke width / badge size scaled to the image so marks stay legible at any size. */
function markMetrics(width: number, height: number) {
  const base = Math.min(width, height)
  return {
    stroke: Math.max(4, Math.round(base / 180)),
    halo: Math.max(3, Math.round(base / 360)),
    badge: Math.max(28, Math.round(base / 22)),
  }
}

/** Draw every annotation (outline + halo + label badge) onto a 2D context. */
export function drawAnnotations(
  ctx: CanvasRenderingContext2D,
  annotations: Annotation[],
  width: number,
  height: number,
) {
  const m = markMetrics(width, height)
  ctx.save()
  ctx.lineJoin = 'round'
  ctx.lineCap = 'round'
  annotations.forEach((a, index) => {
    const color = annotationColor(index)
    const path = new Path2D()
    let anchorX = 0
    let anchorY = 0
    if (a.kind === 'rect' && a.points.length >= 4) {
      const [x0, y0, x1, y1] = a.points
      const x = Math.min(x0, x1)
      const y = Math.min(y0, y1)
      path.rect(x, y, Math.abs(x1 - x0), Math.abs(y1 - y0))
      anchorX = x
      anchorY = y
    } else if (a.points.length >= 2) {
      path.moveTo(a.points[0], a.points[1])
      for (let i = 2; i + 1 < a.points.length; i += 2) path.lineTo(a.points[i], a.points[i + 1])
      let minX = Number.POSITIVE_INFINITY
      let minY = Number.POSITIVE_INFINITY
      for (let i = 0; i + 1 < a.points.length; i += 2) {
        minX = Math.min(minX, a.points[i])
        minY = Math.min(minY, a.points[i + 1])
      }
      anchorX = minX
      anchorY = minY
    }
    // White halo under the colour so the outline reads on dark AND light content.
    ctx.strokeStyle = 'white'
    ctx.lineWidth = m.stroke + m.halo * 2
    ctx.stroke(path)
    ctx.strokeStyle = color
    ctx.lineWidth = m.stroke
    ctx.stroke(path)
    // Label badge at the shape's top-left, kept inside the image.
    const bx = Math.min(Math.max(anchorX - m.badge / 2, 0), width - m.badge)
    const by = Math.min(Math.max(anchorY - m.badge / 2, 0), height - m.badge)
    ctx.fillStyle = color
    ctx.strokeStyle = 'white'
    ctx.lineWidth = m.halo
    ctx.beginPath()
    ctx.roundRect(bx, by, m.badge, m.badge, m.badge / 4)
    ctx.fill()
    ctx.stroke()
    ctx.fillStyle = 'white'
    ctx.font = `bold ${Math.round(m.badge * 0.62)}px system-ui, sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(a.label, bx + m.badge / 2, by + m.badge / 2 + 1)
  })
  ctx.restore()
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('Failed to export image'))), 'image/png')
  })
}

function cloneAnnotations(list: Annotation[]): Annotation[] {
  return list.map((a) => ({ ...a, points: [...a.points] }))
}

/** Relabel in creation order after a deletion; notes stay with their shapes. */
function relabel(list: Annotation[]): Annotation[] {
  return list.map((a, i) => ({ ...a, label: labelFor(i) }))
}

export interface LoadedSource {
  /** Pixel size the editor works at (normalized). */
  width: number
  height: number
  /** True when the source had to be resampled (it must then be uploaded as a new file). */
  normalized: boolean
}

export function useImageEditor() {
  const imageCanvasRef = useRef<HTMLCanvasElement>(null)
  const maskCanvasRef = useRef<HTMLCanvasElement>(null)
  const annotationCanvasRef = useRef<HTMLCanvasElement>(null)

  const [tab, setTab] = useState<EditorTab>('mask')
  const [maskTool, setMaskTool] = useState<MaskTool>('brush')
  const [annotateTool, setAnnotateTool] = useState<AnnotateTool>('rect')
  const [brushSize, setBrushSize] = useState<BrushSize>(25)
  const [annotations, setAnnotationsState] = useState<Annotation[]>([])
  const [canvasReady, setCanvasReady] = useState(false)
  const [source, setSource] = useState<LoadedSource | null>(null)
  const [maskEmpty, setMaskEmpty] = useState(true)
  const [maskColor, setMaskColorState] = useState<string>(DEFAULT_MASK_COLOR)
  const [maskOpacity, setMaskOpacityState] = useState<number>(DEFAULT_MASK_OPACITY)
  /** Bumped on every committed change; the dialog saves the draft when it moves. */
  const [revision, setRevision] = useState(0)

  const maskColorRef = useRef<string>(DEFAULT_MASK_COLOR)
  const annotationsRef = useRef<Annotation[]>([])
  const historyRef = useRef<Snapshot[]>([])
  const historyIndexRef = useRef(-1)
  const drawingRef = useRef<{ lastX: number; lastY: number } | null>(null)
  const draftShapeRef = useRef<Annotation | null>(null)

  const setAnnotations = useCallback((list: Annotation[]) => {
    annotationsRef.current = list
    setAnnotationsState(list)
  }, [])

  const renderAnnotations = useCallback((extra?: Annotation | null) => {
    const canvas = annotationCanvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    const list = extra ? [...annotationsRef.current, extra] : annotationsRef.current
    drawAnnotations(ctx, list, canvas.width, canvas.height)
  }, [])

  /** Repaint every painted mask pixel in `color`, keeping its coverage (alpha) as is. */
  const recolorMask = useCallback((color: string) => {
    const canvas = maskCanvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    ctx.save()
    ctx.globalCompositeOperation = 'source-in'
    ctx.fillStyle = color
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.restore()
  }, [])

  /** Display colour of the mask. Not part of the undo history: it changes no edit area. */
  const setMaskColor = useCallback(
    (color: string) => {
      if (!isHexColor(color)) return
      maskColorRef.current = color
      setMaskColorState(color)
      recolorMask(color)
      setRevision((r) => r + 1)
    },
    [recolorMask],
  )

  const setMaskOpacity = useCallback((value: number) => {
    setMaskOpacityState(clampMaskOpacity(value))
    setRevision((r) => r + 1)
  }, [])

  const refreshMaskEmpty = useCallback(() => {
    const canvas = maskCanvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data
    let empty = true
    for (let i = 3; i < data.length; i += 4) {
      if (data[i] > 10) {
        empty = false
        break
      }
    }
    setMaskEmpty(empty)
  }, [])

  const pushHistory = useCallback(() => {
    const canvas = maskCanvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    const snapshot: Snapshot = {
      mask: ctx.getImageData(0, 0, canvas.width, canvas.height),
      annotations: cloneAnnotations(annotationsRef.current),
    }
    const idx = historyIndexRef.current + 1
    historyRef.current = historyRef.current.slice(0, idx)
    historyRef.current.push(snapshot)
    if (historyRef.current.length > MAX_HISTORY) historyRef.current.shift()
    historyIndexRef.current = historyRef.current.length - 1
  }, [])

  const commit = useCallback(() => {
    pushHistory()
    refreshMaskEmpty()
    setRevision((r) => r + 1)
  }, [pushHistory, refreshMaskEmpty])

  const restoreSnapshot = useCallback(
    (snapshot: Snapshot) => {
      const ctx = maskCanvasRef.current?.getContext('2d')
      ctx?.putImageData(snapshot.mask, 0, 0)
      recolorMask(maskColorRef.current)
      setAnnotations(cloneAnnotations(snapshot.annotations))
      renderAnnotations()
      refreshMaskEmpty()
      setRevision((r) => r + 1)
    },
    [recolorMask, renderAnnotations, refreshMaskEmpty, setAnnotations],
  )

  /**
   * Load the source (normalizing it when it breaks the size rule) and, optionally, a
   * restored draft's mask bitmap and annotations.
   */
  const load = useCallback(
    (
      imageUrl: string,
      restore?: { maskPng?: string | null; annotations?: Annotation[] },
      forceNormalize = false,
    ): Promise<LoadedSource> =>
      new Promise((resolve, reject) => {
        const img = new Image()
        img.crossOrigin = 'anonymous'
        img.onerror = () => reject(new Error('Failed to load image'))
        img.onload = () => {
          const imageCanvas = imageCanvasRef.current
          const maskCanvas = maskCanvasRef.current
          const annotationCanvas = annotationCanvasRef.current
          if (!imageCanvas || !maskCanvas || !annotationCanvas) {
            reject(new Error('Canvas not ready'))
            return
          }
          const natural = { width: img.naturalWidth, height: img.naturalHeight }
          const needsNormalize = forceNormalize || imageSizeProblem(natural.width, natural.height) !== null
          const size = needsNormalize ? normalizeImageSize(natural.width, natural.height) : natural
          for (const c of [imageCanvas, maskCanvas, annotationCanvas]) {
            c.width = size.width
            c.height = size.height
          }
          const imgCtx = imageCanvas.getContext('2d')
          imgCtx?.drawImage(img, 0, 0, size.width, size.height)
          const maskCtx = maskCanvas.getContext('2d')
          maskCtx?.clearRect(0, 0, size.width, size.height)
          setAnnotations(relabel(cloneAnnotations(restore?.annotations ?? [])))
          renderAnnotations()

          const finish = () => {
            historyRef.current = []
            historyIndexRef.current = -1
            pushHistory()
            refreshMaskEmpty()
            const loaded = { width: size.width, height: size.height, normalized: needsNormalize }
            setSource(loaded)
            setCanvasReady(true)
            resolve(loaded)
          }
          if (restore?.maskPng && maskCtx) {
            const maskImg = new Image()
            maskImg.onload = () => {
              maskCtx.drawImage(maskImg, 0, 0, size.width, size.height)
              recolorMask(maskColorRef.current)
              finish()
            }
            maskImg.onerror = finish
            maskImg.src = restore.maskPng
          } else {
            finish()
          }
        }
        img.src = imageUrl
      }),
    [pushHistory, recolorMask, refreshMaskEmpty, renderAnnotations, setAnnotations],
  )

  const pointFromEvent = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = e.currentTarget
    const rect = canvas.getBoundingClientRect()
    return {
      x: ((e.clientX - rect.left) * canvas.width) / rect.width,
      y: ((e.clientY - rect.top) * canvas.height) / rect.height,
    }
  }, [])

  const strokeMask = useCallback(
    (x0: number, y0: number, x1: number, y1: number) => {
      const ctx = maskCanvasRef.current?.getContext('2d')
      if (!ctx) return
      ctx.save()
      ctx.globalCompositeOperation = maskTool === 'eraser' ? 'destination-out' : 'source-over'
      ctx.strokeStyle = maskColorRef.current
      ctx.fillStyle = maskColorRef.current
      ctx.lineWidth = brushSize * 2
      ctx.lineCap = 'round'
      ctx.lineJoin = 'round'
      ctx.beginPath()
      if (x0 === x1 && y0 === y1) {
        ctx.arc(x0, y0, brushSize, 0, Math.PI * 2)
        ctx.fill()
      } else {
        ctx.moveTo(x0, y0)
        ctx.lineTo(x1, y1)
        ctx.stroke()
      }
      ctx.restore()
    },
    [brushSize, maskTool],
  )

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (!canvasReady) return
      const { x, y } = pointFromEvent(e)
      e.currentTarget.setPointerCapture(e.pointerId)
      if (tab === 'mask') {
        drawingRef.current = { lastX: x, lastY: y }
        strokeMask(x, y, x, y)
        return
      }
      if (annotationsRef.current.length >= MAX_LABELS) return
      draftShapeRef.current = {
        id: crypto.randomUUID().slice(0, 8),
        label: labelFor(annotationsRef.current.length),
        kind: annotateTool,
        points: annotateTool === 'rect' ? [x, y, x, y] : [x, y],
        note: '',
      }
      renderAnnotations(draftShapeRef.current)
    },
    [annotateTool, canvasReady, pointFromEvent, renderAnnotations, strokeMask, tab],
  )

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      const { x, y } = pointFromEvent(e)
      if (tab === 'mask') {
        const last = drawingRef.current
        if (!last) return
        strokeMask(last.lastX, last.lastY, x, y)
        drawingRef.current = { lastX: x, lastY: y }
        return
      }
      const shape = draftShapeRef.current
      if (!shape) return
      if (shape.kind === 'rect') shape.points = [shape.points[0], shape.points[1], x, y]
      else shape.points.push(x, y)
      renderAnnotations(shape)
    },
    [pointFromEvent, renderAnnotations, strokeMask, tab],
  )

  const handlePointerUp = useCallback(() => {
    if (drawingRef.current) {
      drawingRef.current = null
      commit()
      return
    }
    const shape = draftShapeRef.current
    if (!shape) return
    draftShapeRef.current = null
    // Ignore a click without a drag: a zero-size mark says nothing.
    const tooSmall =
      shape.kind === 'rect'
        ? Math.abs(shape.points[2] - shape.points[0]) < 4 || Math.abs(shape.points[3] - shape.points[1]) < 4
        : shape.points.length < 6
    if (tooSmall) {
      renderAnnotations()
      return
    }
    setAnnotations([...annotationsRef.current, shape])
    renderAnnotations()
    commit()
  }, [commit, renderAnnotations, setAnnotations])

  const setNote = useCallback(
    (id: string, note: string) => {
      setAnnotations(annotationsRef.current.map((a) => (a.id === id ? { ...a, note } : a)))
      setRevision((r) => r + 1)
    },
    [setAnnotations],
  )

  const removeAnnotation = useCallback(
    (id: string) => {
      setAnnotations(relabel(annotationsRef.current.filter((a) => a.id !== id)))
      renderAnnotations()
      commit()
    },
    [commit, renderAnnotations, setAnnotations],
  )

  const undo = useCallback(() => {
    if (historyIndexRef.current <= 0) return
    historyIndexRef.current--
    restoreSnapshot(historyRef.current[historyIndexRef.current])
  }, [restoreSnapshot])

  const redo = useCallback(() => {
    if (historyIndexRef.current >= historyRef.current.length - 1) return
    historyIndexRef.current++
    restoreSnapshot(historyRef.current[historyIndexRef.current])
  }, [restoreSnapshot])

  /** Clear the ACTIVE tab's layer. */
  const clearTab = useCallback(() => {
    if (tab === 'mask') {
      const canvas = maskCanvasRef.current
      canvas?.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height)
    } else {
      setAnnotations([])
      renderAnnotations()
    }
    commit()
  }, [commit, renderAnnotations, setAnnotations, tab])

  /** The source as the editor sees it (normalized size), for upload when resampled. */
  const exportSource = useCallback(async (): Promise<Blob> => {
    const canvas = imageCanvasRef.current
    if (!canvas) throw new Error('Canvas not ready')
    return canvasToBlob(canvas)
  }, [])

  /**
   * The mask PNG: opaque everywhere, alpha 0 where painted (the Images API format:
   * fully transparent pixels mark the area that may change).
   */
  const exportMask = useCallback(async (): Promise<Blob> => {
    const maskCanvas = maskCanvasRef.current
    if (!maskCanvas) throw new Error('Canvas not ready')
    const out = document.createElement('canvas')
    out.width = maskCanvas.width
    out.height = maskCanvas.height
    const ctx = out.getContext('2d')
    const src = maskCanvas.getContext('2d')
    if (!ctx || !src) throw new Error('Cannot export mask')
    const painted = src.getImageData(0, 0, out.width, out.height)
    const mask = ctx.createImageData(out.width, out.height)
    for (let i = 0; i < painted.data.length; i += 4) {
      const edit = painted.data[i + 3] > 10
      mask.data[i] = 0
      mask.data[i + 1] = 0
      mask.data[i + 2] = 0
      mask.data[i + 3] = edit ? 0 : 255
    }
    ctx.putImageData(mask, 0, 0)
    return canvasToBlob(out)
  }, [])

  /** The source with the annotations drawn on it, at the source's size. */
  const exportAnnotated = useCallback(async (): Promise<Blob> => {
    const imageCanvas = imageCanvasRef.current
    if (!imageCanvas) throw new Error('Canvas not ready')
    const out = document.createElement('canvas')
    out.width = imageCanvas.width
    out.height = imageCanvas.height
    const ctx = out.getContext('2d')
    if (!ctx) throw new Error('Cannot export annotated image')
    ctx.drawImage(imageCanvas, 0, 0)
    drawAnnotations(ctx, annotationsRef.current, out.width, out.height)
    return canvasToBlob(out)
  }, [])

  /** The display preview: the source with the mask tint (A4) -- what the bubble shows first. */
  const exportPreview = useCallback(async (): Promise<Blob> => {
    const imageCanvas = imageCanvasRef.current
    const maskCanvas = maskCanvasRef.current
    if (!imageCanvas || !maskCanvas) throw new Error('Canvas not ready')
    const out = document.createElement('canvas')
    out.width = imageCanvas.width
    out.height = imageCanvas.height
    const ctx = out.getContext('2d')
    if (!ctx) throw new Error('Cannot export preview')
    ctx.drawImage(imageCanvas, 0, 0)
    ctx.globalAlpha = maskOpacity
    ctx.drawImage(maskCanvas, 0, 0)
    return canvasToBlob(out)
  }, [maskOpacity])

  /** The mask layer as a data URL for the draft (null when empty). */
  const maskDataUrl = useCallback((): string | null => {
    const canvas = maskCanvasRef.current
    if (!canvas || maskEmpty) return null
    return canvas.toDataURL('image/png')
  }, [maskEmpty])

  return {
    imageCanvasRef,
    maskCanvasRef,
    annotationCanvasRef,
    tab,
    setTab,
    maskTool,
    setMaskTool,
    annotateTool,
    setAnnotateTool,
    brushSize,
    setBrushSize,
    annotations,
    setNote,
    removeAnnotation,
    canvasReady,
    source,
    maskEmpty,
    revision,
    load,
    handlePointerDown,
    handlePointerMove,
    handlePointerUp,
    undo,
    redo,
    clearTab,
    exportSource,
    exportMask,
    exportAnnotated,
    exportPreview,
    maskDataUrl,
    maskColor,
    setMaskColor,
    maskOpacity,
    setMaskOpacity,
    maxLabels: MAX_LABELS,
  }
}
