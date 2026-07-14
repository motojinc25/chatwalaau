/**
 * Web SPA authentication hook (CTR-0096 v2, PRP-0057, PRP-0110).
 *
 * Wraps the CTR-0094 endpoints (GET /api/auth/status, POST /api/auth/login,
 * POST /api/auth/logout) and exposes a small React state shape. State is
 * NEVER persisted to localStorage / sessionStorage; full reload re-fetches
 * /api/auth/status. The session cookie is HttpOnly and managed by the
 * browser -- JavaScript cannot read it (by design).
 *
 * PRP-0110 / UDR-0088 D1+D2: subscribes to the AUTH_REQUIRED signal raised by
 * the 401 fetch interceptor and exposes `reauthRequired` so a modal dialog can
 * re-authenticate over the preserved application state. A mid-session 401 does
 * NOT flip `authenticated` -- flipping it would make AuthGuard unmount the
 * protected subtree through <Navigate>, destroying exactly the state we are
 * trying to keep. `hasEverAuthenticated` latches the boot-vs-mid-session
 * distinction for AuthGuard; an explicit logout resets it so the guard resumes
 * its normal redirect.
 */

import { useCallback, useEffect, useState } from 'react'
import { subscribeAuthRequired } from '@/lib/auth-fetch'

export type AuthMode = 'open' | 'api-key-only' | 'login-required'

/** PRP-0067 / CTR-0094 v4 -- runtime tool-approval policy. */
export type ToolApprovalMode = 'skip' | 'auto' | 'always'

export interface AuthState {
  mode: AuthMode | null
  authenticated: boolean
  username: string | null
  /** PRP-0066 / CTR-0094 v3: backend DEMO_MODE flag. SPA renders a "DEMO" badge when true. */
  demoMode: boolean
  /** PRP-0067 / CTR-0094 v4: backend TOOL_APPROVAL_MODE. "skip" renders PermissionsDisabledBanner. */
  toolApprovalMode: ToolApprovalMode
  /** PRP-0068 / CTR-0094 v5: running backend app version. null when the backend omits it. */
  version: string | null
  loading: boolean
  /**
   * PRP-0110 / UDR-0088 D2: true once this page lifetime has observed an
   * authenticated session. AuthGuard redirects to /login only while false, so a
   * mid-session auth loss raises the dialog instead of unmounting the app.
   */
  hasEverAuthenticated: boolean
  /** PRP-0110 / UDR-0088 D1: a 401 was intercepted; render ReauthDialog. */
  reauthRequired: boolean
  /**
   * PRP-0112 / UDR-0092 D1: the last `GET /api/auth/status` could not reach the
   * server (network error, not an HTTP status). Distinct from every auth state --
   * "we could not reach the server" is NOT "the server says you are fine".
   */
  backendUnreachable: boolean
}

export type LoginResult = { ok: true } | { ok: false; reason: 'invalid-credentials' | 'disabled' | 'network' }

export interface AuthActions {
  login: (username: string, password: string) => Promise<LoginResult>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

/** The username to prefill in ReauthDialog: the last one we saw authenticated. */
export type ReauthUsername = string | null

interface StatusPayload {
  mode: AuthMode
  authenticated: boolean
  username: string | null
  /** Optional in older backend builds; defaults to false (PRP-0066, CTR-0094 v3). */
  demo_mode?: boolean
  /** Optional in older backend builds; defaults to "auto" (PRP-0067, CTR-0094 v4). */
  tool_approval_mode?: ToolApprovalMode
  /** Optional in older backend builds; null when absent (PRP-0068, CTR-0094 v5). */
  version?: string
}

const STATUS_URL = '/api/auth/status'
const LOGIN_URL = '/api/auth/login'
const LOGOUT_URL = '/api/auth/logout'

async function fetchStatus(): Promise<StatusPayload | null> {
  try {
    const res = await fetch(STATUS_URL, { credentials: 'same-origin' })
    if (!res.ok) return null
    return (await res.json()) as StatusPayload
  } catch {
    return null
  }
}

export function useAuth(): AuthState & AuthActions {
  const [state, setState] = useState<AuthState>({
    mode: null,
    authenticated: false,
    username: null,
    demoMode: false,
    toolApprovalMode: 'auto',
    version: null,
    loading: true,
    hasEverAuthenticated: false,
    reauthRequired: false,
    backendUnreachable: false,
  })

  const refresh = useCallback(async () => {
    const payload = await fetchStatus()
    if (payload === null) {
      // PRP-0112 / UDR-0092 D1: an unreachable backend MUST NOT be reported as an
      // authenticated session.
      //
      // This branch used to synthesize `mode: 'open', authenticated: true` -- a
      // well-intentioned lie (it kept users off /login when the backend was down)
      // that made AuthGuard render the full chat UI against a dead server. The user
      // then saw an empty session list, no model selector, no settings icon, and
      // sends that silently failed, with nothing anywhere naming the real cause.
      //
      // The honest state is `backendUnreachable`. BootGate (CTR-0096 v3) holds the
      // app behind a readiness indicator while this is true at boot, so the /login
      // trap the old fallback guarded against is now unreachable by construction:
      // an unreachable backend never gets as far as AuthGuard.
      setState((prev) => ({
        ...prev,
        backendUnreachable: true,
        loading: false,
      }))
      return
    }
    setState((prev) => ({
      ...prev,
      // The server answered, so whatever we thought before, it is reachable now.
      backendUnreachable: false,
      mode: payload.mode,
      authenticated: payload.authenticated,
      // /api/auth/status omits the username when the session is gone. Retain the
      // last known one so ReauthDialog can prefill it (PRP-0110).
      username: payload.authenticated ? payload.username : (payload.username ?? prev.username),
      demoMode: payload.demo_mode === true,
      toolApprovalMode:
        payload.tool_approval_mode === 'skip' || payload.tool_approval_mode === 'always'
          ? payload.tool_approval_mode
          : 'auto',
      version: payload.version && payload.version.length > 0 ? payload.version : null,
      loading: false,
      // Latch (UDR-0088 D2) and clear the dialog once the session is live again.
      hasEverAuthenticated: prev.hasEverAuthenticated || payload.authenticated,
      reauthRequired: payload.authenticated ? false : prev.reauthRequired,
    }))
  }, [])

  const login = useCallback(
    async (username: string, password: string): Promise<LoginResult> => {
      try {
        const res = await fetch(LOGIN_URL, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        })
        if (res.status === 204) {
          await refresh()
          return { ok: true }
        }
        if (res.status === 401) return { ok: false, reason: 'invalid-credentials' }
        if (res.status === 503) return { ok: false, reason: 'disabled' }
        return { ok: false, reason: 'network' }
      } catch {
        return { ok: false, reason: 'network' }
      }
    },
    [refresh],
  )

  const logout = useCallback(async () => {
    try {
      await fetch(LOGOUT_URL, { method: 'POST', credentials: 'same-origin' })
    } catch {
      // Best-effort logout; cookie may already be gone.
    }
    setState((prev) => ({
      mode: 'login-required',
      authenticated: false,
      username: null,
      demoMode: prev.demoMode,
      toolApprovalMode: prev.toolApprovalMode,
      version: prev.version,
      loading: false,
      // An explicit sign-out returns the SPA to a boot-like state, so AuthGuard
      // resumes its normal /login redirect rather than raising the dialog.
      hasEverAuthenticated: false,
      reauthRequired: false,
      // A successful logout proves the backend answered.
      backendUnreachable: false,
    }))
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // PRP-0110 / UDR-0088 D1: a 401 raises the dialog. `authenticated` is left
  // untouched on purpose -- flipping it would unmount the protected subtree via
  // AuthGuard's <Navigate> and destroy the state the dialog exists to preserve.
  useEffect(
    () =>
      subscribeAuthRequired(() => {
        setState((prev) => (prev.reauthRequired ? prev : { ...prev, reauthRequired: true }))
      }),
    [],
  )

  return { ...state, login, logout, refresh }
}
