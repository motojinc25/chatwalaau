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
 *
 * Password-field usability (v0.102.2, CTR-0096 v2): a Show/Hide password
 * toggle temporarily reveals the typed value, and a Caps Lock hint warns when
 * the lock is active so a mistyped password is easier to spot before submit.
 */

import { Eye, EyeOff } from 'lucide-react'
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
  const [showPassword, setShowPassword] = useState(false)
  const [capsLockOn, setCapsLockOn] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  // Caps Lock hint. Detection is GLOBAL for the whole login page (document-level
  // listeners, not gated on which field is focused) because the initial focus is
  // the username field, so a Caps Lock press before touching the password field
  // must still register. A FocusEvent cannot report the modifier state, so we
  // rely on key + pointer events.
  //
  // The Caps Lock key itself is the hard case, especially on the Surface Type
  // Cover: its OWN keydown reports the PRE-toggle modifier state, and its keyup
  // is often swallowed by the OS Caps Lock indicator, so reading
  // getModifierState on that key never flips the hint. So we handle it two ways
  // that agree with each other: on its non-repeat keydown we TOGGLE our flag
  // (this is the only signal that survives on Surface), and if a keyup DOES
  // arrive we overwrite with the authoritative getModifierState (standard
  // keyboards). EVERY OTHER key/pointer event re-syncs to getModifierState, so a
  // drifted flag self-corrects on the very next keystroke or click.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'CapsLock' || event.code === 'CapsLock') {
        if (event.type === 'keyup' && typeof event.getModifierState === 'function') {
          setCapsLockOn(event.getModifierState('CapsLock'))
        } else if (event.type === 'keydown' && !event.repeat) {
          setCapsLockOn((prev) => !prev)
        }
        return
      }
      if (typeof event.getModifierState === 'function') {
        setCapsLockOn(event.getModifierState('CapsLock'))
      }
    }
    const onPointer = (event: PointerEvent) => {
      if (typeof event.getModifierState === 'function') {
        setCapsLockOn(event.getModifierState('CapsLock'))
      }
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('keyup', onKey, true)
    document.addEventListener('pointerdown', onPointer, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('keyup', onKey, true)
      document.removeEventListener('pointerdown', onPointer, true)
    }
  }, [])

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
            <div className="relative">
              <Input
                id="login-password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                className="pr-10"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting}
                aria-describedby={capsLockOn ? 'login-capslock' : undefined}
                required
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                disabled={submitting}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                aria-pressed={showPassword}
                title={showPassword ? 'Hide password' : 'Show password'}
                className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50">
                {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
            {capsLockOn && (
              <p id="login-capslock" className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-500" role="alert">
                Caps Lock is on.
              </p>
            )}
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
