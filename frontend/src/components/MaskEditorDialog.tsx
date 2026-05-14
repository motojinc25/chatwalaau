import { Eraser, Paintbrush, Redo2, Trash2, Undo2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useMaskEditor } from '@/hooks/useMaskEditor'

interface MaskEditorDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  imageUrl: string
  onGenerate: (compositedBlob: Blob, previewBlob: Blob, prompt: string) => void
}

export function MaskEditorDialog({ open, onOpenChange, imageUrl, onGenerate }: MaskEditorDialogProps) {
  const {
    imageCanvasRef,
    maskCanvasRef,
    brushSize,
    setBrushSize,
    tool,
    setTool,
    canvasReady,
    initCanvas,
    handlePointerDown,
    handlePointerMove,
    handlePointerUp,
    undo,
    redo,
    clear,
    exportCompositedImage,
    exportPreview,
  } = useMaskEditor()

  const [prompt, setPrompt] = useState('')

  useEffect(() => {
    if (open && imageUrl) {
      setPrompt('')
      const t = setTimeout(() => initCanvas(imageUrl), 100)
      return () => clearTimeout(t)
    }
  }, [open, imageUrl, initCanvas])

  const handleGenerate = useCallback(async () => {
    if (!prompt.trim()) return
    try {
      const compositedBlob = await exportCompositedImage()
      const previewBlob = await exportPreview()
      onOpenChange(false)
      onGenerate(compositedBlob, previewBlob, prompt.trim())
    } catch {
      // export failed -- keep dialog open
    }
  }, [prompt, exportCompositedImage, exportPreview, onOpenChange, onGenerate])

  const brushSizes = [
    { size: 10 as const, label: 'S' },
    { size: 25 as const, label: 'M' },
    { size: 50 as const, label: 'L' },
  ]

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] max-w-4xl flex-col">
        <DialogHeader>
          <DialogTitle>Edit Image with Mask</DialogTitle>
          <DialogDescription>Paint over the areas you want to change, then describe the edit.</DialogDescription>
        </DialogHeader>

        {/* Canvas area */}
        <div
          className="relative mx-auto w-full overflow-auto rounded-lg border bg-muted/30"
          style={{ maxHeight: '50vh' }}>
          <div className="relative inline-block">
            <canvas ref={imageCanvasRef} className="block max-w-full" style={{ maxHeight: '48vh' }} />
            <canvas
              ref={maskCanvasRef}
              className="absolute inset-0 block max-w-full"
              style={{ maxHeight: '48vh', cursor: 'crosshair', touchAction: 'none' }}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              onPointerLeave={handlePointerUp}
            />
          </div>
        </div>

        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-1 text-xs text-muted-foreground">Brush:</span>
          {brushSizes.map((b) => (
            <Button
              key={b.size}
              variant={tool === 'brush' && brushSize === b.size ? 'default' : 'outline'}
              size="sm"
              className="h-7 w-7 p-0 text-xs"
              onClick={() => {
                setTool('brush')
                setBrushSize(b.size)
              }}>
              {b.label}
            </Button>
          ))}
          <Button
            variant={tool === 'eraser' ? 'default' : 'outline'}
            size="sm"
            className="h-7 gap-1 px-2 text-xs"
            onClick={() => setTool('eraser')}>
            <Eraser className="h-3 w-3" />
            Eraser
          </Button>
          <div className="mx-1 h-4 w-px bg-border" />
          <Button variant="outline" size="sm" className="h-7 w-7 p-0" onClick={undo} aria-label="Undo">
            <Undo2 className="h-3 w-3" />
          </Button>
          <Button variant="outline" size="sm" className="h-7 w-7 p-0" onClick={redo} aria-label="Redo">
            <Redo2 className="h-3 w-3" />
          </Button>
          <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs" onClick={clear}>
            <Trash2 className="h-3 w-3" />
            Clear
          </Button>
        </div>

        {/* Prompt */}
        <Input
          placeholder="Describe what to put in the masked area..."
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              handleGenerate()
            }
          }}
        />

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleGenerate} disabled={!canvasReady || !prompt.trim()}>
            <Paintbrush className="mr-2 h-4 w-4" />
            Generate
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
