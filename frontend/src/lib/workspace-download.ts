/**
 * Workspace download helpers over CTR-0136 v2 (PRP-0093 / UDR-0071 D2).
 *
 * Lifted out of FileExplorer by PRP-0166 (UDR-0150 D3) so the chat's workspace
 * file references and the File Explorer share ONE download path. The request is a
 * same-origin fetch, so the session cookie authenticates it, and a 401 reaches the
 * global re-auth dialog through the auth-fetch interceptor (CTR-0096).
 */

/** Build the CTR-0136 `/raw` URL for a workspace-relative path. */
export function workspaceRawUrl(path: string): string {
  return `/api/workspace/raw?path=${encodeURIComponent(path)}`
}

/**
 * Fetch `url` and save the body as `filename`.
 *
 * Returns null on success, or the server's `detail` (else a generic message) on
 * failure, so a caller can show why. The File Explorer ignores the result, which
 * keeps its pre-PRP-0166 best-effort behaviour.
 */
export async function downloadUrl(url: string, filename: string): Promise<string | null> {
  try {
    const res = await fetch(url)
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      return typeof body?.detail === 'string' && body.detail ? body.detail : `Download failed (${res.status})`
    }
    const blob = await res.blob()
    const objectUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(objectUrl)
    return null
  } catch {
    return 'Download failed'
  }
}
