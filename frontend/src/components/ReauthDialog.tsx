/**
 * In-place re-authentication dialog (CTR-0096 v2, PRP-0110 / UDR-0088 D1).
 *
 * Raised when the 401 fetch interceptor observes an expired or revoked session
 * MID-SESSION. It replaces the pre-PRP-0110 `window.location.replace('/login')`,
 * which tore down the SPA and destroyed the user's typed message, pending
 * attachments, and per-session model / generation options.
 *
 * The dialog is MODAL and NON-DISMISSIBLE (no outside-click, no Escape, no close
 * button): a dismissed dialog would leave the user operating a view whose every
 * subsequent request 401s. On success the application state behind it is intact
 * and the user simply resumes -- the composer still holds their text (CTR-0004 v2).
 *
 * Reuses POST /api/auth/login (CTR-0094) verbatim. No auth state is written to
 * localStorage / sessionStorage / IndexedDB (UDR-0033 D4, UDR-0088 D4).
 */

import { Eye, EyeOff } from 'lucide-react'
import { type FormEvent, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import type { LoginResult } from '@/hooks/useAuth'

interface ReauthDialogProps {
  open: boolean
  /** Last known authenticated username; prefilled read-only when present. */
  username: string | null
  login: (username: string, password: string) => Promise<LoginResult>
}

export function ReauthDialog({ open, username, login }: ReauthDialogProps) {
  const [typedUsername, setTypedUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  // Never keep a typed password mounted across open/close cycles.
  useEffect(() => {
    if (!open) {
      setPassword('')
      setErrorMessage(null)
      setShowPassword(false)
    }
  }, [open])

  const effectiveUsername = username ?? typedUsername

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    if (submitting || !effectiveUsername || !password) return
    setSubmitting(true)
    setErrorMessage(null)
    const result = await login(effectiveUsername, password)
    setSubmitting(false)
    if (result.ok) {
      // useAuth.refresh() inside login() clears `reauthRequired`, unmounting us.
      setPassword('')
      return
    }
    if (result.reason === 'invalid-credentials') setErrorMessage('Invalid username or password.')
    else if (result.reason === 'disabled') setErrorMessage('Login is not configured on this server.')
    else setErrorMessage('Cannot reach the server. Check your connection and try again.')
  }

  return (
    <Dialog open={open}>
      <DialogContent
        // Non-dismissible: suppress outside-click, Escape, and the built-in close X.
        onInteractOutside={(event) => event.preventDefault()}
        onEscapeKeyDown={(event) => event.preventDefault()}
        onPointerDownOutside={(event) => event.preventDefault()}
        className="max-w-md [&>button]:hidden">
        <DialogHeader>
          <DialogTitle>Session expired</DialogTitle>
          <DialogDescription>
            Your sign-in session ended, most likely because the server restarted. Sign in again to continue -- your chat
            and anything you have typed are still here.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="reauth-username" className="text-sm font-medium">
              Username
            </label>
            <Input
              id="reauth-username"
              autoComplete="username"
              value={effectiveUsername}
              readOnly={username !== null}
              disabled={submitting}
              onChange={(e) => setTypedUsername(e.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="reauth-password" className="text-sm font-medium">
              Password
            </label>
            <div className="relative">
              <Input
                id="reauth-password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                // eslint-disable-next-line jsx-a11y/no-autofocus -- the dialog is modal and this is the only action
                autoFocus
                value={password}
                disabled={submitting}
                onChange={(e) => setPassword(e.target.value)}
                className="pr-10"
              />
              <button
                type="button"
                onClick={() => setShowPassword((prev) => !prev)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                aria-label={showPassword ? 'Hide password' : 'Show password'}>
                {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>

          {errorMessage && <p className="text-sm text-red-600">{errorMessage}</p>}

          <Button type="submit" className="w-full" disabled={submitting || !effectiveUsername || !password}>
            {submitting ? 'Signing in...' : 'Sign in'}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
