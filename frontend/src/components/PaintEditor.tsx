import {
  Canvas,
  Ellipse,
  FabricImage,
  type FabricObject,
  Group,
  loadSVGFromString,
  PencilBrush,
  Point,
  Rect,
  Textbox,
  util,
} from 'fabric'
import {
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Circle as CircleIcon,
  Download,
  Eye,
  EyeOff,
  FolderOpen,
  Hand,
  HardDrive,
  ImagePlus,
  Loader2,
  Lock,
  Maximize,
  Minus,
  MousePointer2,
  Paintbrush,
  Redo2,
  RotateCcw,
  Square,
  Trash2,
  Type,
  Undo2,
  Unlock,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import type { CSSProperties } from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type ArrowHead,
  assignFreshPid,
  Connector,
  type ConnectorRouting,
  commitPendingBind,
  ensureScenePids,
  getPid,
  isConnector,
  placeConnectorEndpoint,
  refreshConnectorBindings,
  remapDuplicatedBindings,
  syncConnectorControls,
} from '@/components/paint/connector'
import {
  clampEdge,
  DEFAULT_CANVAS,
  exceedsAreaBudget,
  MAX_CANVAS_EDGE,
  MAX_CANVAS_PX,
  MIN_CANVAS,
  ZOOM_STEP,
} from '@/components/paint/constants'
import { PaintCanvasSizeControl } from '@/components/paint/PaintCanvasSizeControl'
import { PaintConnectorOptions } from '@/components/paint/PaintConnectorOptions'
import { PaintObjectContextMenu } from '@/components/paint/PaintObjectContextMenu'
import {
  type Artboard,
  clampPan,
  drawArtboardPage,
  drawArtboardScrim,
  exportArtboardDataUrl,
  fitArtboard,
  panBy,
  zoomAtCenter,
  zoomAtPoint,
  zoomToSelection,
} from '@/components/paint/viewport'
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
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { WorkspaceImagePicker } from '@/components/WorkspaceImagePicker'
import { useFileExplorerAvailable } from '@/hooks/useFileExplorerAvailable'
import { cn } from '@/lib/utils'

type Tool = 'select' | 'hand' | 'draw' | 'rect' | 'ellipse' | 'line' | 'arrow' | 'text'

const CONNECTOR_TOOLS: Tool[] = ['line', 'arrow']

interface PaintEditorProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Fabric scene JSON to load on open (re-edit). Undefined = fresh canvas. */
  initialScene?: unknown
  /** Called when the user attaches: rendered PNG + the editable Fabric scene. */
  onAttach: (blob: Blob, scene: unknown) => void
}

const PALETTE = ['#111827', '#ef4444', '#f59e0b', '#10b981', '#3b82f6', '#8b5cf6', '#ec4899', '#ffffff']
const SVG_SIGNATURE = /^\s*(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*<svg[\s>]/i

function dataUrlToBlob(dataUrl: string): Promise<Blob> {
  return fetch(dataUrl).then((r) => r.blob())
}

/** A drag payload is acceptable only if it carries an image file or text (SVG). */
function dragHasAcceptable(dt: DataTransfer): boolean {
  for (const item of dt.items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) return true
    if (item.kind === 'string' && (item.type === 'text/plain' || item.type === 'text/html')) return true
  }
  // Some browsers expose only `types` during dragover.
  return dt.types.includes('Files') || dt.types.includes('text/plain')
}

/** True while the keyboard focus is somewhere that owns its own keystrokes (D7). */
function focusOwnsKeys(): boolean {
  const ae = document.activeElement as HTMLElement | null
  return !!ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)
}

/**
 * Full-screen object-based paint editor (CTR-0160, PRP-0099).
 *
 * Built on Fabric.js v6 for the object model, per-object transform handles,
 * scene serialization (re-editability), and native SVG import (UDR-0078 D1).
 * Reuses the CTR-0137 File Explorer full-screen Dialog recipe and an
 * unsaved-content close/reset guard (UDR-0078 D4).
 *
 * v3 (PRP-0125 / UDR-0108): the canvas ELEMENT tracks the stage and the
 * artboard is a logical rect in scene space, so pan/zoom are viewport
 * operations and the stage is not a scroll container. That separation is what
 * removes the pointer drift on a large canvas structurally -- Fabric converts
 * pointer events through a cached element offset, and only a scrolling stage
 * could make it stale. Adds bounded artboard sizes with ratio presets, a Hand
 * tool plus modifier panning, an object clipboard with a context menu, and
 * Arrow / Line connectors that bind to shapes (CTR-0191).
 */
export function PaintEditor({ open, onOpenChange, initialScene, onAttach }: PaintEditorProps) {
  const stageRef = useRef<HTMLDivElement | null>(null)
  /** The drawable pane: the canvas's own parent, and the box it must fill. */
  const paneRef = useRef<HTMLDivElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const fabricRef = useRef<Canvas | null>(null)

  // Image import source (CTR-0160 v2, PRP-0102 / UDR-0078 D10): the "from
  // workspace" option is offered only when the coding workspace / File Explorer
  // is available (CTR-0136); otherwise only local device upload is possible.
  const workspaceAvailable = useFileExplorerAvailable()
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false)
  // Animated indicator while an imported image is fetched / decoded onto the canvas.
  const [importing, setImporting] = useState(false)

  // Tool/style state mirrored into refs so Fabric event handlers (registered
  // once) always read the live values instead of a stale closure.
  const [tool, setTool] = useState<Tool>('select')
  const [color, setColor] = useState('#111827')
  const [width, setWidth] = useState(4)
  const toolRef = useRef(tool)
  const colorRef = useRef(color)
  const widthRef = useRef(width)
  toolRef.current = tool
  colorRef.current = color
  widthRef.current = width

  // Connector defaults for newly drawn arrows / lines (CTR-0191, D11 / D17).
  const [connectorStyle, setConnectorStyle] = useState<{
    routing: ConnectorRouting
    headStart: ArrowHead
    headEnd: ArrowHead
  }>({ routing: 'straight', headStart: 'none', headEnd: 'arrow' })
  const connectorStyleRef = useRef(connectorStyle)
  connectorStyleRef.current = connectorStyle

  // Artboard = the exported image, a logical rect in scene space (D1). The
  // canvas ELEMENT is sized to the stage, not to this. In fit mode the artboard
  // tracks the stage; every other size is explicit and machine-independent (D3).
  const [canvasSize, setCanvasSize] = useState<Artboard>({ ...DEFAULT_CANVAS })
  const canvasSizeRef = useRef(canvasSize)
  canvasSizeRef.current = canvasSize
  const [fitMode, setFitMode] = useState(false)
  const fitModeRef = useRef(fitMode)
  fitModeRef.current = fitMode

  const [zoom, setZoom] = useState(1)
  const [dirty, setDirty] = useState(false)
  const [layersVersion, setLayersVersion] = useState(0)
  const [confirm, setConfirm] = useState<null | 'close' | 'reset'>(null)
  const [isDraggingOver, setIsDraggingOver] = useState(false)
  const dragDepthRef = useRef(0)
  // The canvas-selected object, mirrored so the Layers panel can highlight it.
  const [activeObj, setActiveObj] = useState<FabricObject | null>(null)

  // Object clipboard (D6): INTERNAL and module-scoped, never the system
  // clipboard -- the system clipboard is already bound to image / SVG import
  // (UDR-0078 D6) and Fabric JSON must not leak into other applications.
  const clipboardRef = useRef<Record<string, unknown>[] | null>(null)
  const [canPaste, setCanPaste] = useState(false)
  // Scene point of the last right-click, so "Paste" from the canvas menu lands
  // under the pointer instead of at a fixed offset.
  const menuPointRef = useRef<{ x: number; y: number } | null>(null)

  // Text editing is done through an HTML <textarea> overlay rather than Fabric's
  // built-in in-canvas editor: Fabric's hidden textarea fights the Radix Dialog
  // focus trap, so keystrokes never land. A plain React-rendered textarea inside
  // the dialog focuses and types reliably; its value is written back to the
  // Textbox on commit.
  const [textEdit, setTextEdit] = useState<{ obj: Textbox; isNew: boolean } | null>(null)
  const textEditRef = useRef(textEdit)
  textEditRef.current = textEdit
  const textAreaRef = useRef<HTMLTextAreaElement | null>(null)
  const startTextEditRef = useRef<(obj: Textbox, isNew: boolean) => void>(() => {})

  const restoringRef = useRef(false)
  const historyRef = useRef<string[]>([])
  const histIdxRef = useRef(-1)
  // Object currently being drawn by a drag gesture, exposed to the render hook
  // so a connector's snap preview can be highlighted while it is drawn.
  const drawingRef = useRef<FabricObject | null>(null)
  // Space held = temporary Hand, in any tool (D5).
  const spaceDownRef = useRef(false)

  const bumpLayers = useCallback(() => setLayersVersion((v) => v + 1), [])

  const snapshot = useCallback(() => {
    const c = fabricRef.current
    if (!c || restoringRef.current) return
    const json = JSON.stringify(c.toJSON())
    historyRef.current = historyRef.current.slice(0, histIdxRef.current + 1)
    historyRef.current.push(json)
    if (historyRef.current.length > 50) historyRef.current.shift()
    histIdxRef.current = historyRef.current.length - 1
    setDirty(true)
    bumpLayers()
  }, [bumpLayers])

  const restore = useCallback(
    async (json: string) => {
      const c = fabricRef.current
      if (!c) return
      restoringRef.current = true
      await c.loadFromJSON(json)
      // A restored scene is transparent behind the artboard page; the export
      // path re-applies the page white (D8).
      c.backgroundColor = ''
      ensureScenePids(c)
      refreshConnectorBindings(c)
      c.requestRenderAll()
      restoringRef.current = false
      bumpLayers()
    },
    [bumpLayers],
  )

  const undo = useCallback(() => {
    if (histIdxRef.current <= 0) return
    histIdxRef.current -= 1
    void restore(historyRef.current[histIdxRef.current])
  }, [restore])

  const redo = useCallback(() => {
    if (histIdxRef.current >= historyRef.current.length - 1) return
    histIdxRef.current += 1
    void restore(historyRef.current[histIdxRef.current])
  }, [restore])

  // ---- Artboard + viewport ----
  /**
   * A fit is owed but could not be performed yet, because the stage had no
   * usable measurement at the time.
   *
   * The editor mounts inside a Radix Dialog, so the very first measurement can
   * arrive before the dialog body has been laid out. Fitting against that
   * measurement is what made "Fit the artboard" open at the wrong scale: a
   * zero-sized stage produced a 1x1 viewport, `fitArtboard` clamped the zoom to
   * MIN_ZOOM, and nothing ever re-fitted -- which is why toggling "Fit to
   * window" appeared to repair it (that path resets the transform outright).
   */
  const needsFitRef = useRef(true)

  /**
   * The user has deliberately changed the view (zoomed or panned).
   *
   * While this is false the view is AUTO-FITTED: any change to the pane -- window
   * resize, the toolbar wrapping to a second row, the dialog opening -- re-frames
   * the artboard immediately, which is what makes the page look right at every
   * window size instead of keeping a ratio chosen for the previous one. Once the
   * user zooms or pans, re-fitting under them would be hostile, so their view is
   * kept until they ask for Fit again (which re-arms this).
   */
  const userAdjustedRef = useRef(false)

  /**
   * Publish the zoom READOUT from the canvas itself.
   *
   * The percentage next to the zoom buttons used to be a second copy of the
   * zoom, set by whoever changed the viewport. Any path that forgot to update it
   * -- or ran before the viewport settled -- left the toolbar claiming a zoom the
   * canvas was not at, which is worse than no readout because it is believable.
   * Reading `getZoom()` after every viewport change makes drift impossible.
   */
  const publishZoom = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    setZoom(c.getZoom())
  }, [])

  /**
   * Measure the drawable pane.
   *
   * React attaches refs CHILD-FIRST, so while the `<canvas>` ref callback runs
   * `stageRef.current` is still null -- the stage is its ancestor. That is why
   * the canvas used to be built at a fallback size and then depended entirely on
   * the ResizeObserver to be corrected: until that landed, `lower-canvas` and
   * `upper-canvas` sat at the fallback size inside a full-width container, i.e. a
   * small drawing surface in a large empty pane. The pane element is the canvas's
   * own parent, so it is available and laid out at that moment; measuring it
   * removes the dependency on ordering entirely.
   */
  const measurePane = useCallback((): { w: number; h: number } | null => {
    const el = paneRef.current ?? stageRef.current
    if (!el) return null
    const w = el.clientWidth
    const h = el.clientHeight
    if (w < 2 || h < 2) return null
    return { w, h }
  }, [])

  /**
   * Frame the whole artboard, or record that a fit is still owed.
   *
   * Never fits against an unmeasured pane: doing so is silently wrong rather
   * than visibly broken, which is exactly how the original defect survived.
   */
  const fitIfMeasured = useCallback(() => {
    const c = fabricRef.current
    if (!c || !measurePane()) {
      needsFitRef.current = true
      return
    }
    needsFitRef.current = false
    userAdjustedRef.current = false
    fitArtboard(c, canvasSizeRef.current)
    publishZoom()
    c.requestRenderAll()
  }, [publishZoom, measurePane])

  /**
   * Size the canvas ELEMENT to the stage and refresh Fabric's cached element
   * offset (D1). `calcOffset` is the whole reason the old model drifted: Fabric
   * refreshes it on window resize only, so a stage that changed size or
   * position left every pointer coordinate wrong.
   *
   * The element ALWAYS fills the stage, so the drawable area is the whole pane
   * at whatever size the window happens to be; the artboard is drawn inside it
   * (D1). A stage that measures zero is skipped rather than clamped to 1x1 --
   * a 1x1 canvas is a real element that then has to be corrected, and until it
   * is, everything computed from it (the fit above all) is wrong.
   */
  const syncStageSize = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    const measured = measurePane()
    if (!measured) {
      needsFitRef.current = true
      return
    }
    const { w, h } = measured
    // Fabric sizes `.canvas-container`, `lower-canvas` and `upper-canvas` from
    // this call, so it is what actually makes the drawing surface fill the pane.
    if (c.getWidth() !== w || c.getHeight() !== h) c.setDimensions({ width: w, height: h })
    c.calcOffset()
    if (fitModeRef.current) {
      const size = { w: clampEdge(w), h: clampEdge(h) }
      setCanvasSize((prev) => (prev.w === size.w && prev.h === size.h ? prev : size))
      canvasSizeRef.current = size
      c.setViewportTransform([1, 0, 0, 1, 0, 0])
      needsFitRef.current = false
    } else if (needsFitRef.current) {
      // The first real measurement. Fit now rather than leaving the artboard at
      // whatever scale the unmeasured stage produced.
      needsFitRef.current = false
      fitArtboard(c, canvasSizeRef.current)
    } else if (!userAdjustedRef.current) {
      // Still auto-fitted: track the pane so the artboard stays framed at
      // whatever size the window now is.
      fitArtboard(c, canvasSizeRef.current)
    } else {
      // The user chose this zoom -- keep it, and only make sure the page is
      // still reachable.
      clampPan(c, canvasSizeRef.current)
    }
    publishZoom()
    c.requestRenderAll()
  }, [publishZoom, measurePane])

  /**
   * Apply an explicit artboard size. Edges are clamped; an over-budget AREA is
   * refused by the size control before it reaches here (D2), and refused again
   * defensively so no caller can slip one through.
   */
  const applyCanvasSize = useCallback(
    (w: number, h: number) => {
      const W = clampEdge(w)
      const H = clampEdge(h)
      if (exceedsAreaBudget(W, H)) return
      setFitMode(false)
      fitModeRef.current = false
      setCanvasSize((prev) => (prev.w === W && prev.h === H ? prev : { w: W, h: H }))
      canvasSizeRef.current = { w: W, h: H }
      fitIfMeasured()
    },
    [fitIfMeasured],
  )

  /** Fit to window: the artboard tracks the stage at 1:1 (D3, still available). */
  const applyFitMode = useCallback(() => {
    setFitMode(true)
    fitModeRef.current = true
    userAdjustedRef.current = false
    syncStageSize()
  }, [syncStageSize])

  const setZoomLevel = useCallback(
    (z: number) => {
      const c = fabricRef.current
      if (!c) return
      userAdjustedRef.current = true
      zoomAtCenter(c, z, canvasSizeRef.current)
      publishZoom()
      c.requestRenderAll()
    },
    [publishZoom],
  )

  const doFitView = useCallback(() => {
    fitIfMeasured()
  }, [fitIfMeasured])

  const doZoomToSelection = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    userAdjustedRef.current = true
    zoomToSelection(c, canvasSizeRef.current)
    publishZoom()
    c.requestRenderAll()
  }, [publishZoom])

  /**
   * Give a loaded scene an artboard big enough to actually show it.
   *
   * Object coordinates in a Fabric scene are ABSOLUTE, but `toJSON()` does not
   * serialize the canvas dimensions. A paint drawn on a large screen and
   * re-opened on a smaller one got a smaller artboard, leaving most of the
   * artwork outside it: fully intact in the file, entirely invisible on screen.
   *
   * Two cases:
   *  - the scene records its size (saved by this version) -> restore it exactly,
   *    INCLUDING sizes outside the preset list and ratios outside 16:9 / 4:3 /
   *    1:1 (UDR-0108 D10);
   *  - it does not (saved earlier) -> grow the artboard to the artwork's bounding
   *    box, which recovers drawings already saved without a size.
   *
   * Either way fit mode is turned OFF, or a stage resize would immediately
   * reshape the artboard and re-hide the drawing.
   */
  const restoreArtboardSize = useCallback(
    (c: Canvas, scene: unknown) => {
      const saved = scene as { width?: unknown; height?: unknown } | null
      const savedW = typeof saved?.width === 'number' ? saved.width : 0
      const savedH = typeof saved?.height === 'number' ? saved.height : 0
      const commit = (w: number, h: number) => {
        const size = { w: Math.max(MIN_CANVAS, Math.round(w)), h: Math.max(MIN_CANVAS, Math.round(h)) }
        setFitMode(false)
        fitModeRef.current = false
        setCanvasSize(size)
        canvasSizeRef.current = size
        fitIfMeasured()
      }
      if (savedW > 0 && savedH > 0) {
        commit(savedW, savedH)
        return
      }

      const objects = c.getObjects()
      if (objects.length === 0) return
      let right = 0
      let bottom = 0
      for (const o of objects) {
        const r = o.getBoundingRect()
        right = Math.max(right, r.left + r.width)
        bottom = Math.max(bottom, r.top + r.height)
      }
      // A small margin so a stroke sitting exactly on the edge is not clipped.
      right += 24
      bottom += 24
      const cur = canvasSizeRef.current
      if (right > cur.w || bottom > cur.h) commit(Math.max(right, cur.w), Math.max(bottom, cur.h))
    },
    [fitIfMeasured],
  )

  // ---- Canvas lifecycle ----
  // Initialize Fabric when the <canvas> node mounts (Radix renders the dialog
  // body only while open), and dispose when it unmounts.
  //
  // This callback runs MORE THAN ONCE per open. React StrictMode deliberately
  // double-invokes ref callbacks in development: attach(node) -> detach(null) ->
  // attach(node) on the same element. So the canvas built by the first attach is
  // disposed while any async work it started is still in flight -- see `seed`
  // below, which is why every step there is guarded on this instance still being
  // the live one (`fabricRef.current === c`).
  const attachCanvas = useCallback(
    (node: HTMLCanvasElement | null) => {
      if (!node) {
        fabricRef.current?.dispose()
        fabricRef.current = null
        return
      }
      // `node.parentElement` is the pane. It is laid out already, whereas
      // `stageRef.current` and `paneRef.current` are still null here (refs attach
      // child-first), so measuring the parent is what stops the canvas from being
      // born at a fallback size and rendering small until something else corrects
      // it. Seeding paneRef from it also lets `measurePane()` work during seed(),
      // so the FIRST fit happens now rather than waiting for the observer -- which
      // is what makes the editor open on a centred, white, correctly-scaled
      // artboard instead of a grey pane that snaps into place a frame later.
      const pane = node.parentElement as HTMLDivElement | null
      if (pane && !paneRef.current) paneRef.current = pane
      const c = new Canvas(node, {
        width: Math.max(1, pane?.clientWidth ?? 0),
        height: Math.max(1, pane?.clientHeight ?? 0),
        // Transparent: the neutral backdrop is the stage's own background and
        // the white page is drawn per-frame in `before:render` (D1). The export
        // path re-applies the page white so the PNG is unchanged (D8).
        backgroundColor: '',
        preserveObjectStacking: true,
        fireRightClick: true,
        stopContextMenu: false,
      })
      fabricRef.current = c

      // The artboard page and the out-of-page scrim. `before:render` fires after
      // Fabric clears the context and before it renders the objects, so the page
      // lands underneath the artwork.
      c.on('before:render', ({ ctx }) => drawArtboardPage(ctx, c, canvasSizeRef.current))
      c.on('after:render', ({ ctx }) => {
        drawArtboardScrim(ctx, c, canvasSizeRef.current)
        const drawn = drawingRef.current
        const active = c.getActiveObject()
        const conn = isConnector(drawn) ? drawn : isConnector(active) ? active : null
        const hl = conn?.snapHighlight
        if (!hl) return
        const p = new Point(hl.x, hl.y).transform(c.viewportTransform)
        ctx.save()
        ctx.beginPath()
        ctx.arc(p.x, p.y, 7, 0, Math.PI * 2)
        ctx.strokeStyle = '#22c55e'
        ctx.lineWidth = 2
        ctx.stroke()
        ctx.restore()
      })

      let drawing: FabricObject | null = null
      let origin = new Point(0, 0)
      // Pan gesture state (Hand tool, space-drag, middle-button drag; D5).
      let panning = false
      let panLast = { x: 0, y: 0 }

      const wantsPan = (e: MouseEvent | TouchEvent): boolean =>
        toolRef.current === 'hand' || spaceDownRef.current || ('button' in e && (e as MouseEvent).button === 1)

      c.on('mouse:down', (opt) => {
        const ev = opt.e as MouseEvent
        if (wantsPan(ev)) {
          panning = true
          userAdjustedRef.current = true
          panLast = { x: ev.clientX, y: ev.clientY }
          c.setCursor('grabbing')
          return
        }
        // Right-click only opens the context menu; it must not start a shape.
        if ('button' in ev && ev.button === 2) {
          const p = c.getScenePoint(ev)
          menuPointRef.current = { x: p.x, y: p.y }
          return
        }
        const t = toolRef.current
        if (t === 'select' || t === 'draw') return
        if (opt.target) return // interacting with an existing object
        const p = c.getScenePoint(ev)
        origin = p
        const stroke = colorRef.current
        const sw = widthRef.current
        if (t === 'rect') {
          drawing = new Rect({ left: p.x, top: p.y, width: 1, height: 1, fill: 'transparent', stroke, strokeWidth: sw })
        } else if (t === 'ellipse') {
          drawing = new Ellipse({ left: p.x, top: p.y, rx: 1, ry: 1, fill: 'transparent', stroke, strokeWidth: sw })
        } else if (t === 'line' || t === 'arrow') {
          const style = connectorStyleRef.current
          const conn = new Connector({
            points: [
              { x: p.x, y: p.y },
              { x: p.x, y: p.y },
            ],
            routing: style.routing,
            headStart: t === 'arrow' ? style.headStart : 'none',
            headEnd: t === 'arrow' ? style.headEnd : 'none',
            stroke,
            strokeWidth: sw,
          })
          syncConnectorControls(conn)
          drawing = conn
        }
        if (drawing) {
          c.add(drawing)
          drawingRef.current = drawing
          // Snap the START endpoint too, so an arrow can begin on a shape edge.
          if (isConnector(drawing)) placeConnectorEndpoint(drawing, 'start', p.x, p.y, ev.altKey)
          c.setActiveObject(drawing)
        }
      })

      c.on('mouse:move', (opt) => {
        const ev = opt.e as MouseEvent
        if (panning) {
          panBy(c, ev.clientX - panLast.x, ev.clientY - panLast.y, canvasSizeRef.current)
          panLast = { x: ev.clientX, y: ev.clientY }
          c.setCursor('grabbing')
          c.requestRenderAll()
          return
        }
        if (!drawing) return
        const p = c.getScenePoint(ev)
        if (isConnector(drawing)) {
          placeConnectorEndpoint(drawing, 'end', p.x, p.y, ev.altKey)
        } else if (drawing instanceof Ellipse) {
          const rx = Math.abs(p.x - origin.x) / 2
          const ry = Math.abs(p.y - origin.y) / 2
          drawing.set({ rx, ry, left: Math.min(p.x, origin.x), top: Math.min(p.y, origin.y) })
        } else {
          drawing.set({
            width: Math.abs(p.x - origin.x),
            height: Math.abs(p.y - origin.y),
            left: Math.min(p.x, origin.x),
            top: Math.min(p.y, origin.y),
          })
        }
        drawing.setCoords()
        c.requestRenderAll()
      })

      c.on('mouse:up', () => {
        if (panning) {
          panning = false
          c.setCursor(toolRef.current === 'hand' || spaceDownRef.current ? 'grab' : 'default')
          return
        }
        if (drawing) {
          const tooSmall = isConnector(drawing)
            ? Math.hypot(
                drawing.points[drawing.points.length - 1].x - drawing.points[0].x,
                drawing.points[drawing.points.length - 1].y - drawing.points[0].y,
              ) < 3
            : (drawing.width ?? 0) < 3 && (drawing.height ?? 0) < 3
          if (tooSmall) {
            c.remove(drawing)
          } else {
            if (isConnector(drawing)) {
              commitPendingBind(drawing)
              syncConnectorControls(drawing)
            }
            snapshot()
          }
          drawing = null
          drawingRef.current = null
          setTool('select')
        }
      })

      // Wheel: pan by default, zoom at the CURSOR with Ctrl/Cmd (D4 / D5).
      c.on('mouse:wheel', (opt) => {
        const e = opt.e as WheelEvent
        e.preventDefault()
        e.stopPropagation()
        userAdjustedRef.current = true
        if (e.ctrlKey || e.metaKey) {
          const next = c.getZoom() * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP)
          zoomAtPoint(c, next, e.offsetX, e.offsetY, canvasSizeRef.current)
          setZoom(c.getZoom())
        } else if (e.shiftKey) {
          panBy(c, -(e.deltaY || e.deltaX), 0, canvasSizeRef.current)
        } else {
          panBy(c, -e.deltaX, -e.deltaY, canvasSizeRef.current)
        }
        c.requestRenderAll()
      })

      c.on('path:created', () => snapshot())

      // Bound endpoints follow their targets (D14). Recomputation runs BEFORE
      // the history snapshot so the new positions are captured, and never
      // snapshots on its own -- moving a shape is ONE user action no matter how
      // many connectors follow it (D18).
      c.on('object:moving', (opt) => {
        const t = opt.target
        if (isConnector(t)) t.applyMoveDelta()
        refreshConnectorBindings(c)
      })
      c.on('object:modified', (opt) => {
        const t = opt.target
        if (isConnector(t)) t.applyMoveDelta()
        refreshConnectorBindings(c)
        if (isConnector(t)) commitPendingBind(t)
        snapshot()
      })
      c.on('object:removed', () => {
        if (restoringRef.current) return
        refreshConnectorBindings(c)
      })
      c.on('object:added', (opt) => {
        if (opt.target) ensureScenePids(c)
      })

      // Mirror canvas selection into the Layers panel (highlight the active row).
      const syncActive = () => {
        const a = c.getActiveObject() ?? null
        if (isConnector(a)) syncConnectorControls(a)
        setActiveObj(a)
      }
      c.on('selection:created', syncActive)
      c.on('selection:updated', syncActive)
      c.on('selection:cleared', () => setActiveObj(null))

      // Double-click a text object to (re-)edit it via the overlay editor.
      c.on('mouse:dblclick', (opt) => {
        if (opt.target instanceof Textbox) startTextEditRef.current(opt.target, false)
      })

      // Seed history, size the element to the stage, then load any scene.
      //
      // `loadFromJSON` is the ONLY await in the init path, which is why re-editing
      // a saved paint was the only case that broke: under StrictMode this canvas
      // is disposed mid-await, and Fabric's own `clear()` at the end of the load
      // then throws `Cannot read properties of undefined (reading 'clearRect')`
      // off the disposed instance. That aborted the rest of `seed`, so the scene
      // never landed (a blank editor) and `restoringRef` stayed true forever,
      // which silently disables `snapshot()` -- no undo/redo and no dirty tracking
      // for the rest of the session.
      const seed = async () => {
        needsFitRef.current = true
        syncStageSize()
        if (initialScene) {
          restoringRef.current = true
          try {
            await c.loadFromJSON(initialScene as object)
            if (fabricRef.current !== c) return // disposed while loading; the live canvas seeds itself
            c.backgroundColor = ''
            ensureScenePids(c)
            refreshConnectorBindings(c)
            restoreArtboardSize(c, initialScene)
            c.requestRenderAll()
          } catch (err) {
            // A dispose mid-load is expected and harmless -- the surviving canvas
            // runs its own seed. Anything else is a real failure worth seeing.
            if (fabricRef.current === c) {
              console.error('Paint: failed to load the saved scene', err)
            }
            return
          } finally {
            // MUST be reset on every path. Leaving it set makes the editor look
            // fine while silently recording no history at all.
            restoringRef.current = false
          }
        }
        if (fabricRef.current !== c) return
        historyRef.current = [JSON.stringify(c.toJSON())]
        histIdxRef.current = 0
        setDirty(false)
        bumpLayers()
        // Paint NOW, not on the next animation frame: `requestRenderAll` would
        // leave one frame in which the canvas is still transparent and the pane's
        // grey backdrop is all there is to see.
        c.renderAll()
      }
      void seed()
    },
    [initialScene, snapshot, bumpLayers, syncStageSize, restoreArtboardSize],
  )

  // Apply tool changes to the live canvas (drawing mode + brush settings).
  useEffect(() => {
    const c = fabricRef.current
    if (!c) return
    c.isDrawingMode = tool === 'draw'
    c.selection = tool === 'select'
    // Any tool other than Select draws instead of grabbing, so a connector can
    // start ON a shape edge rather than being swallowed by it.
    c.skipTargetFind = tool !== 'select'
    if (tool === 'draw') {
      const brush = new PencilBrush(c)
      brush.color = color
      brush.width = width
      c.freeDrawingBrush = brush
    }
    c.defaultCursor = tool === 'hand' ? 'grab' : tool === 'select' ? 'default' : 'crosshair'
  }, [tool, color, width])

  // Keep the canvas element matched to the stage and Fabric's cached offset
  // fresh (D1). This ResizeObserver is what makes the pointer stay under the
  // cursor when the window, the sidebar, or the toolbar reflows.
  useEffect(() => {
    if (!open) return
    const el = paneRef.current ?? stageRef.current
    if (!el) return
    const ro = new ResizeObserver(() => syncStageSize())
    ro.observe(el)
    // One more pass after the browser has laid the dialog out. The observer's
    // first callback normally covers this, but a re-sync on the next frame costs
    // nothing and removes any dependence on when that callback lands.
    const raf = requestAnimationFrame(() => syncStageSize())
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [open, syncStageSize])

  // Space = temporary Hand in any tool (D5).
  useEffect(() => {
    if (!open) return
    const down = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || focusOwnsKeys() || textEditRef.current) return
      if (!spaceDownRef.current) {
        spaceDownRef.current = true
        fabricRef.current?.setCursor('grab')
      }
      e.preventDefault()
    }
    const up = (e: KeyboardEvent) => {
      if (e.code !== 'Space') return
      spaceDownRef.current = false
      fabricRef.current?.setCursor(toolRef.current === 'hand' ? 'grab' : 'default')
    }
    document.addEventListener('keydown', down)
    document.addEventListener('keyup', up)
    return () => {
      document.removeEventListener('keydown', down)
      document.removeEventListener('keyup', up)
      spaceDownRef.current = false
    }
  }, [open])

  // ---- Imports (file picker, paste, drop) ----
  const addImageFromDataUrl = useCallback(
    async (dataUrl: string) => {
      const c = fabricRef.current
      if (!c) return
      const img = await FabricImage.fromURL(dataUrl)
      const art = canvasSizeRef.current
      const scale = Math.min(1, (art.w * 0.9) / (img.width ?? 1), (art.h * 0.9) / (img.height ?? 1))
      img.scale(scale)
      img.set({ left: 24, top: 24 })
      c.add(img)
      // Imported art should be immediately movable/scalable (UX request).
      setTool('select')
      c.setActiveObject(img)
      c.requestRenderAll()
      snapshot()
    },
    [snapshot],
  )

  const addSvgFromString = useCallback(
    async (svg: string) => {
      const c = fabricRef.current
      if (!c) return
      const { objects } = await loadSVGFromString(svg)
      const valid = objects.filter((o): o is FabricObject => o != null)
      if (valid.length === 0) return
      const obj = valid.length === 1 ? valid[0] : new Group(valid)
      obj.set({ left: 24, top: 24 })
      c.add(obj)
      setTool('select')
      c.setActiveObject(obj)
      c.requestRenderAll()
      snapshot()
    },
    [snapshot],
  )

  const ingestFile = useCallback(
    async (file: File) => {
      if (!file.type.startsWith('image/')) return
      // Show the animated indicator until the image is fetched, decoded, and on
      // the canvas -- imports (especially from the workspace) may take a moment.
      setImporting(true)
      try {
        if (file.type === 'image/svg+xml') {
          await addSvgFromString(await file.text())
        } else {
          const dataUrl = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => resolve(reader.result as string)
            reader.onerror = () => reject(reader.error ?? new Error('read failed'))
            reader.readAsDataURL(file)
          })
          await addImageFromDataUrl(dataUrl)
        }
      } catch {
        // Best-effort import: a failed read/decode simply adds nothing.
      } finally {
        setImporting(false)
      }
    },
    [addImageFromDataUrl, addSvgFromString],
  )

  // ---- Object clipboard (D6) ----
  const serializeSelection = useCallback((): Record<string, unknown>[] | null => {
    const c = fabricRef.current
    if (!c) return null
    const objs = c.getActiveObjects()
    if (objs.length === 0) return null
    return objs.map((o) => o.toObject() as Record<string, unknown>)
  }, [])

  const pasteFrom = useCallback(
    async (data: Record<string, unknown>[], at?: { x: number; y: number }) => {
      const c = fabricRef.current
      if (!c || data.length === 0) return
      const objs = await util.enlivenObjects<FabricObject>(data)
      if (objs.length === 0) return
      // A duplicate/paste MUST mint fresh ids, and bindings are remapped to the
      // copies when both ends were copied together, dropped otherwise (D19).
      const pidMap = new Map<string, string>()
      for (const o of objs) {
        const old = getPid(o)
        const next = assignFreshPid(o)
        if (old) pidMap.set(old, next)
      }
      remapDuplicatedBindings(objs, pidMap)

      let dx = 16
      let dy = 16
      if (at) {
        let minX = Number.POSITIVE_INFINITY
        let minY = Number.POSITIVE_INFINITY
        for (const o of objs) {
          const r = o.getBoundingRect()
          minX = Math.min(minX, r.left)
          minY = Math.min(minY, r.top)
        }
        if (Number.isFinite(minX)) {
          dx = at.x - minX
          dy = at.y - minY
        }
      }
      for (const o of objs) {
        if (isConnector(o)) {
          o.translatePoints(dx, dy)
          o.updateGeometry()
          syncConnectorControls(o)
        } else {
          o.set({ left: (o.left ?? 0) + dx, top: (o.top ?? 0) + dy })
          o.setCoords()
        }
        c.add(o)
      }
      setTool('select')
      c.discardActiveObject()
      if (objs.length === 1) c.setActiveObject(objs[0])
      c.requestRenderAll()
      snapshot()
    },
    [snapshot],
  )

  const copySelection = useCallback(() => {
    const data = serializeSelection()
    if (!data) return
    clipboardRef.current = data
    setCanPaste(true)
  }, [serializeSelection])

  const deleteActive = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    const active = c.getActiveObjects()
    if (active.length === 0) return
    for (const o of active) c.remove(o)
    c.discardActiveObject()
    c.requestRenderAll()
    snapshot()
  }, [snapshot])

  const cutSelection = useCallback(() => {
    copySelection()
    deleteActive()
  }, [copySelection, deleteActive])

  const pasteClipboard = useCallback(
    (at?: { x: number; y: number }) => {
      const data = clipboardRef.current
      if (!data) return
      void pasteFrom(data, at)
    },
    [pasteFrom],
  )

  /** Duplicate does NOT touch the clipboard -- it copies through a local buffer. */
  const duplicateSelection = useCallback(() => {
    const data = serializeSelection()
    if (!data) return
    void pasteFrom(data)
  }, [serializeSelection, pasteFrom])

  // Clipboard paste while the editor is open (CTR-0160 / UDR-0078 D6): image
  // blobs are placed as images; SVG markup is auto-detected and imported as
  // editable vectors.
  //
  // Paste PRECEDENCE (UDR-0108 D6): the SYSTEM clipboard wins. Only when the
  // event carries no image and no SVG does the internal object clipboard get a
  // turn -- reversing this would regress the documented import behavior.
  useEffect(() => {
    if (!open) return
    const onPaste = (e: ClipboardEvent) => {
      // Do not hijack paste while editing text in the overlay textarea.
      if (textEditRef.current || focusOwnsKeys()) return
      const dt = e.clipboardData
      if (!dt) return
      for (const item of dt.items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const f = item.getAsFile()
          if (f) {
            e.preventDefault()
            void ingestFile(f)
            return
          }
        }
      }
      const text = dt.getData('text/plain')
      if (text && SVG_SIGNATURE.test(text)) {
        e.preventDefault()
        void addSvgFromString(text)
        return
      }
      if (clipboardRef.current) {
        e.preventDefault()
        pasteClipboard()
      }
    }
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [open, ingestFile, addSvgFromString, pasteClipboard])

  // Editor shortcuts. Every one is scoped to the open dialog and skipped while a
  // form field or the text overlay owns the keyboard (D7): Ctrl+C inside the
  // text editor must copy text, not duplicate an object. Ctrl+V is deliberately
  // absent -- it is served by the `paste` listener above so the system clipboard
  // keeps precedence by construction.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (textEditRef.current || focusOwnsKeys()) return
      const c = fabricRef.current
      if (!c) return
      const mod = e.ctrlKey || e.metaKey
      if (mod) {
        const k = e.key.toLowerCase()
        if (k === 'c') {
          e.preventDefault()
          copySelection()
        } else if (k === 'x') {
          e.preventDefault()
          cutSelection()
        } else if (k === 'd') {
          e.preventDefault()
          duplicateSelection()
        } else if (k === '0') {
          e.preventDefault()
          doFitView()
        } else if (k === '1') {
          e.preventDefault()
          setZoomLevel(1)
        }
        return
      }
      if (e.key === 'h' || e.key === 'H') {
        e.preventDefault()
        setTool((t) => (t === 'hand' ? 'select' : 'hand'))
        return
      }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        const active = c.getActiveObject()
        if (!active || (active as { isEditing?: boolean }).isEditing) return
        e.preventDefault()
        deleteActive()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, copySelection, cutSelection, duplicateSelection, deleteActive, doFitView, setZoomLevel])

  // Focus the text overlay when it opens; select all for a fresh text box so the
  // placeholder is replaced as the user types.
  useEffect(() => {
    if (!textEdit) return
    const ta = textAreaRef.current
    if (!ta) return
    ta.focus()
    if (textEdit.isNew) ta.select()
  }, [textEdit])

  // ---- Drag-and-drop onto the canvas ----
  // stopPropagation is essential: the editor is a Radix portal, so React synthetic
  // drag events would otherwise bubble through the React tree to the ChatPanel
  // drop zone and ALSO register the file as a chat attachment.
  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragDepthRef.current += 1
    if (dragHasAcceptable(e.dataTransfer)) setIsDraggingOver(true)
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const ok = dragHasAcceptable(e.dataTransfer)
    // Unsupported payloads show the no-drop cursor and cannot be dropped.
    e.dataTransfer.dropEffect = ok ? 'copy' : 'none'
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
    if (dragDepthRef.current === 0) setIsDraggingOver(false)
  }, [])

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      dragDepthRef.current = 0
      setIsDraggingOver(false)
      const img = Array.from(e.dataTransfer.files).find((f) => f.type.startsWith('image/'))
      if (img) {
        void ingestFile(img)
        return
      }
      const text = e.dataTransfer.getData('text/plain')
      if (text && SVG_SIGNATURE.test(text)) void addSvgFromString(text)
      // Anything else (unsupported file type / plain text) is ignored.
    },
    [ingestFile, addSvgFromString],
  )

  // ---- Text overlay editor ----
  // Open the HTML textarea overlay over a Textbox. The Fabric object is hidden
  // while editing so only the textarea shows; its value is written back on commit.
  const startTextEdit = useCallback((obj: Textbox, isNew: boolean) => {
    const c = fabricRef.current
    if (!c) return
    obj.set('visible', false)
    c.discardActiveObject()
    c.requestRenderAll()
    setTextEdit({ obj, isNew })
  }, [])
  startTextEditRef.current = startTextEdit

  const finishTextEdit = useCallback(
    (commit: boolean) => {
      const te = textEditRef.current
      const c = fabricRef.current
      setTextEdit(null)
      if (!te || !c) return
      const { obj, isNew } = te
      const value = (textAreaRef.current?.value ?? '').replace(/\s+$/, '')
      if (commit && value !== '') {
        obj.set({ text: value, visible: true })
        c.setActiveObject(obj)
        setActiveObj(obj)
        c.requestRenderAll()
        snapshot()
      } else if (isNew) {
        c.remove(obj) // a brand-new text left empty / cancelled is discarded
        c.requestRenderAll()
      } else {
        obj.set('visible', true) // existing text: keep prior content on cancel
        c.requestRenderAll()
      }
    },
    [snapshot],
  )

  const textOverlayStyle = (obj: Textbox): CSSProperties => {
    const c = fabricRef.current
    if (!c) return { display: 'none' }
    const zoomNow = c.getZoom()
    // Composes the FULL viewport transform, so the overlay tracks pan as well as
    // zoom -- under the v3 viewport model the translation is no longer always 0.
    const center = util.transformPoint(obj.getCenterPoint(), c.viewportTransform)
    return {
      position: 'absolute',
      left: center.x,
      top: center.y,
      transform: 'translate(-50%, -50%)',
      width: Math.max(60, (obj.width ?? 160) * (obj.scaleX ?? 1) * zoomNow),
      fontSize: (obj.fontSize ?? 24) * (obj.scaleY ?? 1) * zoomNow,
      fontFamily: obj.fontFamily,
      color: typeof obj.fill === 'string' ? obj.fill : '#111827',
      textAlign: (obj.textAlign as CSSProperties['textAlign']) ?? 'left',
      lineHeight: 1.16,
    }
  }

  // ---- Object actions ----
  const addText = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    const art = canvasSizeRef.current
    const t = new Textbox('Text', {
      left: art.w / 2 - 80,
      top: art.h / 2 - 20,
      fontSize: Math.max(16, width * 6),
      fill: color,
      width: 200,
      editable: true,
    })
    c.add(t)
    setTool('select')
    startTextEdit(t, true)
  }, [color, width, startTextEdit])

  const handleToolClick = useCallback(
    (t: Tool) => {
      if (t === 'text') {
        addText()
        return
      }
      setTool(t)
    },
    [addText],
  )

  const doReset = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    c.remove(...c.getObjects())
    c.backgroundColor = ''
    c.requestRenderAll()
    snapshot()
    setConfirm(null)
  }, [snapshot])

  const handleAttach = useCallback(async () => {
    const c = fabricRef.current
    if (!c) return
    c.discardActiveObject()
    c.requestRenderAll()
    const art = canvasSizeRef.current
    const dataUrl = exportArtboardDataUrl(c, art)
    const blob = await dataUrlToBlob(dataUrl)
    // Fabric's toJSON() serializes the OBJECTS but not the canvas dimensions, and
    // object coordinates are absolute. Without the artboard size, re-editing on a
    // smaller stage rebuilt a smaller artboard and everything drawn beyond it fell
    // outside the canvas -- the drawing was intact but invisible. Record it.
    const scene = { ...c.toJSON(), width: art.w, height: art.h }
    onAttach(blob, scene)
    setDirty(false)
    onOpenChange(false)
  }, [onAttach, onOpenChange])

  const handleDownload = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    c.discardActiveObject()
    c.requestRenderAll()
    const dataUrl = exportArtboardDataUrl(c, canvasSizeRef.current)
    const a = document.createElement('a')
    a.href = dataUrl
    a.download = `paint_${Date.now()}.png`
    a.click()
  }, [])

  // Reset/close confirmation guard (UDR-0078 D4/D8).
  const requestClose = useCallback(
    (next: boolean) => {
      if (!next && dirty) {
        setConfirm('close')
        return
      }
      onOpenChange(next)
    },
    [dirty, onOpenChange],
  )

  const requestReset = useCallback(() => {
    if (dirty) setConfirm('reset')
    else doReset()
  }, [dirty, doReset])

  // ---- Layers panel data (top object first) ----
  const layerObjects: FabricObject[] = fabricRef.current ? [...fabricRef.current.getObjects()].reverse() : []
  void layersVersion // re-render dependency

  const labelOf = (o: FabricObject): string => {
    if (isConnector(o)) return o.headStart === 'none' && o.headEnd === 'none' ? 'Line' : 'Arrow'
    const type = (o as { type?: string }).type ?? 'object'
    if (o instanceof Textbox) return `Text: ${o.text?.slice(0, 12) ?? ''}`
    return type.charAt(0).toUpperCase() + type.slice(1)
  }

  const toggleVisible = (o: FabricObject) => {
    o.visible = !o.visible
    fabricRef.current?.requestRenderAll()
    snapshot()
  }
  const toggleLock = (o: FabricObject) => {
    const locked = !o.selectable
    o.set({ selectable: locked, evented: locked })
    fabricRef.current?.requestRenderAll()
    bumpLayers()
  }
  const selectLayer = (o: FabricObject) => {
    const c = fabricRef.current
    if (!c || !o.selectable) return
    c.setActiveObject(o)
    setActiveObj(o)
    c.requestRenderAll()
  }
  const moveLayer = (o: FabricObject, dir: 'up' | 'down') => {
    const c = fabricRef.current
    if (!c) return
    if (dir === 'up') c.bringObjectForward(o)
    else c.sendObjectBackwards(o)
    // Keep the moved object selected so its row stays highlighted and the new
    // z-level is visible.
    c.setActiveObject(o)
    setActiveObj(o)
    c.requestRenderAll()
    snapshot()
  }
  const stack = (o: FabricObject, where: 'front' | 'back') => {
    const c = fabricRef.current
    if (!c) return
    if (where === 'front') c.bringObjectToFront(o)
    else c.sendObjectToBack(o)
    c.requestRenderAll()
    snapshot()
  }

  /** Build the shared context-menu actions for one object (D6). */
  const actionsFor = (o: FabricObject | null) => ({
    onDuplicate: () => {
      if (o) selectLayer(o)
      duplicateSelection()
    },
    onCut: () => {
      if (o) selectLayer(o)
      cutSelection()
    },
    onCopy: () => {
      if (o) selectLayer(o)
      copySelection()
    },
    onPaste: () => pasteClipboard(menuPointRef.current ?? undefined),
    onBringToFront: () => o && stack(o, 'front'),
    onBringForward: () => o && moveLayer(o, 'up'),
    onSendBackward: () => o && moveLayer(o, 'down'),
    onSendToBack: () => o && stack(o, 'back'),
    onToggleLock: () => o && toggleLock(o),
    onToggleVisible: () => o && toggleVisible(o),
    onDelete: () => {
      if (o) selectLayer(o)
      deleteActive()
    },
  })

  /** Right-click on the canvas selects what is under the pointer, then opens. */
  const onCanvasContextMenu = (e: React.MouseEvent) => {
    const c = fabricRef.current
    if (!c) return
    const p = c.getScenePoint(e.nativeEvent)
    menuPointRef.current = { x: p.x, y: p.y }
    const target = c.findTarget(e.nativeEvent)
    if (target) {
      c.setActiveObject(target)
      setActiveObj(target)
    } else {
      c.discardActiveObject()
      setActiveObj(null)
    }
    c.requestRenderAll()
  }

  const connectorSelected = isConnector(activeObj) ? activeObj : null
  const showConnectorOptions = CONNECTOR_TOOLS.includes(tool) || connectorSelected !== null

  /** Route a connector-option change to the selected connector, else the default. */
  const changeConnectorOptions = (patch: {
    routing?: ConnectorRouting
    headStart?: ArrowHead
    headEnd?: ArrowHead
  }) => {
    if (connectorSelected) {
      const c = fabricRef.current
      connectorSelected.set(patch as Record<string, unknown>)
      connectorSelected.updateGeometry()
      syncConnectorControls(connectorSelected)
      c?.requestRenderAll()
      snapshot()
      return
    }
    setConnectorStyle((prev) => ({ ...prev, ...patch }))
  }

  const toolButtons: { id: Tool; icon: typeof Square; label: string }[] = [
    { id: 'select', icon: MousePointer2, label: 'Select / move' },
    { id: 'hand', icon: Hand, label: 'Pan (H, or hold Space)' },
    { id: 'draw', icon: Paintbrush, label: 'Free draw' },
    { id: 'line', icon: Minus, label: 'Line (snaps to shapes)' },
    { id: 'arrow', icon: ArrowRight, label: 'Arrow (snaps to shapes)' },
    { id: 'rect', icon: Square, label: 'Rectangle' },
    { id: 'ellipse', icon: CircleIcon, label: 'Ellipse' },
    { id: 'text', icon: Type, label: 'Text' },
  ]

  const iconBtn = 'inline-flex h-8 w-8 items-center justify-center rounded-md text-zinc-600 hover:bg-zinc-100'

  return (
    <>
      <Dialog open={open} onOpenChange={requestClose}>
        <DialogContent className="flex h-screen w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-zinc-50 p-0 text-zinc-900 sm:rounded-none">
          <DialogHeader className="flex shrink-0 flex-row items-center justify-between border-b bg-white px-3 py-2 pr-12 text-left">
            <DialogTitle className="text-sm font-semibold text-zinc-900">Paint</DialogTitle>
            {/* Radix requires an accessible description on every DialogContent; without
                one it warns and screen-reader users get no summary of the dialog. */}
            <DialogDescription className="sr-only">
              Compose an image on a canvas: draw shapes and freehand strokes, add text, connect shapes with arrows,
              import pictures, arrange layers, then attach the result to your message.
            </DialogDescription>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" className="h-8" onClick={handleDownload}>
                <Download className="mr-1.5 h-4 w-4" />
                Download
              </Button>
              <Button size="sm" className="h-8" onClick={() => void handleAttach()}>
                <ImagePlus className="mr-1.5 h-4 w-4" />
                Attach
              </Button>
            </div>
          </DialogHeader>

          {/* Toolbar */}
          <div className="flex shrink-0 flex-wrap items-center gap-1 border-b bg-white px-3 py-1.5">
            {toolButtons.map((b) => (
              <button
                key={b.id}
                type="button"
                title={b.label}
                aria-label={b.label}
                onClick={() => handleToolClick(b.id)}
                className={cn(iconBtn, tool === b.id && 'bg-blue-100 text-blue-700')}>
                <b.icon className="h-4 w-4" />
              </button>
            ))}

            <span className="mx-2 h-5 w-px bg-zinc-200" />

            {/* Color palette */}
            <div className="flex items-center gap-1">
              {PALETTE.map((c) => (
                <button
                  key={c}
                  type="button"
                  aria-label={`Color ${c}`}
                  onClick={() => setColor(c)}
                  className={cn(
                    'h-5 w-5 rounded-full border border-zinc-300 transition-transform hover:scale-110',
                    color === c && 'ring-2 ring-blue-500 ring-offset-1',
                  )}
                  style={{ backgroundColor: c }}
                />
              ))}
              <input
                type="color"
                value={color}
                onChange={(e) => setColor(e.target.value)}
                aria-label="Custom color"
                className="ml-1 h-6 w-6 cursor-pointer rounded border border-zinc-300 bg-transparent p-0"
              />
            </div>

            <span className="mx-2 h-5 w-px bg-zinc-200" />

            {/* Stroke width */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-500">Width</span>
              <input
                type="range"
                min={1}
                max={40}
                value={width}
                onChange={(e) => setWidth(Number(e.target.value))}
                className="w-24"
                aria-label="Stroke width"
              />
              {/* v0.112.2: the value is also directly typeable, not just draggable.
                  Empty input is allowed while typing; the value is clamped to [1, 40]
                  on change and normalized on blur so the canvas never gets NaN. */}
              <input
                type="number"
                min={1}
                max={40}
                value={width}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  if (Number.isFinite(n)) setWidth(Math.min(40, Math.max(1, Math.round(n))))
                }}
                onBlur={(e) => {
                  const n = Number(e.target.value)
                  setWidth(Number.isFinite(n) && n > 0 ? Math.min(40, Math.max(1, Math.round(n))) : 1)
                }}
                className="w-12 rounded border border-zinc-200 bg-white px-1 py-0.5 text-xs tabular-nums text-zinc-700 outline-none focus:ring-1 focus:ring-zinc-400"
                aria-label="Stroke width value"
              />
            </div>

            {showConnectorOptions && (
              <>
                <span className="mx-2 h-5 w-px bg-zinc-200" />
                <PaintConnectorOptions
                  routing={connectorSelected?.routing ?? connectorStyle.routing}
                  headStart={connectorSelected?.headStart ?? connectorStyle.headStart}
                  headEnd={connectorSelected?.headEnd ?? connectorStyle.headEnd}
                  onChange={changeConnectorOptions}
                />
              </>
            )}

            <span className="mx-2 h-5 w-px bg-zinc-200" />

            {/* Import image (CTR-0160 v2): when the coding workspace is available,
                branch between a local device upload and a workspace image; otherwise
                the button opens the local file picker directly. */}
            {workspaceAvailable ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button type="button" title="Import image" aria-label="Import image" className={iconBtn}>
                    <ImagePlus className="h-4 w-4" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start">
                  <DropdownMenuItem onSelect={() => fileInputRef.current?.click()}>
                    <HardDrive className="mr-2 h-4 w-4" /> From this device
                  </DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => setWorkspacePickerOpen(true)}>
                    <FolderOpen className="mr-2 h-4 w-4" /> From workspace
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : (
              <button
                type="button"
                title="Import image"
                aria-label="Import image"
                onClick={() => fileInputRef.current?.click()}
                className={iconBtn}>
                <ImagePlus className="h-4 w-4" />
              </button>
            )}
            <button type="button" title="Undo" aria-label="Undo" onClick={undo} className={iconBtn}>
              <Undo2 className="h-4 w-4" />
            </button>
            <button type="button" title="Redo" aria-label="Redo" onClick={redo} className={iconBtn}>
              <Redo2 className="h-4 w-4" />
            </button>
            <button
              type="button"
              title="Delete selected (Del)"
              aria-label="Delete selected"
              onClick={deleteActive}
              className={cn(iconBtn, 'hover:bg-red-50 hover:text-red-600')}>
              <Trash2 className="h-4 w-4" />
            </button>
            <button
              type="button"
              title="Reset canvas"
              aria-label="Reset canvas"
              onClick={requestReset}
              className={iconBtn}>
              <RotateCcw className="h-4 w-4" />
            </button>

            <span className="mx-2 h-5 w-px bg-zinc-200" />

            {/* Artboard size: ratio presets + a bounded custom lane (D2 / D3). */}
            <PaintCanvasSizeControl
              size={canvasSize}
              fitMode={fitMode}
              onApply={applyCanvasSize}
              onFitToWindow={applyFitMode}
            />

            <span className="mx-2 h-5 w-px bg-zinc-200" />

            {/* Zoom: multiplicative steps over a wide range (D4). */}
            <button
              type="button"
              title="Zoom out"
              aria-label="Zoom out"
              onClick={() => setZoomLevel(zoom / ZOOM_STEP)}
              className={iconBtn}>
              <ZoomOut className="h-4 w-4" />
            </button>
            <span className="w-10 text-center text-xs tabular-nums text-zinc-500">{Math.round(zoom * 100)}%</span>
            <button
              type="button"
              title="Zoom in"
              aria-label="Zoom in"
              onClick={() => setZoomLevel(zoom * ZOOM_STEP)}
              className={iconBtn}>
              <ZoomIn className="h-4 w-4" />
            </button>
            <button
              type="button"
              title="Fit the artboard (Ctrl+0)"
              aria-label="Fit the artboard"
              onClick={doFitView}
              className={cn(iconBtn, 'h-7 w-7')}>
              <Maximize className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              title="Zoom to 100% (Ctrl+1)"
              aria-label="Zoom to 100 percent"
              onClick={() => setZoomLevel(1)}
              className="h-7 rounded-md px-1.5 text-[11px] text-zinc-600 hover:bg-zinc-100">
              1:1
            </button>
            <button
              type="button"
              title="Zoom to selection"
              aria-label="Zoom to selection"
              disabled={!activeObj}
              onClick={doZoomToSelection}
              className="h-7 rounded-md px-1.5 text-[11px] text-zinc-600 hover:bg-zinc-100 disabled:opacity-40">
              Sel
            </button>
          </div>

          {/* Body: stage (holds the viewport-sized canvas) + layers panel */}
          <div className="relative flex min-h-0 flex-1">
            {/* Import indicator (CTR-0160 v2): shown until an imported image is decoded onto the canvas. */}
            {importing && (
              <div className="absolute inset-0 z-30 flex items-center justify-center bg-zinc-50/70">
                <div className="flex items-center gap-2 rounded-md bg-white px-4 py-2 text-sm text-zinc-700 shadow-md">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading image...
                </div>
              </div>
            )}
            {/* The stage is NOT a scroll container (D1). Panning is a viewport
                transform, so the canvas element never moves inside a scrolling
                parent and Fabric's cached pointer offset cannot go stale. */}
            {/* biome-ignore lint/a11y/noStaticElementInteractions: canvas drop target needs drag events */}
            <div
              ref={stageRef}
              onDrop={handleDrop}
              onDragEnter={handleDragEnter}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              className="relative min-w-0 flex-1 overflow-hidden bg-zinc-300/70">
              <PaintObjectContextMenu
                asChild
                hasTarget={activeObj !== null}
                canPaste={canPaste}
                locked={activeObj ? !activeObj.selectable : false}
                visible={activeObj ? activeObj.visible !== false : true}
                onOpen={onCanvasContextMenu}
                {...actionsFor(activeObj)}>
                {/* Fabric inserts its own `.canvas-container` around the <canvas>
                    and gives it an INLINE pixel size from setDimensions, so it
                    can lag the stage and leave the drawable pane as a
                    sub-rectangle with dead grey space around it. The container is
                    therefore pinned to fill the stage.
                    The <canvas> elements are deliberately NOT pinned. Fabric
                    sizes the BITMAP to `clientWidth * devicePixelRatio` and the
                    CSS to `clientWidth` px -- an exact integer mapping, which is
                    what makes the drawing crisp. `clientWidth` is rounded, while
                    a flex pane is usually a fractional width, so forcing the
                    canvas CSS to 100% stretches an integer bitmap over a
                    fractional box and resamples the ENTIRE canvas. That reads as
                    a permanently slightly-blurry drawing. Let Fabric own the
                    canvas CSS; the sub-pixel sliver it can leave at the edge is
                    invisible, and the pane is filled by the container. */}
                <div
                  ref={paneRef}
                  className="absolute inset-0 [&>.canvas-container]:!absolute [&>.canvas-container]:!inset-0 [&>.canvas-container]:!h-full [&>.canvas-container]:!w-full">
                  <canvas ref={attachCanvas} />
                  {textEdit && (
                    <textarea
                      ref={textAreaRef}
                      defaultValue={textEdit.obj.text ?? ''}
                      onBlur={() => finishTextEdit(true)}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape') {
                          e.preventDefault()
                          finishTextEdit(false)
                        } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                          e.preventDefault()
                          finishTextEdit(true)
                        }
                      }}
                      style={textOverlayStyle(textEdit.obj)}
                      className="z-20 resize-none overflow-hidden rounded-sm border border-blue-400 bg-white/95 px-1 shadow outline-none"
                      aria-label="Edit text"
                    />
                  )}
                </div>
              </PaintObjectContextMenu>
              {isDraggingOver && (
                <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-blue-500/10 backdrop-blur-[1px]">
                  <div className="rounded-xl border-2 border-dashed border-blue-500 bg-white/90 px-6 py-4 text-sm font-medium text-blue-700">
                    Drop an image or SVG to add it to the canvas
                  </div>
                </div>
              )}
            </div>

            {/* Layers panel */}
            <div className="flex w-60 shrink-0 flex-col border-l bg-white">
              <div className="flex shrink-0 items-center justify-between border-b px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                <span>Layers</span>
                <span className="normal-case text-[10px] text-zinc-400">top = front</span>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-1">
                {layerObjects.length === 0 && (
                  <p className="px-2 py-3 text-xs text-zinc-400">Draw, add text, or paste / drop an image to begin.</p>
                )}
                {layerObjects.map((o, i) => {
                  const level = layerObjects.length - i // top row = highest z
                  const isActive = o === activeObj
                  const isTop = i === 0
                  const isBottom = i === layerObjects.length - 1
                  return (
                    <PaintObjectContextMenu
                      key={getPid(o) ?? `${labelOf(o)}-${i}`}
                      asChild
                      hasTarget
                      canPaste={canPaste}
                      locked={!o.selectable}
                      visible={o.visible !== false}
                      {...actionsFor(o)}>
                      <div
                        className={cn(
                          'flex items-center gap-1 rounded px-1 py-1 text-xs',
                          isActive ? 'bg-blue-100 ring-1 ring-blue-300' : 'hover:bg-zinc-100',
                        )}>
                        <span
                          className="w-5 shrink-0 text-center text-[10px] font-medium tabular-nums text-zinc-400"
                          title={`Layer ${level} of ${layerObjects.length}`}>
                          {level}
                        </span>
                        <button
                          type="button"
                          onClick={() => toggleVisible(o)}
                          className="text-zinc-400 hover:text-zinc-700"
                          aria-label="Toggle visibility">
                          {o.visible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                        </button>
                        <button
                          type="button"
                          onClick={() => selectLayer(o)}
                          className={cn('flex-1 truncate text-left', isActive ? 'text-blue-900' : 'text-zinc-700')}>
                          {labelOf(o)}
                        </button>
                        <button
                          type="button"
                          onClick={() => moveLayer(o, 'up')}
                          disabled={isTop}
                          className="text-zinc-400 hover:text-zinc-700 disabled:opacity-30"
                          aria-label="Bring forward">
                          <ChevronUp className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => moveLayer(o, 'down')}
                          disabled={isBottom}
                          className="text-zinc-400 hover:text-zinc-700 disabled:opacity-30"
                          aria-label="Send backward">
                          <ChevronDown className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => toggleLock(o)}
                          className="text-zinc-400 hover:text-zinc-700"
                          aria-label="Toggle lock">
                          {o.selectable ? <Unlock className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
                        </button>
                      </div>
                    </PaintObjectContextMenu>
                  )
                })}
              </div>
              <div className="shrink-0 border-t px-3 py-1.5 text-[10px] leading-snug text-zinc-400">
                Space / middle-drag pans. Ctrl+wheel zooms. Up to {MAX_CANVAS_EDGE} px per side,{' '}
                {(MAX_CANVAS_PX / 1_000_000).toFixed(0)} MPx total.
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <input
        ref={fileInputRef}
        type="file"
        accept=".png,.jpg,.jpeg,.gif,.webp,.bmp,.svg,image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) void ingestFile(f)
          e.target.value = ''
        }}
      />

      {/* Workspace image picker (CTR-0160 v2 / UDR-0078 D10): browse the coding
          workspace and load an image via CTR-0136, for upload-restricted sites. */}
      <WorkspaceImagePicker
        open={workspacePickerOpen}
        onOpenChange={setWorkspacePickerOpen}
        onPick={(file) => {
          setWorkspacePickerOpen(false)
          void ingestFile(file)
        }}
      />

      {/* Reset / close unsaved-content guard (UDR-0078 D4). */}
      <AlertDialog open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirm === 'reset' ? 'Reset the canvas?' : 'Discard this paint?'}</AlertDialogTitle>
            <AlertDialogDescription>
              {confirm === 'reset'
                ? 'This clears every object on the canvas. You can undo afterwards.'
                : 'You have unsaved changes. Closing now discards them. Attach first to keep your paint.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (confirm === 'reset') doReset()
                else {
                  setConfirm(null)
                  onOpenChange(false)
                }
              }}>
              {confirm === 'reset' ? 'Reset' : 'Discard'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
