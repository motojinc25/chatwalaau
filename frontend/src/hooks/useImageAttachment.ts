import { useCallback, useState } from 'react'

export interface ImageAttachment {
  id: string
  file: File
  previewUrl: string
  uploadedUri: string | null
  mediaType: string
  status: 'uploading' | 'ready' | 'error'
  /** Paint-origin attachment (CTR-0160/CTR-0161, PRP-0099): re-editable. */
  isPaint?: boolean
  /** Server-side filename, used as the CTR-0161 scene sidecar key. */
  filename?: string
}

const IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/gif', 'image/webp'])
const PDF_TYPE = 'application/pdf'
const ALLOWED_TYPES = new Set([...IMAGE_TYPES, PDF_TYPE])
const MAX_SIZE_IMAGE = 20 * 1024 * 1024 // 20MB
const MAX_SIZE_PDF = 50 * 1024 * 1024 // 50MB

export function useImageAttachment() {
  const [attachments, setAttachments] = useState<ImageAttachment[]>([])

  const addFiles = useCallback(async (files: FileList | File[], threadId: string) => {
    const validFiles = Array.from(files).filter((f) => {
      if (!ALLOWED_TYPES.has(f.type)) return false
      const maxSize = f.type === PDF_TYPE ? MAX_SIZE_PDF : MAX_SIZE_IMAGE
      return f.size <= maxSize
    })
    if (validFiles.length === 0) return

    const newAttachments: ImageAttachment[] = validFiles.map((file) => ({
      id: crypto.randomUUID(),
      file,
      previewUrl: URL.createObjectURL(file),
      uploadedUri: null,
      mediaType: file.type,
      status: 'uploading' as const,
    }))

    setAttachments((prev) => [...prev, ...newAttachments])

    for (const attachment of newAttachments) {
      try {
        const formData = new FormData()
        formData.append('file', attachment.file)

        const res = await fetch(`/api/upload/${threadId}`, {
          method: 'POST',
          body: formData,
        })
        if (!res.ok) throw new Error(`Upload failed: ${res.status}`)

        const data = await res.json()
        setAttachments((prev) =>
          prev.map((a) =>
            a.id === attachment.id ? { ...a, uploadedUri: data.uri as string, status: 'ready' as const } : a,
          ),
        )
      } catch {
        setAttachments((prev) => prev.map((a) => (a.id === attachment.id ? { ...a, status: 'error' as const } : a)))
      }
    }
  }, [])

  // Paint image attachment (CTR-0160/CTR-0161, PRP-0099). Uploads the rendered
  // PNG through the existing CTR-0022 path, then persists the editable Fabric
  // scene as a sidecar (CTR-0161) keyed to the uploaded filename so the image
  // stays re-editable. When replaceId is given (re-editing a pending paint
  // attachment) the prior attachment is swapped in place. Returns the new id.
  const attachPaintImage = useCallback(
    async (blob: Blob, scene: unknown, threadId: string, replaceId?: string): Promise<string> => {
      const id = crypto.randomUUID()
      const file = new File([blob], `paint_${id}.png`, { type: 'image/png' })
      const previewUrl = URL.createObjectURL(file)

      setAttachments((prev) => {
        let base = prev
        if (replaceId) {
          const target = prev.find((a) => a.id === replaceId)
          if (target) URL.revokeObjectURL(target.previewUrl)
          base = prev.filter((a) => a.id !== replaceId)
        }
        return [
          ...base,
          {
            id,
            file,
            previewUrl,
            uploadedUri: null,
            mediaType: 'image/png',
            status: 'uploading' as const,
            isPaint: true,
          },
        ]
      })

      try {
        const formData = new FormData()
        formData.append('file', file)
        const res = await fetch(`/api/upload/${threadId}`, { method: 'POST', body: formData })
        if (!res.ok) throw new Error(`Upload failed: ${res.status}`)
        const data = await res.json()
        const filename = data.filename as string

        // Persist the editable scene sidecar (CTR-0161). Best-effort: the PNG
        // attachment is fully usable even if the scene fails to save (it just
        // would not be re-editable).
        try {
          await fetch(`/api/paint/${threadId}/${encodeURIComponent(filename)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(scene),
          })
        } catch {
          // ignore: re-editability is best-effort
        }

        setAttachments((prev) =>
          prev.map((a) =>
            a.id === id ? { ...a, uploadedUri: data.uri as string, filename, status: 'ready' as const } : a,
          ),
        )
      } catch {
        setAttachments((prev) => prev.map((a) => (a.id === id ? { ...a, status: 'error' as const } : a)))
      }
      return id
    },
    [],
  )

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const target = prev.find((a) => a.id === id)
      if (target) URL.revokeObjectURL(target.previewUrl)
      return prev.filter((a) => a.id !== id)
    })
  }, [])

  const clearAttachments = useCallback(() => {
    setAttachments((prev) => {
      for (const a of prev) URL.revokeObjectURL(a.previewUrl)
      return []
    })
  }, [])

  const getImageRefs = useCallback(() => {
    return attachments
      .filter((a) => a.status === 'ready' && a.uploadedUri)
      .map((a) => ({ uri: a.uploadedUri as string, media_type: a.mediaType }))
  }, [attachments])

  return {
    attachments,
    addFiles,
    attachPaintImage,
    removeAttachment,
    clearAttachments,
    getImageRefs,
    hasReadyAttachments: attachments.some((a) => a.status === 'ready'),
    isUploading: attachments.some((a) => a.status === 'uploading'),
  }
}
