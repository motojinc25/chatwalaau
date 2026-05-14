import { useCallback, useRef, useState } from 'react'

type BrushSize = 10 | 25 | 50
type Tool = 'brush' | 'eraser'

const MASK_COLOR = 'rgba(59, 130, 246, 0.4)'
const STROKE_COLOR = 'white'
const MAX_HISTORY = 30

export function useMaskEditor() {
  const imageCanvasRef = useRef<HTMLCanvasElement>(null)
  const maskCanvasRef = useRef<HTMLCanvasElement>(null)
  const [brushSize, setBrushSize] = useState<BrushSize>(25)
  const [tool, setTool] = useState<Tool>('brush')
  const [isDrawing, setIsDrawing] = useState(false)
  const [canvasReady, setCanvasReady] = useState(false)

  // Undo/redo history
  const historyRef = useRef<ImageData[]>([])
  const historyIndexRef = useRef(-1)

  // Internal mask canvas (white strokes on black for export)
  const internalMaskRef = useRef<HTMLCanvasElement | null>(null)

  const saveSnapshot = useCallback(() => {
    const maskCanvas = maskCanvasRef.current
    if (!maskCanvas) return
    const ctx = maskCanvas.getContext('2d')
    if (!ctx) return
    const snapshot = ctx.getImageData(0, 0, maskCanvas.width, maskCanvas.height)
    const idx = historyIndexRef.current + 1
    historyRef.current = historyRef.current.slice(0, idx)
    historyRef.current.push(snapshot)
    if (historyRef.current.length > MAX_HISTORY) {
      historyRef.current.shift()
    } else {
      historyIndexRef.current = idx
    }
  }, [])

  const initCanvas = useCallback(
    (imageUrl: string) => {
      const img = new Image()
      img.crossOrigin = 'anonymous'
      img.onload = () => {
        const imageCanvas = imageCanvasRef.current
        const maskCanvas = maskCanvasRef.current
        if (!imageCanvas || !maskCanvas) return

        imageCanvas.width = img.naturalWidth
        imageCanvas.height = img.naturalHeight
        maskCanvas.width = img.naturalWidth
        maskCanvas.height = img.naturalHeight

        const imgCtx = imageCanvas.getContext('2d')
        imgCtx?.drawImage(img, 0, 0)

        const maskCtx = maskCanvas.getContext('2d')
        maskCtx?.clearRect(0, 0, maskCanvas.width, maskCanvas.height)

        // Internal mask canvas
        const internal = document.createElement('canvas')
        internal.width = img.naturalWidth
        internal.height = img.naturalHeight
        const intCtx = internal.getContext('2d')
        if (intCtx) {
          intCtx.fillStyle = 'black'
          intCtx.fillRect(0, 0, internal.width, internal.height)
        }
        internalMaskRef.current = internal

        historyRef.current = []
        historyIndexRef.current = -1
        saveSnapshot()
        setCanvasReady(true)
      }
      img.src = imageUrl
    },
    [saveSnapshot],
  )

  const getCanvasPoint = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = maskCanvasRef.current
    if (!canvas) return null
    const rect = canvas.getBoundingClientRect()
    const scaleX = canvas.width / rect.width
    const scaleY = canvas.height / rect.height
    return { x: (e.clientX - rect.left) * scaleX, y: (e.clientY - rect.top) * scaleY }
  }, [])

  const drawAt = useCallback(
    (x: number, y: number) => {
      const maskCanvas = maskCanvasRef.current
      const internal = internalMaskRef.current
      if (!maskCanvas || !internal) return
      const maskCtx = maskCanvas.getContext('2d')
      const intCtx = internal.getContext('2d')
      if (!maskCtx || !intCtx) return

      if (tool === 'eraser') {
        maskCtx.globalCompositeOperation = 'destination-out'
        intCtx.globalCompositeOperation = 'destination-out'
      } else {
        maskCtx.globalCompositeOperation = 'source-over'
        intCtx.globalCompositeOperation = 'source-over'
      }

      // Visual mask (blue semi-transparent)
      maskCtx.beginPath()
      maskCtx.arc(x, y, brushSize, 0, Math.PI * 2)
      maskCtx.fillStyle = tool === 'eraser' ? 'rgba(0,0,0,1)' : MASK_COLOR
      maskCtx.fill()

      // Internal mask (white on black)
      intCtx.beginPath()
      intCtx.arc(x, y, brushSize, 0, Math.PI * 2)
      intCtx.fillStyle = tool === 'eraser' ? 'rgba(0,0,0,1)' : STROKE_COLOR
      intCtx.fill()
    },
    [brushSize, tool],
  )

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      const pt = getCanvasPoint(e)
      if (!pt) return
      setIsDrawing(true)
      drawAt(pt.x, pt.y)
      ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    },
    [getCanvasPoint, drawAt],
  )

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (!isDrawing) return
      const pt = getCanvasPoint(e)
      if (pt) drawAt(pt.x, pt.y)
    },
    [isDrawing, getCanvasPoint, drawAt],
  )

  const handlePointerUp = useCallback(() => {
    if (!isDrawing) return
    setIsDrawing(false)
    saveSnapshot()

    // Sync internal mask
    const maskCanvas = maskCanvasRef.current
    const internal = internalMaskRef.current
    if (maskCanvas && internal) {
      const intCtx = internal.getContext('2d')
      if (intCtx) {
        intCtx.globalCompositeOperation = 'source-over'
        intCtx.fillStyle = 'black'
        intCtx.fillRect(0, 0, internal.width, internal.height)
        intCtx.globalCompositeOperation = 'destination-over'

        // Rebuild internal from visual mask
        const maskCtx = maskCanvas.getContext('2d')
        if (maskCtx) {
          const data = maskCtx.getImageData(0, 0, maskCanvas.width, maskCanvas.height)
          intCtx.globalCompositeOperation = 'source-over'
          intCtx.fillStyle = 'black'
          intCtx.fillRect(0, 0, internal.width, internal.height)
          // Where visual mask has content -> white on internal
          for (let i = 3; i < data.data.length; i += 4) {
            if (data.data[i] > 10) {
              data.data[i - 3] = 255
              data.data[i - 2] = 255
              data.data[i - 1] = 255
              data.data[i] = 255
            } else {
              data.data[i - 3] = 0
              data.data[i - 2] = 0
              data.data[i - 1] = 0
              data.data[i] = 255
            }
          }
          intCtx.putImageData(data, 0, 0)
        }
      }
    }
  }, [isDrawing, saveSnapshot])

  const undo = useCallback(() => {
    if (historyIndexRef.current <= 0) return
    historyIndexRef.current--
    const maskCanvas = maskCanvasRef.current
    if (!maskCanvas) return
    const ctx = maskCanvas.getContext('2d')
    if (!ctx) return
    ctx.putImageData(historyRef.current[historyIndexRef.current], 0, 0)
  }, [])

  const redo = useCallback(() => {
    if (historyIndexRef.current >= historyRef.current.length - 1) return
    historyIndexRef.current++
    const maskCanvas = maskCanvasRef.current
    if (!maskCanvas) return
    const ctx = maskCanvas.getContext('2d')
    if (!ctx) return
    ctx.putImageData(historyRef.current[historyIndexRef.current], 0, 0)
  }, [])

  const clear = useCallback(() => {
    const maskCanvas = maskCanvasRef.current
    if (!maskCanvas) return
    const ctx = maskCanvas.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, maskCanvas.width, maskCanvas.height)
    saveSnapshot()
  }, [saveSnapshot])

  const exportCompositedImage = useCallback(async (): Promise<Blob> => {
    const imageCanvas = imageCanvasRef.current
    const maskCanvas = maskCanvasRef.current
    if (!imageCanvas || !maskCanvas) throw new Error('Canvas not ready')

    // Create composited image: source image with painted areas made transparent.
    // Azure OpenAI /images/edits treats transparent pixels as areas to regenerate.
    const exportCanvas = document.createElement('canvas')
    exportCanvas.width = imageCanvas.width
    exportCanvas.height = imageCanvas.height
    const ctx = exportCanvas.getContext('2d')
    if (!ctx) throw new Error('Cannot create export context')

    // Draw source image
    ctx.drawImage(imageCanvas, 0, 0)

    // Read composited image pixels
    const imgData = ctx.getImageData(0, 0, exportCanvas.width, exportCanvas.height)

    // Read mask pixels
    const maskCtx = maskCanvas.getContext('2d')
    if (!maskCtx) throw new Error('Cannot read mask')
    const maskData = maskCtx.getImageData(0, 0, maskCanvas.width, maskCanvas.height)

    // Where mask has content (user painted), set source image alpha to 0
    for (let i = 0; i < maskData.data.length; i += 4) {
      if (maskData.data[i + 3] > 10) {
        imgData.data[i + 3] = 0 // Make transparent -> edit region
      }
    }
    ctx.putImageData(imgData, 0, 0)

    return new Promise<Blob>((resolve, reject) => {
      exportCanvas.toBlob((blob) => {
        if (blob) resolve(blob)
        else reject(new Error('Failed to export composited image'))
      }, 'image/png')
    })
  }, [])

  const exportPreview = useCallback(async (): Promise<Blob> => {
    const imageCanvas = imageCanvasRef.current
    const maskCanvas = maskCanvasRef.current
    if (!imageCanvas || !maskCanvas) throw new Error('Canvas not ready')

    // Create a visual preview: original image with blue mask overlay visible
    const canvas = document.createElement('canvas')
    canvas.width = imageCanvas.width
    canvas.height = imageCanvas.height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('Cannot create preview context')

    ctx.drawImage(imageCanvas, 0, 0)
    ctx.drawImage(maskCanvas, 0, 0)

    return new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) resolve(blob)
        else reject(new Error('Failed to export preview'))
      }, 'image/png')
    })
  }, [])

  return {
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
  }
}
