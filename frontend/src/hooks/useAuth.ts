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

export interface AuthState {
  mode: AuthMode | null
  authenticated: boolean
  username: string | null
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
    loading: true,
  })

  const refresh = useCallback(async () => {
    const payload = await fetchStatus()
    if (payload === null) {
      // Network error -- best-effort: treat as open so the user is not
      // trapped on /login when the backend is unreachable. The 401
      // interceptor still catches subsequent failures.
      setState({ mode: 'open', authenticated: true, username: null, loading: false })
      return
    }
    setState({
      mode: payload.mode,
      authenticated: payload.authenticated,
      username: payload.username,
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
    setState({
      mode: 'login-required',
      authenticated: false,
      username: null,
      loading: false,
    })
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { ...state, login, logout, refresh }
}
