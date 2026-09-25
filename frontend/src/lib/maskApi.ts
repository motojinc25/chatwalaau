import type { Annotation, EditorTab } from '@/hooks/useImageEditor'

/**
 * Canvas Image Editor API clients (PRP-0187).
 *
 *  - CTR-0053 v2 `POST /api/images/edit`: the editor's Generate calls the Images API
 *    DIRECTLY (UDR-0169 D8), naming its inputs by slot -- the server orders `image[]`,
 *    composes the prompt and persists the turn.
 *  - CTR-0222 `/api/images/edit-drafts/{thread}/{name}`: the editor state of one image,
 *    restored when Edit is pressed on that image again (D11).
 */

export interface ImageEditResultImage {
  url: string
  filename: string
  revised_prompt?: string
  size?: string
}

export interface ImageEditResult {
  images: ImageEditResultImage[]
  count: number
  tool: string
  inputs?: string[]
  parameters?: Record<string, string>
  prompt?: string
  warnings?: string[]
  tool_call_id?: string
}

export interface ImageEditRequest {
  thread_id: string
  source: string
  mask?: string | null
  annotated?: string | null
  references: string[]
  change: string
  preserve: string
  annotations: Array<{ label: string; note: string }>
  quality?: string | null
  background?: string | null
  n: number
  user_message_id: string
  assistant_message_id: string
  display_text: string
  display_images: Array<{ uri: string; media_type: string }>
}

export class ImageEditError extends Error {}

export async function requestImageEdit(body: ImageEditRequest): Promise<ImageEditResult> {
  const res = await fetch('/api/images/edit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ImageEditError(typeof err.detail === 'string' ? err.detail : 'Image editing failed')
  }
  return res.json()
}

/** The editor draft document (a client payload convention; the server stores opaque JSON). */
export interface ImageEditDraft {
  version: 1
  /** URL of the image being edited. */
  source: string
  width: number
  height: number
  mask_png: string | null
  annotations: Annotation[]
  /** Reference image upload URLs (<= 14). */
  references: string[]
  change: string
  preserve: string
  active_tab: EditorTab
  brush_size: number
  /** Mask DISPLAY colour (#rrggbb) and layer opacity -- never affect the mask sent. */
  mask_color?: string
  mask_opacity?: number
  quality: string
  background: string
  n: number
}

function draftUrl(threadId: string, name: string): string {
  return `/api/images/edit-drafts/${encodeURIComponent(threadId)}/${encodeURIComponent(name)}`
}

/** The draft for an image, or null when the image has none (a fresh edit). */
export async function loadImageEditDraft(threadId: string, name: string): Promise<ImageEditDraft | null> {
  try {
    const res = await fetch(draftUrl(threadId, name))
    if (!res.ok) return null
    const body = (await res.json()) as ImageEditDraft
    return body && body.version === 1 ? body : null
  } catch {
    return null
  }
}

export async function saveImageEditDraft(threadId: string, name: string, draft: ImageEditDraft): Promise<boolean> {
  try {
    const res = await fetch(draftUrl(threadId, name), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draft),
    })
    return res.ok
  } catch {
    return false
  }
}

/** Upload a blob into the session (CTR-0022); returns the stored filename and URL. */
export async function uploadSessionImage(
  threadId: string,
  blob: Blob,
  filename: string,
): Promise<{ filename: string; uri: string }> {
  const form = new FormData()
  form.append('file', new File([blob], filename, { type: blob.type || 'image/png' }))
  const res = await fetch(`/api/upload/${threadId}`, { method: 'POST', body: form })
  const data = res.ok ? await res.json().catch(() => null) : null
  if (!data?.filename || !data?.uri) throw new ImageEditError(`Failed to upload ${filename}`)
  return { filename: data.filename, uri: data.uri }
}

/** The file name at the end of an /api/uploads/<thread>/<name> URL. */
export function uploadName(url: string): string {
  const clean = url.split(/[?#]/)[0]
  return decodeURIComponent(clean.slice(clean.lastIndexOf('/') + 1))
}

/**
 * Re-encode a picked reference image to PNG unless it is already PNG or JPEG -- the
 * only input formats the Images edit API accepts (UDR-0169 D14).
 */
export async function toApiImage(file: File): Promise<Blob> {
  if (file.type === 'image/png' || file.type === 'image/jpeg') return file
  const url = URL.createObjectURL(file)
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image()
      el.onload = () => resolve(el)
      el.onerror = () => reject(new ImageEditError(`${file.name} is not a readable image`))
      el.src = url
    })
    const canvas = document.createElement('canvas')
    canvas.width = img.naturalWidth
    canvas.height = img.naturalHeight
    canvas.getContext('2d')?.drawImage(img, 0, 0)
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new ImageEditError('PNG conversion failed'))), 'image/png'),
    )
  } finally {
    URL.revokeObjectURL(url)
  }
}
