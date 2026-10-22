// Composer attachment intake policy (CTR-0023 v2, PRP-0228, UDR-0211 D3).
//
// The ONLY frontend copy of the CTR-0022 upload policy: allowed media types,
// the extension fallback and the per-type size limits. The backend SSOT is
// backend/src/app/upload/validation.py; test_prp0228_composer_attachment_intake.py
// fails when the two differ. Picker, drop and paste all classify through here, so
// they cannot disagree about what is supported.

export const IMAGE_MEDIA_TYPES = ['image/gif', 'image/jpeg', 'image/png', 'image/webp'] as const
export const PDF_MEDIA_TYPE = 'application/pdf'
export const MAX_SIZE_IMAGE = 20 * 1024 * 1024 // 20MB
export const MAX_SIZE_PDF = 50 * 1024 * 1024 // 50MB

/** `accept=` values for the composer file pickers. */
export const IMAGE_ACCEPT = IMAGE_MEDIA_TYPES.join(',')
export const PDF_ACCEPT = PDF_MEDIA_TYPE

// Used only when the browser reports an empty File.type (seen for .webp on some
// Windows setups); a non-empty type always wins.
const EXTENSION_MEDIA_TYPES: Record<string, string> = {
  gif: 'image/gif',
  jpeg: 'image/jpeg',
  jpg: 'image/jpeg',
  png: 'image/png',
  webp: 'image/webp',
  pdf: PDF_MEDIA_TYPE,
}

const IMAGE_SET = new Set<string>(IMAGE_MEDIA_TYPES)

const SUPPORTED_TEXT = 'Supported: JPEG, PNG, GIF and WebP images (up to 20 MB) and PDF files (up to 50 MB).'

export const NO_THREAD_NOTICE = "Attachments aren't available in this view. Open the chat page to attach files."

export type RejectReason = 'unsupported-type' | 'too-large'

export interface Rejection {
  name: string
  reason: RejectReason
  size: number
  mediaType: string | null
}

export type Classification = { ok: true; file: File; mediaType: string } | ({ ok: false } & Rejection)

function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot < 0 ? '' : name.slice(dot + 1).toLowerCase()
}

/** The media type the upload will carry, or null when the file is not supported. */
export function resolveMediaType(file: File): string | null {
  if (file.type) return IMAGE_SET.has(file.type) || file.type === PDF_MEDIA_TYPE ? file.type : null
  return EXTENSION_MEDIA_TYPES[extensionOf(file.name)] ?? null
}

export function maxSizeFor(mediaType: string): number {
  return mediaType === PDF_MEDIA_TYPE ? MAX_SIZE_PDF : MAX_SIZE_IMAGE
}

/**
 * Accept or reject one file. An accepted file whose browser type was empty is
 * re-wrapped with the resolved type, because the backend validates the multipart
 * content type and would otherwise answer 400.
 */
export function classifyFile(file: File): Classification {
  const mediaType = resolveMediaType(file)
  if (!mediaType) {
    return { ok: false, name: file.name, reason: 'unsupported-type', size: file.size, mediaType: null }
  }
  if (file.size > maxSizeFor(mediaType)) {
    return { ok: false, name: file.name, reason: 'too-large', size: file.size, mediaType }
  }
  const typed = file.type === mediaType ? file : new File([file], file.name, { type: mediaType })
  return { ok: true, file: typed, mediaType }
}

export function formatSize(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** One English notice for every file one action did not attach (UDR-0211 D4). */
export function rejectionNotice(rejected: Rejection[]): string | null {
  if (rejected.length === 0) return null
  if (rejected.length > 1) {
    return `${rejected.length} files can't be attached: ${rejected.map((r) => r.name).join(', ')}. ${SUPPORTED_TEXT}`
  }
  const [r] = rejected
  if (r.reason === 'too-large' && r.mediaType) {
    const limit = r.mediaType === PDF_MEDIA_TYPE ? 'PDF files can be up to 50 MB.' : 'Images can be up to 20 MB.'
    return `"${r.name}" is too large (${formatSize(r.size)}). ${limit}`
  }
  return `"${r.name}" can't be attached. ${SUPPORTED_TEXT}`
}

export function uploadFailedNotice(name: string, detail: string): string {
  return `"${name}" couldn't be uploaded: ${detail}`
}

const EXTENSION_FOR_TYPE: Record<string, string> = {
  'image/gif': 'gif',
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
}

// Browsers name clipboard images generically (Chrome/Edge/Firefox: "image.png").
const GENERIC_CLIPBOARD_NAME = /^image\.(png|jpe?g|gif|webp)$/i

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** `pasted-YYYYMMDD-HHmmss-<n>.<ext>` in local time (UDR-0211 D2). */
export function pastedImageName(mediaType: string, index: number, now: Date = new Date()): string {
  const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
  const time = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  const ext = EXTENSION_FOR_TYPE[mediaType] ?? 'png'
  return `pasted-${date}-${time}-${index}.${ext}`
}

/**
 * The files a paste should attach, or null to let the browser paste normally
 * (UDR-0211 D1). Only a clipboard with at least one file and NO text/plain is
 * taken over: copies from Excel or Word carry text and a picture, and the user
 * means the text. Generically named clipboard images get a unique pasted-... name;
 * a file that carries its own name (copied in Explorer) keeps it.
 */
export function clipboardAttachmentFiles(data: DataTransfer | null, now: Date = new Date()): File[] | null {
  if (!data || data.types.includes('text/plain')) return null
  const files: File[] = []
  for (const item of Array.from(data.items)) {
    if (item.kind !== 'file') continue
    const file = item.getAsFile()
    if (file) files.push(file)
  }
  if (files.length === 0) return null
  let index = 0
  return files.map((file) => {
    if (!IMAGE_SET.has(file.type) || (file.name && !GENERIC_CLIPBOARD_NAME.test(file.name))) return file
    index += 1
    return new File([file], pastedImageName(file.type, index, now), { type: file.type })
  })
}
