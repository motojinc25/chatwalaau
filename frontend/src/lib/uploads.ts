/**
 * Upload serving helpers (CTR-0022, PRP-0116, UDR-0097).
 *
 * The upload serve route is authenticated (CTR-0083). A same-origin top-level
 * navigation carries the session cookie automatically, so "open full size" is a
 * plain `window.open` of the server URL: a logged-in user opens it, an
 * unauthenticated viewer gets 401, and localhost (loopback) opens it with no
 * configuration. No signed URL / token is involved.
 */
export function openUploadFullSize(uri: string): void {
  window.open(uri, '_blank', 'noopener,noreferrer')
}
