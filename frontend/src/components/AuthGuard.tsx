/**
 * Route guard for the Web SPA auth lane (CTR-0096, PRP-0057).
 *
 * Wraps protected routes. While useAuth is bootstrapping it renders a
 * lightweight loading placeholder (no flash of protected content or
 * login). When the user is not authenticated and the backend reports
 * either mode="login-required" OR mode="api-key-only", redirects to
 * /login?redirect=<current>. The api-key-only case matters for browsers
 * reaching a non-loopback instance (e.g. a tunnel): a browser cannot
 * present the Bearer API_KEY, so rendering the app would only produce a
 * 401 storm and a /login <-> /chat redirect loop. The login page shows a
 * clear "enable browser sign-in" message instead (PRP-0097 fix). In
 * "open" mode (loopback / auth disabled) the children render unchanged.
 */

import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'

interface AuthGuardProps {
  children: ReactNode
}

export function AuthGuard({ children }: AuthGuardProps) {
  const { mode, authenticated, loading } = useAuth()
  const location = useLocation()

  if (loading || mode === null) {
    // Render-nothing during bootstrap. Avoids the flash of either the
    // protected page or the login page before the mode is known.
    return null
  }

  // A browser cannot satisfy "api-key-only" (no way to send a Bearer header), so treat it
  // like login-required and route to /login -- which renders an informational message
  // rather than the app, breaking the 401 redirect loop.
  if (!authenticated && (mode === 'login-required' || mode === 'api-key-only')) {
    const target = `${location.pathname}${location.search}`
    return <Navigate to={`/login?redirect=${encodeURIComponent(target)}`} replace />
  }

  return <>{children}</>
}
