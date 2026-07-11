/**
 * Route guard for the Web SPA auth lane (CTR-0096 v2, PRP-0057, PRP-0110).
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
 *
 * PRP-0110 / UDR-0088 D2: the redirect fires only while this page lifetime has
 * NEVER seen an authenticated session. Once it has, a later auth loss keeps the
 * children mounted and raises ReauthDialog over them -- unmounting ChatPage via
 * <Navigate> would discard exactly the state (typed message, attachments, model
 * selection) that PRP-0110 exists to preserve. An explicit logout resets the
 * latch, so signing out still routes to /login as before. The api-key-only lane
 * never authenticates, so it keeps the v1 redirect that breaks its 401 loop.
 */

import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { ReauthDialog } from '@/components/ReauthDialog'
import { useAuth } from '@/hooks/useAuth'

interface AuthGuardProps {
  children: ReactNode
}

export function AuthGuard({ children }: AuthGuardProps) {
  const { mode, authenticated, loading, hasEverAuthenticated, reauthRequired, username, login } = useAuth()
  const location = useLocation()

  if (loading || mode === null) {
    // Render-nothing during bootstrap. Avoids the flash of either the
    // protected page or the login page before the mode is known.
    return null
  }

  // A browser cannot satisfy "api-key-only" (no way to send a Bearer header), so treat it
  // like login-required and route to /login -- which renders an informational message
  // rather than the app, breaking the 401 redirect loop.
  if (!authenticated && !hasEverAuthenticated && (mode === 'login-required' || mode === 'api-key-only')) {
    const target = `${location.pathname}${location.search}`
    return <Navigate to={`/login?redirect=${encodeURIComponent(target)}`} replace />
  }

  return (
    <>
      {children}
      <ReauthDialog
        open={mode === 'login-required' && hasEverAuthenticated && reauthRequired}
        username={username}
        login={login}
      />
    </>
  )
}
