import {
  Canvas,
  Ellipse,
  FabricImage,
  type FabricObject,
  Group,
  Line,
  loadSVGFromString,
  PencilBrush,
  Point,
  Rect,
  Textbox,
  util,
} from 'fabric'
import {
  ChevronDown,
  ChevronUp,
  Circle as CircleIcon,
  Download,
  Eye,
  EyeOff,
  FolderOpen,
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
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { WorkspaceImagePicker } from '@/components/WorkspaceImagePicker'
import { useFileExplorerAvailable } from '@/hooks/useFileExplorerAvailable'
import { cn } from '@/lib/utils'

type Tool = 'select' | 'draw' | 'rect' | 'ellipse' | 'line' | 'text'

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
const MIN_CANVAS = 64

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

/**
 * Full-screen object-based paint editor (CTR-0160, PRP-0099).
 *
 * Built on Fabric.js v6 for the object model, per-object transform handles,
 * scene serialization (re-editability), and native SVG import (UDR-0078 D1).
 * Reuses the CTR-0137 File Explorer full-screen Dialog recipe and an
 * unsaved-content close/reset guard (UDR-0078 D4).
 */
export function PaintEditor({ open, onOpenChange, initialScene, onAttach }: PaintEditorProps) {
  const stageRef = useRef<HTMLDivElement | null>(null)
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

  // Artboard pixel dimensions (also the export size). In fit mode the artboard
  // tracks the stage so the whole area is drawable; the user can set an explicit
  // size, which turns fit mode off.
  const [canvasSize, setCanvasSize] = useState({ w: 960, h: 600 })
  const canvasSizeRef = useRef(canvasSize)
  canvasSizeRef.current = canvasSize
  const [fitMode, setFitMode] = useState(true)
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

  // ---- Canvas sizing ----
  const applyCanvasSize = useCallback((w: number, h: number) => {
    const W = Math.max(MIN_CANVAS, Math.round(w))
    const H = Math.max(MIN_CANVAS, Math.round(h))
    setCanvasSize((prev) => (prev.w === W && prev.h === H ? prev : { w: W, h: H }))
    fabricRef.current?.setDimensions({ width: W, height: H })
  }, [])

  const fitToStage = useCallback(() => {
    const stage = stageRef.current
    if (!stage) return
    applyCanvasSize(stage.clientWidth, stage.clientHeight)
  }, [applyCanvasSize])

  // ---- Canvas lifecycle ----
  // Initialize Fabric when the <canvas> node mounts (Radix renders the dialog
  // body only while open), and dispose when it unmounts.
  const attachCanvas = useCallback(
    (node: HTMLCanvasElement | null) => {
      if (!node) {
        fabricRef.current?.dispose()
        fabricRef.current = null
        return
      }
      const init = canvasSizeRef.current
      const c = new Canvas(node, {
        width: init.w,
        height: init.h,
        backgroundColor: '#ffffff',
        preserveObjectStacking: true,
      })
      fabricRef.current = c

      let drawing: FabricObject | null = null
      let origin = new Point(0, 0)

      c.on('mouse:down', (opt) => {
        const t = toolRef.current
        if (t === 'select' || t === 'draw') return
        if (opt.target) return // interacting with an existing object
        const p = c.getScenePoint(opt.e)
        origin = p
        const stroke = colorRef.current
        const sw = widthRef.current
        if (t === 'rect') {
          drawing = new Rect({ left: p.x, top: p.y, width: 1, height: 1, fill: 'transparent', stroke, strokeWidth: sw })
        } else if (t === 'ellipse') {
          drawing = new Ellipse({ left: p.x, top: p.y, rx: 1, ry: 1, fill: 'transparent', stroke, strokeWidth: sw })
        } else if (t === 'line') {
          drawing = new Line([p.x, p.y, p.x, p.y], { stroke, strokeWidth: sw })
        }
        if (drawing) {
          c.add(drawing)
          c.setActiveObject(drawing)
        }
      })

      c.on('mouse:move', (opt) => {
        if (!drawing) return
        const p = c.getScenePoint(opt.e)
        if (drawing instanceof Line) {
          drawing.set({ x2: p.x, y2: p.y })
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
        if (drawing) {
          const tooSmall =
            drawing instanceof Line
              ? Math.hypot((drawing.x2 ?? 0) - (drawing.x1 ?? 0), (drawing.y2 ?? 0) - (drawing.y1 ?? 0)) < 3
              : (drawing.width ?? 0) < 3 && (drawing.height ?? 0) < 3
          if (tooSmall) {
            c.remove(drawing)
          } else {
            snapshot()
          }
          drawing = null
          setTool('select')
        }
      })

      c.on('path:created', () => snapshot())
      c.on('object:modified', () => snapshot())

      // Mirror canvas selection into the Layers panel (highlight the active row).
      const syncActive = () => setActiveObj(c.getActiveObject() ?? null)
      c.on('selection:created', syncActive)
      c.on('selection:updated', syncActive)
      c.on('selection:cleared', () => setActiveObj(null))

      // Double-click a text object to (re-)edit it via the overlay editor.
      c.on('mouse:dblclick', (opt) => {
        if (opt.target instanceof Textbox) startTextEditRef.current(opt.target, false)
      })

      // Seed history, fit the artboard to the stage, then load any scene.
      const seed = async () => {
        if (fitModeRef.current) fitToStage()
        if (initialScene) {
          restoringRef.current = true
          await c.loadFromJSON(initialScene as object)
          c.requestRenderAll()
          restoringRef.current = false
        }
        historyRef.current = [JSON.stringify(c.toJSON())]
        histIdxRef.current = 0
        setDirty(false)
        bumpLayers()
      }
      void seed()
    },
    [initialScene, snapshot, bumpLayers, fitToStage],
  )

  // Apply tool changes to the live canvas (drawing mode + brush settings).
  useEffect(() => {
    const c = fabricRef.current
    if (!c) return
    c.isDrawingMode = tool === 'draw'
    c.selection = tool === 'select'
    if (tool === 'draw') {
      const brush = new PencilBrush(c)
      brush.color = color
      brush.width = width
      c.freeDrawingBrush = brush
    }
    c.defaultCursor = tool === 'select' ? 'default' : 'crosshair'
  }, [tool, color, width])

  // Keep the artboard filling the stage while in fit mode.
  useEffect(() => {
    if (!open) return
    const stage = stageRef.current
    if (!stage) return
    const ro = new ResizeObserver(() => {
      if (fitModeRef.current) fitToStage()
    })
    ro.observe(stage)
    return () => ro.disconnect()
  }, [open, fitToStage])

  // ---- Imports (file picker, paste, drop) ----
  const addImageFromDataUrl = useCallback(
    async (dataUrl: string) => {
      const c = fabricRef.current
      if (!c) return
      const img = await FabricImage.fromURL(dataUrl)
      const maxW = c.getWidth() * 0.9
      const maxH = c.getHeight() * 0.9
      const scale = Math.min(1, maxW / (img.width ?? 1), maxH / (img.height ?? 1))
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

  // Clipboard paste while the editor is open (CTR-0160 / UDR-0078 D6): image
  // blobs are placed as images; SVG markup is auto-detected and imported as
  // editable vectors; other text is ignored.
  useEffect(() => {
    if (!open) return
    const onPaste = (e: ClipboardEvent) => {
      // Do not hijack paste while editing text in the overlay textarea.
      if (textEditRef.current) return
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
      }
    }
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [open, ingestFile, addSvgFromString])

  // Delete / Backspace removes the selection (CTR-0160). Skipped while typing in
  // a form field or editing a text object so it does not eat keystrokes.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      const ae = document.activeElement as HTMLElement | null
      if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return
      const c = fabricRef.current
      const active = c?.getActiveObject()
      if (!active || (active as { isEditing?: boolean }).isEditing) return
      e.preventDefault()
      for (const o of c?.getActiveObjects() ?? []) c?.remove(o)
      c?.discardActiveObject()
      c?.requestRenderAll()
      snapshot()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, snapshot])

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
    const zoom = c.getZoom()
    const center = util.transformPoint(obj.getCenterPoint(), c.viewportTransform)
    return {
      position: 'absolute',
      left: center.x,
      top: center.y,
      transform: 'translate(-50%, -50%)',
      width: Math.max(60, (obj.width ?? 160) * (obj.scaleX ?? 1) * zoom),
      fontSize: (obj.fontSize ?? 24) * (obj.scaleY ?? 1) * zoom,
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
    const t = new Textbox('Text', {
      left: c.getWidth() / 2 - 80,
      top: c.getHeight() / 2 - 20,
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

  const setZoomLevel = useCallback((z: number) => {
    const c = fabricRef.current
    if (!c) return
    const next = Math.min(4, Math.max(0.2, z))
    c.zoomToPoint(new Point(c.getWidth() / 2, c.getHeight() / 2), next)
    setZoom(next)
  }, [])

  const doReset = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    c.remove(...c.getObjects())
    c.backgroundColor = '#ffffff'
    c.requestRenderAll()
    snapshot()
    setConfirm(null)
  }, [snapshot])

  const handleAttach = useCallback(async () => {
    const c = fabricRef.current
    if (!c) return
    c.discardActiveObject()
    c.requestRenderAll()
    const dataUrl = c.toDataURL({ format: 'png', multiplier: 1 })
    const blob = await dataUrlToBlob(dataUrl)
    const scene = c.toJSON()
    onAttach(blob, scene)
    setDirty(false)
    onOpenChange(false)
  }, [onAttach, onOpenChange])

  const handleDownload = useCallback(() => {
    const c = fabricRef.current
    if (!c) return
    c.discardActiveObject()
    c.requestRenderAll()
    const dataUrl = c.toDataURL({ format: 'png', multiplier: 1 })
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

  const editSize = (dim: 'w' | 'h', value: number) => {
    setFitMode(false)
    const next = dim === 'w' ? { w: value, h: canvasSize.h } : { w: canvasSize.w, h: value }
    applyCanvasSize(next.w, next.h)
  }

  const toolButtons: { id: Tool; icon: typeof Square; label: string }[] = [
    { id: 'select', icon: MousePointer2, label: 'Select / move' },
    { id: 'draw', icon: Paintbrush, label: 'Free draw' },
    { id: 'line', icon: Minus, label: 'Line' },
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

            {/* Canvas size */}
            <div className="flex items-center gap-1 text-xs text-zinc-500">
              <span>Canvas</span>
              <input
                type="number"
                min={MIN_CANVAS}
                value={canvasSize.w}
                onChange={(e) => editSize('w', Number(e.target.value))}
                aria-label="Canvas width"
                className="h-7 w-16 rounded border border-zinc-300 px-1 text-center tabular-nums"
              />
              <span>x</span>
              <input
                type="number"
                min={MIN_CANVAS}
                value={canvasSize.h}
                onChange={(e) => editSize('h', Number(e.target.value))}
                aria-label="Canvas height"
                className="h-7 w-16 rounded border border-zinc-300 px-1 text-center tabular-nums"
              />
              <button
                type="button"
                title="Fit to screen"
                aria-label="Fit to screen"
                onClick={() => {
                  setFitMode(true)
                  fitToStage()
                }}
                className={cn(iconBtn, 'h-7 w-7', fitMode && 'bg-blue-100 text-blue-700')}>
                <Maximize className="h-3.5 w-3.5" />
              </button>
            </div>

            <span className="mx-2 h-5 w-px bg-zinc-200" />

            <button
              type="button"
              title="Zoom out"
              aria-label="Zoom out"
              onClick={() => setZoomLevel(zoom - 0.2)}
              className={iconBtn}>
              <ZoomOut className="h-4 w-4" />
            </button>
            <span className="w-10 text-center text-xs tabular-nums text-zinc-500">{Math.round(zoom * 100)}%</span>
            <button
              type="button"
              title="Zoom in"
              aria-label="Zoom in"
              onClick={() => setZoomLevel(zoom + 0.2)}
              className={iconBtn}>
              <ZoomIn className="h-4 w-4" />
            </button>
          </div>

          {/* Body: stage (scrollable, holds the artboard) + layers panel */}
          <div className="relative flex min-h-0 flex-1">
            {/* Import indicator (CTR-0160 v2): shown until an imported image is decoded onto the canvas. */}
            {importing && (
              <div className="absolute inset-0 z-30 flex items-center justify-center bg-zinc-50/70">
                <div className="flex items-center gap-2 rounded-md bg-white px-4 py-2 text-sm text-zinc-700 shadow-md">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading image...
                </div>
              </div>
            )}
            {/* biome-ignore lint/a11y/noStaticElementInteractions: canvas drop target needs drag events */}
            <div
              ref={stageRef}
              onDrop={handleDrop}
              onDragEnter={handleDragEnter}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              className="relative min-w-0 flex-1 overflow-auto bg-zinc-200/70">
              <div className="flex min-h-full min-w-full items-center justify-center">
                <div className="relative shadow-lg ring-1 ring-zinc-300">
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
              </div>
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
                    <div
                      key={(o as { __uid?: number }).__uid ?? `${labelOf(o)}-${i}`}
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
                  )
                })}
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
