import { ChevronLeft, ChevronRight, Loader2, Minus, Plus, RotateCcw } from 'lucide-react'
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist'
import * as pdfjsLib from 'pdfjs-dist'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import '@/lib/pdf-setup'

/**
 * PDF preview pane (CTR-0137, PRP-0093, UDR-0071 D4).
 *
 * Renders a PDF (from a CTR-0136 /raw blob URL) with pdf.js to a canvas, with a custom
 * in-app zoom in / out UI and page navigation. The pdf.js worker is self-hosted/bundled
 * (no CDN; see @/lib/pdf-setup), matching the monaco posture (UDR-0069 D5). View-only.
 */

const MIN_SCALE = 0.25
const MAX_SCALE = 5
const clampScale = (s: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s))

interface PdfViewerProps {
  url: string
  name: string
}

export function PdfViewer({ url, name }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const docRef = useRef<PDFDocumentProxy | null>(null)
  const renderTaskRef = useRef<RenderTask | null>(null)

  const [numPages, setNumPages] = useState(0)
  const [page, setPage] = useState(1)
  const [scale, setScale] = useState(1.2)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Load the document once per URL.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    const task = pdfjsLib.getDocument({ url })
    task.promise.then(
      (pdf) => {
        if (cancelled) return
        docRef.current = pdf
        setNumPages(pdf.numPages)
        setPage(1)
        setLoading(false)
      },
      () => {
        if (!cancelled) {
          setError('Failed to load PDF.')
          setLoading(false)
        }
      },
    )
    return () => {
      cancelled = true
      renderTaskRef.current?.cancel()
      // Destroying the loading task tears down its document + worker resources.
      void task.destroy()
      docRef.current = null
    }
  }, [url])

  // Render the current page whenever page or scale changes.
  useEffect(() => {
    const pdf = docRef.current
    const canvas = canvasRef.current
    if (!pdf || !canvas || loading) return
    let cancelled = false
    pdf.getPage(page).then((pdfPage) => {
      if (cancelled) return
      const dpr = window.devicePixelRatio || 1
      const viewport = pdfPage.getViewport({ scale })
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      canvas.width = Math.floor(viewport.width * dpr)
      canvas.height = Math.floor(viewport.height * dpr)
      canvas.style.width = `${Math.floor(viewport.width)}px`
      canvas.style.height = `${Math.floor(viewport.height)}px`
      renderTaskRef.current?.cancel()
      const task = pdfPage.render({
        canvas,
        canvasContext: ctx,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
      })
      renderTaskRef.current = task
      task.promise.catch(() => {
        /* cancelled or render error: ignore (a newer render supersedes) */
      })
    })
    return () => {
      cancelled = true
    }
  }, [page, scale, loading])

  const zoomBy = useCallback((factor: number) => setScale((s) => clampScale(s * factor)), [])
  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return
    e.preventDefault()
    setScale((s) => clampScale(s * (e.deltaY < 0 ? 1.1 : 1 / 1.1)))
  }, [])

  return (
    <div className="flex h-full flex-col bg-zinc-100">
      <div className="flex shrink-0 items-center gap-1 border-b bg-white px-2 py-1">
        <span className="mr-2 max-w-[10rem] truncate text-[12px] text-zinc-500">{name}</span>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-zinc-500"
          title="Previous page"
          disabled={page <= 1}
          onClick={() => setPage((p) => Math.max(1, p - 1))}>
          <ChevronLeft className="h-3.5 w-3.5" />
        </Button>
        <span className="text-[11px] tabular-nums text-zinc-500">{numPages ? `${page} / ${numPages}` : '-'}</span>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-zinc-500"
          title="Next page"
          disabled={page >= numPages}
          onClick={() => setPage((p) => Math.min(numPages, p + 1))}>
          <ChevronRight className="h-3.5 w-3.5" />
        </Button>
        <div className="mx-1 h-4 w-px bg-zinc-200" />
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-zinc-500"
          title="Zoom out"
          onClick={() => zoomBy(1 / 1.2)}>
          <Minus className="h-3.5 w-3.5" />
        </Button>
        <span className="w-12 text-center text-[11px] tabular-nums text-zinc-500">{Math.round(scale * 100)}%</span>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-zinc-500"
          title="Zoom in"
          onClick={() => zoomBy(1.2)}>
          <Plus className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="ml-auto h-6 w-6 text-zinc-500"
          title="Reset zoom"
          onClick={() => setScale(1.2)}>
          <RotateCcw className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4 text-center" onWheel={onWheel}>
        {loading ? (
          <div className="flex h-full items-center justify-center gap-2 text-sm text-zinc-500">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading {name}...
          </div>
        ) : error ? (
          <div className="flex h-full items-center justify-center text-sm text-red-600">{error}</div>
        ) : (
          <canvas ref={canvasRef} className="inline-block bg-white shadow" />
        )}
      </div>
    </div>
  )
}
