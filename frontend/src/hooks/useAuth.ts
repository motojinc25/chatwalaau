/**
 * Web SPA authentication hook (CTR-0096, PRP-0057).
 *
 * Wraps the CTR-0094 endpoints (GET /api/auth/status, POST /api/auth/login,
 * POST /api/auth/logout) and exposes a small React state shape. State is
 * NEVER persisted to localStorage / sessionStorage; full reload re-fetches
 * /api/auth/status. The session cookie is HttpOnly and managed by the
 * browser -- JavaScript cannot read it (by design).
 */

import { useCallback, useEffect, useState } from 'react'

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
}

export type LoginResult = { ok: true } | { ok: false; reason: 'invalid-credentials' | 'disabled' | 'network' }

export interface AuthActions {
  login: (username: string, password: string) => Promise<LoginResult>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

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
  })

  const refresh = useCallback(async () => {
    const payload = await fetchStatus()
    if (payload === null) {
      // Network error -- best-effort: treat as open so the user is not
      // trapped on /login when the backend is unreachable. The 401
      // interceptor still catches subsequent failures.
      setState({
        mode: 'open',
        authenticated: true,
        username: null,
        demoMode: false,
        toolApprovalMode: 'auto',
        version: null,
        loading: false,
      })
      return
    }
    setState({
      mode: payload.mode,
      authenticated: payload.authenticated,
      username: payload.username,
      demoMode: payload.demo_mode === true,
      toolApprovalMode:
        payload.tool_approval_mode === 'skip' || payload.tool_approval_mode === 'always'
          ? payload.tool_approval_mode
          : 'auto',
      version: payload.version && payload.version.length > 0 ? payload.version : null,
      loading: false,
    })
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
    }))
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { ...state, login, logout, refresh }
}
