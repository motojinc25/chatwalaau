/**
 * Route guard for the Web SPA auth lane (CTR-0096, PRP-0057).
 *
 * Wraps protected routes. While useAuth is bootstrapping it renders a
 * lightweight loading placeholder (no flash of protected content or
 * login). When the backend reports mode="login-required" and the user
 * is not authenticated, redirects to /login?redirect=<current>. For
 * any other mode (open / api-key-only / authenticated) renders the
 * protected children unchanged.
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

  if (mode === 'login-required' && !authenticated) {
    const target = `${location.pathname}${location.search}`
    return <Navigate to={`/login?redirect=${encodeURIComponent(target)}`} replace />
  }

  return <>{children}</>
}
