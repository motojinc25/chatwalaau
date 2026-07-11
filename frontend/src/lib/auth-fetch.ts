/**
 * Global 401 fetch interceptor for the Web SPA auth lane (CTR-0096 v2,
 * PRP-0057, PRP-0110 / UDR-0088 D1).
 *
 * Installs once at app bootstrap. On any /api/* or /ag-ui/* response with
 * status 401, it raises an in-SPA AUTH_REQUIRED signal. It MUST NOT
 * navigate: a full-page redirect to /login tears down the SPA and destroys
 * the user's typed message, the pending attachments, and the per-session
 * model / generation options. The subscriber (useAuth) opens a modal
 * re-authentication dialog over the preserved application state instead.
 *
 * Excludes the auth endpoints themselves (the login page handles its own
 * 401 inline; status / me checks happen during useAuth bootstrap).
 *
 * The interceptor is idempotent: re-installation under Vite HMR replaces
 * the previous wrapper without nesting.
 */

const INSTALLED_MARK = '__chatwalaauAuthFetchInstalled' as const
const AUTH_PATHS_BYPASS = ['/api/auth/login', '/api/auth/logout', '/api/auth/me', '/api/auth/status']

type AuthRequiredListener = () => void

const listeners = new Set<AuthRequiredListener>()

/**
 * Subscribe to the 401 signal. Returns an unsubscribe function.
 *
 * Consumed by useAuth; the dialog it drives is the only recovery UI
 * (UDR-0088 D1). Listeners are notified best-effort -- a throwing listener
 * never breaks the intercepted fetch.
 */
export function subscribeAuthRequired(listener: AuthRequiredListener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function emitAuthRequired(): void {
  for (const listener of listeners) {
    try {
      listener()
    } catch {
      // A listener must never break the request that triggered it.
    }
  }
}

function shouldSignal(url: string): boolean {
  // Only trigger on same-origin API paths. Allow callers full control
  // over their own error UX outside that prefix.
  let path = url
  try {
    if (url.startsWith('http')) {
      const u = new URL(url, window.location.origin)
      if (u.origin !== window.location.origin) return false
      path = u.pathname
    }
  } catch {
    // url was not a valid absolute URL; treat as same-origin path
  }
  if (!path.startsWith('/api/') && !path.startsWith('/ag-ui/')) return false
  return !AUTH_PATHS_BYPASS.some((p) => path.startsWith(p))
}

export function installAuthFetchInterceptor(): void {
  const w = window as unknown as Record<string, unknown>
  if (w[INSTALLED_MARK]) return
  const originalFetch = window.fetch.bind(window)

  window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const response = await originalFetch(input as RequestInfo, init)
    if (response.status === 401 && shouldSignal(url)) {
      emitAuthRequired()
    }
    return response
  }) as typeof window.fetch

  w[INSTALLED_MARK] = true
}
