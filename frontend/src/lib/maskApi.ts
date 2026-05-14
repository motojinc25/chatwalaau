interface MaskEditResult {
  images: Array<{ url: string; filename: string; revised_prompt?: string }>
  count: number
  tool: string
}

export async function applyMaskEdit(params: {
  threadId: string
  compositedImageBlob: Blob
  prompt: string
  size?: string
  quality?: string
}): Promise<MaskEditResult> {
  const form = new FormData()
  form.append('image', new File([params.compositedImageBlob], 'image.png', { type: 'image/png' }))
  form.append('prompt', params.prompt)
  form.append('thread_id', params.threadId)
  if (params.size) form.append('size', params.size)
  if (params.quality) form.append('quality', params.quality)

  const res = await fetch('/api/images/edit', { method: 'POST', body: form })

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Mask edit failed')
  }

  return res.json()
}
