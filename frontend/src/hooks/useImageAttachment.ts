import { useCallback, useState } from 'react'
import { classifyFile, type Rejection, rejectionNotice, uploadFailedNotice } from '@/lib/attachmentPolicy'

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

/**
 * An inline composer notice (CTR-0023 v2, UDR-0211 D4). `id` changes with every
 * notice, so the same text reported twice still restarts its auto-hide timer.
 */
export interface AttachmentNoticeState {
  id: number
  text: string
}

async function uploadErrorDetail(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string' && body.detail) return body.detail
  } catch {
    // not JSON: fall through to the status line
  }
  return `Upload failed (HTTP ${res.status}).`
}

export function useImageAttachment() {
  const [attachments, setAttachments] = useState<ImageAttachment[]>([])
  const [notice, setNotice] = useState<AttachmentNoticeState | null>(null)

  /** Show a composer notice; a new notice replaces the current one. */
  const reportNotice = useCallback((text: string) => {
    setNotice((prev) => ({ id: (prev?.id ?? 0) + 1, text }))
  }, [])

  const dismissNotice = useCallback(() => setNotice(null), [])

  // Picker, drop and paste all arrive here (UDR-0211 D1/D3). Every file that is
  // not attached is reported (D4); accepted files in the same action still attach.
  const addFiles = useCallback(
    async (files: FileList | File[], threadId: string) => {
      const accepted: { file: File; mediaType: string }[] = []
      const rejected: Rejection[] = []
      for (const f of Array.from(files)) {
        const c = classifyFile(f)
        if (c.ok) accepted.push({ file: c.file, mediaType: c.mediaType })
        else rejected.push(c)
      }
      const rejectedText = rejectionNotice(rejected)
      if (rejectedText) reportNotice(rejectedText)
      if (accepted.length === 0) return

      const newAttachments: ImageAttachment[] = accepted.map(({ file, mediaType }) => ({
        id: crypto.randomUUID(),
        file,
        previewUrl: URL.createObjectURL(file),
        uploadedUri: null,
        mediaType,
        status: 'uploading' as const,
      }))

      setAttachments((prev) => [...prev, ...newAttachments])

      for (const attachment of newAttachments) {
        let failure: string | null = null
        try {
          const formData = new FormData()
          formData.append('file', attachment.file)

          const res = await fetch(`/api/upload/${threadId}`, {
            method: 'POST',
            body: formData,
          })
          if (res.ok) {
            // The server may store the file under a -N suffixed name (CTR-0022 v5),
            // so the response uri is the only reference to keep.
            const data = await res.json()
            setAttachments((prev) =>
              prev.map((a) =>
                a.id === attachment.id ? { ...a, uploadedUri: data.uri as string, status: 'ready' as const } : a,
              ),
            )
          } else {
            failure = await uploadErrorDetail(res)
          }
        } catch {
          failure = 'Network error.'
        }
        if (failure !== null) {
          setAttachments((prev) => prev.map((a) => (a.id === attachment.id ? { ...a, status: 'error' as const } : a)))
          reportNotice(uploadFailedNotice(attachment.file.name, failure))
        }
      }
    },
    [reportNotice],
  )

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
    // A sent (or discarded) composer takes its notice with it (UDR-0211 D4).
    setNotice(null)
  }, [])

  const getImageRefs = useCallback(() => {
    return attachments
      .filter((a) => a.status === 'ready' && a.uploadedUri)
      .map((a) => ({ uri: a.uploadedUri as string, media_type: a.mediaType }))
  }, [attachments])

  return {
    attachments,
    addFiles,
    notice,
    reportNotice,
    dismissNotice,
    attachPaintImage,
    removeAttachment,
    clearAttachments,
    getImageRefs,
    hasReadyAttachments: attachments.some((a) => a.status === 'ready'),
    isUploading: attachments.some((a) => a.status === 'uploading'),
  }
}
