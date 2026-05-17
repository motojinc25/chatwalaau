/**
 * Global 401 fetch interceptor for the Web SPA auth lane (CTR-0096, PRP-0057).
 *
 * Installs once at app bootstrap. On any /api/* or /ag-ui/* response
 * with status 401, redirects the browser to /login?redirect=<current>.
 * Excludes the auth endpoints themselves (login pages handle their own
 * 401 inline; status / me checks happen during useAuth bootstrap).
 *
 * The interceptor is idempotent: re-installation under Vite HMR replaces
 * the previous wrapper without nesting.
 */

const INSTALLED_MARK = '__chatwalaauAuthFetchInstalled' as const
const AUTH_PATHS_BYPASS = ['/api/auth/login', '/api/auth/logout', '/api/auth/me', '/api/auth/status']

function shouldRedirect(url: string): boolean {
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

function buildLoginRedirectUrl(): string {
  const current = window.location.pathname + window.location.search
  return `/login?redirect=${encodeURIComponent(current)}`
}

function redirectToLogin(): void {
  if (window.location.pathname === '/login') return
  window.location.replace(buildLoginRedirectUrl())
}

export function installAuthFetchInterceptor(): void {
  const w = window as unknown as Record<string, unknown>
  if (w[INSTALLED_MARK]) return
  const originalFetch = window.fetch.bind(window)

  window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const response = await originalFetch(input as RequestInfo, init)
    if (response.status === 401 && shouldRedirect(url)) {
      redirectToLogin()
    }
    return response
  }) as typeof window.fetch

  w[INSTALLED_MARK] = true
}
