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

  // If the auth lane is not active OR the visitor is already
  // authenticated, send them to the target without showing the form.
  useEffect(() => {
    if (auth.loading) return
    if (auth.mode !== 'login-required') {
      navigate(redirectTarget, { replace: true })
      return
    }
    if (auth.authenticated) {
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
      setErrorMessage('Login is not configured. Ask the operator to set AUTH_USERNAME in .env.')
    } else {
      setErrorMessage('Login service is unavailable. Please try again.')
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm rounded-lg border border-border bg-card p-6 shadow-sm">
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
