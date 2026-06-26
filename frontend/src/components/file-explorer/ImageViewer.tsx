import { Maximize2, Minus, Plus, RotateCcw } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'

/**
 * Image preview pane (CTR-0137, PRP-0093, UDR-0071 D5).
 *
 * Renders an image (from a CTR-0136 /raw blob URL) with dependency-free zoom and pan:
 * CSS-transform scale + translate, mouse-wheel zoom, drag-to-pan, and Zoom in / out /
 * fit / reset controls. Media is view-only (no editing), consistent with the binary
 * read-only posture (UDR-0069 D6).
 */

const MIN_SCALE = 0.1
const MAX_SCALE = 8
const clampScale = (s: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s))

interface ImageViewerProps {
  url: string
  name: string
}

export function ImageViewer({ url, name }: ImageViewerProps) {
  const [scale, setScale] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const dragRef = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null)

  const zoomBy = useCallback((factor: number) => setScale((s) => clampScale(s * factor)), [])
  const reset = useCallback(() => {
    setScale(1)
    setOffset({ x: 0, y: 0 })
  }, [])

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) < 1) return
    e.preventDefault()
    setScale((s) => clampScale(s * (e.deltaY < 0 ? 1.1 : 1 / 1.1)))
  }, [])

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      dragRef.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y }
      ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    },
    [offset],
  )
  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d) return
    setOffset({ x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) })
  }, [])
  const onPointerUp = useCallback((e: React.PointerEvent) => {
    dragRef.current = null
    ;(e.target as HTMLElement).releasePointerCapture?.(e.pointerId)
  }, [])

  return (
    <div className="flex h-full flex-col bg-zinc-100">
      <div className="flex shrink-0 items-center gap-1 border-b bg-white px-2 py-1">
        <span className="mr-auto truncate text-[12px] text-zinc-500">{name}</span>
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
        <Button variant="ghost" size="icon" className="h-6 w-6 text-zinc-500" title="Reset" onClick={reset}>
          <RotateCcw className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-zinc-500"
          title="Fit (100%)"
          onClick={() => setScale(1)}>
          <Maximize2 className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div
        className="relative min-h-0 flex-1 cursor-grab overflow-hidden active:cursor-grabbing"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        style={{
          backgroundImage:
            'linear-gradient(45deg,#e5e7eb 25%,transparent 25%),linear-gradient(-45deg,#e5e7eb 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e5e7eb 75%),linear-gradient(-45deg,transparent 75%,#e5e7eb 75%)',
          backgroundSize: '16px 16px',
          backgroundPosition: '0 0,0 8px,8px -8px,-8px 0',
        }}>
        <img
          src={url}
          alt={name}
          draggable={false}
          className="absolute left-1/2 top-1/2 max-w-none select-none"
          style={{
            transform: `translate(-50%,-50%) translate(${offset.x}px,${offset.y}px) scale(${scale})`,
            transformOrigin: 'center',
          }}
        />
      </div>
    </div>
  )
}
