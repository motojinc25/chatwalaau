/**
 * Chat window navigation / window-open / permission decisions (CTR-0214, UDR-0151 D12).
 * Pure module: the Electron wiring in windows.ts only calls these.
 */

export type OpenDecision = 'desktop-window' | 'external' | 'deny'

function parse(raw: string): URL | null {
  try {
    return new URL(raw)
  } catch {
    return null
  }
}

/** http(s) URLs only, and never ones carrying credentials. */
export function isExternalUrlAllowed(raw: string): boolean {
  const u = parse(raw)
  if (!u) return false
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false
  return u.username === '' && u.password === ''
}

/** Main-frame navigation stays inside the backend origin. */
export function isAllowedNavigation(raw: string, backendOrigin: string): boolean {
  const u = parse(raw)
  return u !== null && u.origin === backendOrigin
}

/** window.open: same origin -> new Desktop window; http(s) -> OS browser; else deny. */
export function decideWindowOpen(raw: string, backendOrigin: string): OpenDecision {
  const u = parse(raw)
  if (!u) return 'deny'
  if (u.origin === backendOrigin) return 'desktop-window'
  return isExternalUrlAllowed(raw) ? 'external' : 'deny'
}

export const GRANTED_PERMISSIONS: ReadonlySet<string> = new Set(['media', 'clipboard-sanitized-write'])

export function isPermissionGranted(permission: string, requestingUrl: string, backendOrigin: string): boolean {
  if (!GRANTED_PERMISSIONS.has(permission)) return false
  const u = parse(requestingUrl)
  return u !== null && u.origin === backendOrigin
}
