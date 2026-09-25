import {
  Eraser,
  Highlighter,
  ImagePlus,
  Paintbrush,
  Redo2,
  Square,
  SquareDashedMousePointer,
  Trash2,
  Undo2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { AuthedImage } from '@/components/AuthedImage'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  type BrushSize,
  clampMaskOpacity,
  DEFAULT_MASK_COLOR,
  DEFAULT_MASK_OPACITY,
  isHexColor,
  MASK_COLOR_PRESETS,
  MASK_OPACITY_RANGE,
  useImageEditor,
} from '@/hooks/useImageEditor'
import {
  IMAGE_BACKGROUNDS,
  IMAGE_QUALITIES,
  isUnsupportedImageValue,
  useImageOutputCapability,
} from '@/lib/imageOptions'
import {
  type ImageEditDraft,
  loadImageEditDraft,
  saveImageEditDraft,
  toApiImage,
  uploadName,
  uploadSessionImage,
} from '@/lib/maskApi'
import { cn } from '@/lib/utils'

/** Up to 14 references: source (1) + annotated (1) + references (14) = the API's 16. */
export const MAX_REFERENCE_IMAGES = 14
/** Images per edit (PRP-0187 Q3): 1 by default, up to 10. */
const MAX_COUNT = 10
const DRAFT_SAVE_DELAY_MS = 1000

// The last mask colour / opacity this browser used, as the starting point of a FRESH edit.
// A per-viewer convenience only: an image's own choice lives in its draft (CTR-0222).
const MASK_STYLE_KEY = 'chatwalaau.imageEditor.maskStyle'

function readMaskStyle(): { color: string; opacity: number } {
  try {
    const raw = JSON.parse(localStorage.getItem(MASK_STYLE_KEY) ?? 'null') as { color?: unknown; opacity?: unknown }
    return {
      color: isHexColor(raw?.color) ? raw.color : DEFAULT_MASK_COLOR,
      opacity: typeof raw?.opacity === 'number' ? clampMaskOpacity(raw.opacity) : DEFAULT_MASK_OPACITY,
    }
  } catch {
    return { color: DEFAULT_MASK_COLOR, opacity: DEFAULT_MASK_OPACITY }
  }
}

function writeMaskStyle(color: string, opacity: number): void {
  try {
    localStorage.setItem(MASK_STYLE_KEY, JSON.stringify({ color, opacity }))
  } catch {
    // storage unavailable (private window, blocked site data): the draft still keeps it
  }
}

export interface ImageReference {
  uri: string
  filename: string
}

/** Everything Generate hands the chat panel, which runs the direct turn (CTR-0053 v2). */
export interface ImageEditSubmission {
  sourceUrl: string
  sourceName: string
  /** Set when the source had to be resampled to the size rule: upload it as the source. */
  sourceBlob: Blob | null
  maskBlob: Blob | null
  previewBlob: Blob | null
  annotatedBlob: Blob | null
  references: ImageReference[]
  change: string
  preserve: string
  annotations: Array<{ label: string; note: string }>
  quality: string | null
  background: string | null
  n: number
}

interface MaskEditorDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  imageUrl: string
  threadId: string
  onGenerate: (submission: ImageEditSubmission) => void
  onNotify?: (message: string) => void
}

const BRUSH_SIZES: Array<{ size: BrushSize; label: string }> = [
  { size: 10, label: 'S' },
  { size: 25, label: 'M' },
  { size: 50, label: 'L' },
]

/**
 * Canvas Image Editor (CTR-0052 v2, FEAT-0013, PRP-0187 / UDR-0169 D10-D14).
 *
 * Changes an existing image partly (Mask tab: the area that may change) or wholly,
 * with labelled regions (Annotate tab: A, B, ... each with its own note) and up to 14
 * reference images. Both layers are used together. Generate calls the Images API
 * directly through the chat panel; the editor state is saved as a draft of THIS image,
 * so pressing Edit on it again restores everything.
 */
export function MaskEditorDialog({
  open,
  onOpenChange,
  imageUrl,
  threadId,
  onGenerate,
  onNotify,
}: MaskEditorDialogProps) {
  const editor = useImageEditor()
  const {
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
    maskColor,
    setMaskColor,
    maskOpacity,
    setMaskOpacity,
  } = editor

  const capability = useImageOutputCapability(open)
  const [change, setChange] = useState('')
  const [preserve, setPreserve] = useState('')
  const [references, setReferences] = useState<ImageReference[]>([])
  const [quality, setQuality] = useState('')
  const [background, setBackground] = useState('')
  const [count, setCount] = useState(1)
  const [uploading, setUploading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [draftLoaded, setDraftLoaded] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const sourceName = uploadName(imageUrl)

  // Open: restore this image's draft if it has one, else start fresh (A3).
  useEffect(() => {
    if (!open || !imageUrl) return
    let alive = true
    setDraftLoaded(false)
    setLoadError(null)
    dirtyRef.current = false
    skipNextSaveRef.current = true
    const timer = setTimeout(async () => {
      const draft = await loadImageEditDraft(threadId, sourceName)
      if (!alive) return
      setChange(draft?.change ?? '')
      setPreserve(draft?.preserve ?? '')
      setReferences((draft?.references ?? []).map((uri) => ({ uri, filename: uploadName(uri) })))
      setQuality(draft?.quality ?? '')
      setBackground(draft?.background ?? '')
      setCount(draft?.n ?? 1)
      if (draft?.active_tab) setTab(draft.active_tab)
      if (draft?.brush_size === 10 || draft?.brush_size === 25 || draft?.brush_size === 50) {
        setBrushSize(draft.brush_size)
      }
      // Set BEFORE load so a restored mask is painted in its own colour.
      const lastStyle = readMaskStyle()
      setMaskColor(isHexColor(draft?.mask_color) ? draft.mask_color : lastStyle.color)
      setMaskOpacity(typeof draft?.mask_opacity === 'number' ? draft.mask_opacity : lastStyle.opacity)
      // A non-PNG/JPEG source (e.g. WebP) is re-encoded like a non-conforming size.
      const forceNormalize = !/\.(png|jpe?g)$/i.test(sourceName)
      try {
        await load(imageUrl, { maskPng: draft?.mask_png, annotations: draft?.annotations }, forceNormalize)
        if (alive) setDraftLoaded(true)
      } catch (err) {
        if (alive) setLoadError(err instanceof Error ? err.message : 'Failed to load image')
      }
    }, 50)
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [open, imageUrl, threadId, sourceName, load, setTab, setBrushSize, setMaskColor, setMaskOpacity])

  const buildDraft = useCallback(
    (): ImageEditDraft => ({
      version: 1,
      source: imageUrl,
      width: source?.width ?? 0,
      height: source?.height ?? 0,
      mask_png: editor.maskDataUrl(),
      annotations,
      references: references.map((r) => r.uri),
      change,
      preserve,
      active_tab: tab,
      brush_size: brushSize,
      mask_color: maskColor,
      mask_opacity: maskOpacity,
      quality,
      background,
      n: count,
    }),
    [
      imageUrl,
      source,
      editor,
      annotations,
      references,
      change,
      preserve,
      tab,
      brushSize,
      maskColor,
      maskOpacity,
      quality,
      background,
      count,
    ],
  )

  // Autosave (debounced) once the draft has been restored, so a restore is never
  // overwritten by the empty state that precedes it.
  const buildDraftRef = useRef(buildDraft)
  buildDraftRef.current = buildDraft
  const pendingRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Only an image the user actually worked on gets a draft: merely opening a result
  // and closing it must leave it "fresh" for the next Edit (A3).
  const dirtyRef = useRef(false)
  const skipNextSaveRef = useRef(true)
  const flushDraft = useCallback(async () => {
    if (pendingRef.current) {
      clearTimeout(pendingRef.current)
      pendingRef.current = null
    }
    if (!draftLoaded || !dirtyRef.current) return
    await saveImageEditDraft(threadId, sourceName, buildDraftRef.current())
  }, [draftLoaded, threadId, sourceName])

  // biome-ignore lint/correctness/useExhaustiveDependencies: every listed value is a draft field whose change must schedule a save
  useEffect(() => {
    if (!open || !draftLoaded) return
    // The first run after a load is the restored state itself, not an edit.
    if (skipNextSaveRef.current) {
      skipNextSaveRef.current = false
      return
    }
    dirtyRef.current = true
    if (pendingRef.current) clearTimeout(pendingRef.current)
    pendingRef.current = setTimeout(() => {
      pendingRef.current = null
      void saveImageEditDraft(threadId, sourceName, buildDraftRef.current())
    }, DRAFT_SAVE_DELAY_MS)
  }, [
    open,
    draftLoaded,
    revision,
    change,
    preserve,
    references,
    quality,
    background,
    count,
    tab,
    brushSize,
    maskColor,
    maskOpacity,
  ])

  // Remember the style for this browser's next fresh edit (not before a restore).
  useEffect(() => {
    if (draftLoaded) writeMaskStyle(maskColor, maskOpacity)
  }, [draftLoaded, maskColor, maskOpacity])

  const close = useCallback(() => {
    void flushDraft()
    onOpenChange(false)
  }, [flushDraft, onOpenChange])

  const addReferences = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      const room = MAX_REFERENCE_IMAGES - references.length
      const picked = Array.from(files).slice(0, Math.max(0, room))
      if (files.length > room) onNotify?.(`At most ${MAX_REFERENCE_IMAGES} reference images can be added.`)
      setUploading(true)
      try {
        const added: ImageReference[] = []
        for (const file of picked) {
          const blob = await toApiImage(file)
          const ext = blob.type === 'image/jpeg' ? 'jpg' : 'png'
          added.push(await uploadSessionImage(threadId, blob, `imgedit_ref_${crypto.randomUUID().slice(0, 12)}.${ext}`))
        }
        setReferences((prev) => [...prev, ...added].slice(0, MAX_REFERENCE_IMAGES))
      } catch (err) {
        onNotify?.(err instanceof Error ? err.message : 'Failed to add a reference image')
      } finally {
        setUploading(false)
        if (fileInputRef.current) fileInputRef.current.value = ''
      }
    },
    [references.length, threadId, onNotify],
  )

  const canGenerate = canvasReady && !uploading && change.trim().length > 0

  const handleGenerate = useCallback(async () => {
    if (!canGenerate || !source) return
    try {
      const hasAnnotations = annotations.length > 0
      const [sourceBlob, maskBlob, previewBlob, annotatedBlob] = await Promise.all([
        source.normalized ? editor.exportSource() : Promise.resolve(null),
        maskEmpty ? Promise.resolve(null) : editor.exportMask(),
        maskEmpty ? Promise.resolve(null) : editor.exportPreview(),
        hasAnnotations ? editor.exportAnnotated() : Promise.resolve(null),
      ])
      await flushDraft()
      onOpenChange(false)
      onGenerate({
        sourceUrl: imageUrl,
        sourceName,
        sourceBlob,
        maskBlob,
        previewBlob,
        annotatedBlob,
        references,
        change: change.trim(),
        preserve: preserve.trim(),
        annotations: annotations.map((a) => ({ label: a.label, note: a.note.trim() })),
        quality: quality || null,
        background: background || null,
        n: count,
      })
    } catch {
      onNotify?.('The edit could not be prepared. Please try again.')
    }
  }, [
    canGenerate,
    source,
    annotations,
    maskEmpty,
    editor,
    flushDraft,
    onOpenChange,
    onGenerate,
    imageUrl,
    sourceName,
    references,
    change,
    preserve,
    quality,
    background,
    count,
    onNotify,
  ])

  // Enter = newline; Ctrl/Cmd+Enter = Generate. Never while an IME composition is open.
  const onPromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      void handleGenerate()
    }
  }

  const defaults = capability?.effective_defaults ?? capability?.defaults ?? {}
  const canvasStyle: React.CSSProperties = { maxHeight: 'calc(90vh - 170px)', maxWidth: '100%' }
  const textareaClass =
    'w-full resize-y rounded-md border bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring'
  const selectClass =
    'h-8 w-full rounded-md border bg-background px-2 text-xs focus:outline-none focus:ring-1 focus:ring-ring'

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      {/* The same size as the App Settings dialog (PRP-0187 / UDR-0169 D12). */}
      <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle>Edit image</DialogTitle>
          <DialogDescription>
            Change part or all of this image. Paint a mask for the area that may change, mark regions A, B, ... to refer
            to them, and add reference images.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
          {/* Canvas + tools */}
          <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <div className="inline-flex rounded-md border p-0.5" role="tablist" aria-label="Editing layer">
                {(['mask', 'annotate'] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    role="tab"
                    aria-selected={tab === t}
                    className={cn(
                      'rounded px-3 py-1 text-xs font-medium',
                      tab === t ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted',
                    )}
                    onClick={() => setTab(t)}>
                    {t === 'mask' ? 'Mask' : `Annotate${annotations.length ? ` (${annotations.length})` : ''}`}
                  </button>
                ))}
              </div>
              <div className="mx-1 h-4 w-px bg-border" />
              {tab === 'mask' ? (
                <>
                  {BRUSH_SIZES.map((b) => (
                    <Button
                      key={b.size}
                      variant={maskTool === 'brush' && brushSize === b.size ? 'default' : 'outline'}
                      size="sm"
                      className="h-7 w-7 p-0 text-xs"
                      aria-label={`Brush ${b.label}`}
                      onClick={() => {
                        setMaskTool('brush')
                        setBrushSize(b.size)
                      }}>
                      {b.label}
                    </Button>
                  ))}
                  <Button
                    variant={maskTool === 'eraser' ? 'default' : 'outline'}
                    size="sm"
                    className="h-7 gap-1 px-2 text-xs"
                    onClick={() => setMaskTool('eraser')}>
                    <Eraser className="h-3 w-3" />
                    Eraser
                  </Button>
                  <div className="mx-1 h-4 w-px bg-border" />
                  {/* Display colour + opacity of the mask (PRP-0187 amendment). Only what
                      you SEE changes; the mask sent is the painted area, whatever its colour. */}
                  <fieldset className="m-0 flex items-center gap-1 border-0 p-0" aria-label="Mask colour">
                    {MASK_COLOR_PRESETS.map((c) => (
                      <button
                        key={c}
                        type="button"
                        className={cn(
                          'h-5 w-5 rounded-full border border-border shadow-xs',
                          maskColor.toLowerCase() === c && 'ring-2 ring-ring ring-offset-1 ring-offset-background',
                        )}
                        style={{ backgroundColor: c }}
                        aria-label={`Mask colour ${c}`}
                        aria-pressed={maskColor.toLowerCase() === c}
                        title={c}
                        onClick={() => setMaskColor(c)}
                      />
                    ))}
                    <input
                      type="color"
                      className="h-6 w-7 cursor-pointer rounded border bg-transparent p-0"
                      value={maskColor}
                      aria-label="Custom mask colour"
                      title="Custom colour"
                      onChange={(e) => setMaskColor(e.target.value)}
                    />
                  </fieldset>
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    Opacity
                    <input
                      type="range"
                      className="w-24 accent-primary"
                      min={MASK_OPACITY_RANGE.min}
                      max={MASK_OPACITY_RANGE.max}
                      step={MASK_OPACITY_RANGE.step}
                      value={maskOpacity}
                      aria-label="Mask opacity"
                      onChange={(e) => setMaskOpacity(Number(e.target.value))}
                    />
                    <span className="w-8 tabular-nums">{Math.round(maskOpacity * 100)}%</span>
                  </label>
                </>
              ) : (
                <>
                  <Button
                    variant={annotateTool === 'rect' ? 'default' : 'outline'}
                    size="sm"
                    className="h-7 gap-1 px-2 text-xs"
                    onClick={() => setAnnotateTool('rect')}>
                    <Square className="h-3 w-3" />
                    Rectangle
                  </Button>
                  <Button
                    variant={annotateTool === 'freehand' ? 'default' : 'outline'}
                    size="sm"
                    className="h-7 gap-1 px-2 text-xs"
                    onClick={() => setAnnotateTool('freehand')}>
                    <Highlighter className="h-3 w-3" />
                    Freehand
                  </Button>
                </>
              )}
              <div className="mx-1 h-4 w-px bg-border" />
              <Button variant="outline" size="sm" className="h-7 w-7 p-0" onClick={undo} aria-label="Undo">
                <Undo2 className="h-3 w-3" />
              </Button>
              <Button variant="outline" size="sm" className="h-7 w-7 p-0" onClick={redo} aria-label="Redo">
                <Redo2 className="h-3 w-3" />
              </Button>
              <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs" onClick={clearTab}>
                <Trash2 className="h-3 w-3" />
                {tab === 'mask' ? 'Clear mask' : 'Clear marks'}
              </Button>
              {source?.normalized && (
                <span className="text-[11px] text-muted-foreground">
                  Resized to {source.width}x{source.height} to fit the image size rules.
                </span>
              )}
            </div>

            <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto rounded-lg border bg-muted/30 p-2">
              {loadError ? (
                <p className="text-sm text-destructive">{loadError}</p>
              ) : (
                <div className="relative inline-block">
                  <canvas ref={imageCanvasRef} className="block" style={canvasStyle} />
                  {/* Opaque strokes, tinted by the LAYER's opacity: overlaps stay uniform. */}
                  <canvas
                    ref={maskCanvasRef}
                    className="pointer-events-none absolute inset-0 block h-full w-full"
                    style={{ opacity: maskOpacity }}
                  />
                  <canvas
                    ref={annotationCanvasRef}
                    className="absolute inset-0 block h-full w-full"
                    style={{ cursor: 'crosshair', touchAction: 'none' }}
                    onPointerDown={handlePointerDown}
                    onPointerMove={handlePointerMove}
                    onPointerUp={handlePointerUp}
                    onPointerLeave={handlePointerUp}
                  />
                </div>
              )}
            </div>
          </div>

          {/* Instructions panel */}
          <div className="flex min-h-0 w-full flex-col gap-3 overflow-y-auto border-t p-3 lg:w-[380px] lg:border-l lg:border-t-0">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium">Change</span>
              <textarea
                className={textareaClass}
                rows={3}
                placeholder="What must be different, e.g. replace the background with a beach at sunset"
                value={change}
                onChange={(e) => setChange(e.target.value)}
                onKeyDown={onPromptKeyDown}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium">Preserve</span>
              <textarea
                className={textareaClass}
                rows={2}
                placeholder="What must stay exactly as it is, e.g. the product's shape, logo and colors"
                value={preserve}
                onChange={(e) => setPreserve(e.target.value)}
                onKeyDown={onPromptKeyDown}
              />
            </label>

            <div className="flex flex-col gap-1">
              <span className="flex items-center gap-1 text-xs font-medium">
                <SquareDashedMousePointer className="h-3 w-3" />
                Marked regions
              </span>
              {annotations.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">
                  Use the Annotate tab to mark a region; each mark adds a note here.
                </p>
              ) : (
                annotations.map((a) => (
                  <div key={a.id} className="flex items-center gap-1.5">
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-primary text-xs font-bold text-primary-foreground">
                      {a.label}
                    </span>
                    <input
                      className="h-7 min-w-0 flex-1 rounded-md border bg-background px-2 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                      placeholder={`What to do with ${a.label}`}
                      value={a.note}
                      onChange={(e) => setNote(a.id, e.target.value)}
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 w-6 p-0"
                      aria-label={`Remove mark ${a.label}`}
                      onClick={() => removeAnnotation(a.id)}>
                      <X className="h-3 w-3" />
                    </Button>
                  </div>
                ))
              )}
            </div>

            <div className="flex flex-col gap-1">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium">
                  References ({references.length}/{MAX_REFERENCE_IMAGES})
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 gap-1 px-2 text-xs"
                  disabled={uploading || references.length >= MAX_REFERENCE_IMAGES}
                  onClick={() => fileInputRef.current?.click()}>
                  <ImagePlus className="h-3 w-3" />
                  {uploading ? 'Adding...' : 'Add'}
                </Button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  multiple
                  className="hidden"
                  onChange={(e) => void addReferences(e.target.files)}
                />
              </div>
              {references.length > 0 && (
                <div className="grid grid-cols-4 gap-1.5">
                  {references.map((ref, i) => (
                    <div key={ref.uri} className="group/ref relative">
                      <AuthedImage
                        uri={ref.uri}
                        alt={`Ref ${i + 1}`}
                        className="aspect-square w-full rounded border object-cover"
                      />
                      <span className="absolute left-0.5 top-0.5 rounded bg-black/70 px-1 text-[10px] font-medium text-white">
                        Ref {i + 1}
                      </span>
                      <button
                        type="button"
                        className="absolute right-0.5 top-0.5 rounded bg-black/70 p-0.5 text-white opacity-80 hover:opacity-100"
                        aria-label={`Remove Ref ${i + 1}`}
                        onClick={() => setReferences((prev) => prev.filter((r) => r.uri !== ref.uri))}>
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              {references.length > 0 && (
                <p className="text-[11px] text-muted-foreground">Refer to them as Ref 1, Ref 2, ... in Change.</p>
              )}
            </div>

            {/* Per-edit options (PRP-0187 Q1/Q3). Size follows the source's shape. */}
            <div className="grid grid-cols-3 gap-2">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium text-muted-foreground">Quality</span>
                <select className={selectClass} value={quality} onChange={(e) => setQuality(e.target.value)}>
                  <option value="">Default ({defaults.quality ?? 'xhigh'})</option>
                  {IMAGE_QUALITIES.map((q) => (
                    <option key={q} value={q} disabled={isUnsupportedImageValue(capability, 'quality', q)}>
                      {q}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium text-muted-foreground">Background</span>
                <select className={selectClass} value={background} onChange={(e) => setBackground(e.target.value)}>
                  <option value="">Default ({defaults.background ?? 'opaque'})</option>
                  {IMAGE_BACKGROUNDS.map((b) => (
                    <option key={b} value={b} disabled={isUnsupportedImageValue(capability, 'background', b)}>
                      {b}
                      {isUnsupportedImageValue(capability, 'background', b) ? ' (not supported)' : ''}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium text-muted-foreground">Images</span>
                <select className={selectClass} value={count} onChange={(e) => setCount(Number(e.target.value))}>
                  {Array.from({ length: MAX_COUNT }, (_, i) => i + 1).map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="text-[11px] text-muted-foreground">Output is PNG and keeps the source's shape.</p>

            <div className="mt-auto flex items-center justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={close}>
                Cancel
              </Button>
              <Button onClick={() => void handleGenerate()} disabled={!canGenerate} title="Ctrl+Enter">
                <Paintbrush className="mr-2 h-4 w-4" />
                Generate
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
