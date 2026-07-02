/**
 * Login page for the Web SPA auth lane (CTR-0096, PRP-0057).
 *
 * Public route at /login. Renders username + password fields and
 * posts to CTR-0094 POST /api/auth/login. On success, navigates to
 * ?redirect= or /chat. On 401, shows an inline error. On 503, tells
 * the operator that login is not configured.
 *
 * Already-authenticated visitors are redirected away from /login to
 * avoid an empty login form when no action is needed.
 */

import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useAuth } from '@/hooks/useAuth'

const DEFAULT_REDIRECT = '/chat'

export function LoginPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const auth = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const redirectTarget = searchParams.get('redirect') || DEFAULT_REDIRECT

  // Navigation rules:
  // - open mode (loopback / auth disabled): no login needed -> go to the app.
  // - login-required + already authenticated: go to the app.
  // - login-required + not authenticated: stay and show the form.
  // - api-key-only: a browser cannot present the Bearer API_KEY, so there is NO
  //   browser login. Do NOT bounce back to the app (that caused a /login <-> /chat
  //   loop); stay here and show an informational message (PRP-0097 fix).
  useEffect(() => {
    if (auth.loading) return
    if (auth.mode === 'open') {
      navigate(redirectTarget, { replace: true })
      return
    }
    if (auth.mode === 'login-required' && auth.authenticated) {
      navigate(redirectTarget, { replace: true })
    }
  }, [auth.loading, auth.mode, auth.authenticated, navigate, redirectTarget])

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitting) return
    if (!username.trim() || !password) {
      setErrorMessage('Please enter both username and password.')
      return
    }
    setSubmitting(true)
    setErrorMessage(null)
    const result = await auth.login(username.trim(), password)
    setSubmitting(false)
    if (result.ok) {
      navigate(redirectTarget, { replace: true })
      return
    }
    if (result.reason === 'invalid-credentials') {
      setErrorMessage('Invalid username or password.')
    } else if (result.reason === 'disabled') {
      setErrorMessage('Login is not configured.')
    } else {
      setErrorMessage('Login service is unavailable. Please try again.')
    }
  }

  // api-key-only: browser sign-in is not enabled on this instance. Show guidance instead
  // of a form (a browser cannot send the Bearer API_KEY), which also breaks the loop.
  if (!auth.loading && auth.mode === 'api-key-only') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-4">
        <div className="w-full max-w-md rounded-lg border border-border bg-card p-6 shadow-xs">
          <h1 className="mb-2 text-xl font-semibold">Browser sign-in is not enabled</h1>
          <p className="mb-3 text-sm text-muted-foreground">
            This server is reachable over the network and is protected by an API key, which only command-line / SDK
            clients can send. Web browsers cannot present it.
          </p>
          <p className="text-sm text-muted-foreground">
            To use ChatWalaʻau in the browser from here, the operator must enable the web sign-in lane by setting{' '}
            <code className="rounded bg-muted px-1">AUTH_USERNAME</code> and{' '}
            <code className="rounded bg-muted px-1">AUTH_PASSWORD_HASH</code> in the backend{' '}
            <code className="rounded bg-muted px-1">.env</code>, then restart. See the Web Authentication guide.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm rounded-lg border border-border bg-card p-6 shadow-xs">
        <h1 className="mb-1 text-xl font-semibold">Sign in to ChatWalaʻau</h1>
        <p className="mb-6 text-sm text-muted-foreground">Enter the username and password.</p>
        <form onSubmit={handleSubmit} className="space-y-4" noValidate>
          <div className="space-y-1">
            <label htmlFor="login-username" className="text-sm font-medium">
              Username
            </label>
            <Input
              id="login-username"
              type="text"
              autoComplete="username"
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
              required
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="login-password" className="text-sm font-medium">
              Password
            </label>
            <Input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              required
            />
          </div>
          {errorMessage !== null && (
            <p className="text-sm text-destructive" role="alert">
              {errorMessage}
            </p>
          )}
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? 'Signing in...' : 'Sign in'}
          </Button>
        </form>
      </div>
    </div>
  )
}
