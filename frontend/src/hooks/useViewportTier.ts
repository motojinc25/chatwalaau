import { useEffect, useSyncExternalStore } from 'react'

/**
 * Viewport tier of the chat surface (PRP-0171, UDR-0153 D1).
 *
 * The ONE place that reads viewport media queries for the chat and the session
 * sidebar. Two independent signals, because they answer different questions:
 *
 *   narrow        = NOT (min-width: 768px)              -> layout, entry gating
 *   touchPrimary  = (hover: none) and (pointer: coarse) -> hover affordances, Enter key
 *
 * A tablet is wide and has no hover; a narrowed desktop window is narrow and has a
 * keyboard. 768 equals Tailwind v4's default `md` breakpoint (48rem).
 */
export const DESKTOP_BREAKPOINT_PX = 768

const NARROW_QUERY = `(max-width: ${DESKTOP_BREAKPOINT_PX - 0.02}px)`
const TOUCH_PRIMARY_QUERY = '(hover: none) and (pointer: coarse)'

export interface ViewportTier {
  narrow: boolean
  touchPrimary: boolean
}

function matches(query: string): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(query).matches
}

/** Non-reactive read, for event handlers outside React render (useSession). */
export function isNarrowViewport(): boolean {
  return matches(NARROW_QUERY)
}

function subscribe(query: string) {
  return (onChange: () => void) => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
    const mql = window.matchMedia(query)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }
}

const subscribeNarrow = subscribe(NARROW_QUERY)
const subscribeTouch = subscribe(TOUCH_PRIMARY_QUERY)
const getNarrow = () => matches(NARROW_QUERY)
const getTouch = () => matches(TOUCH_PRIMARY_QUERY)
const getServer = () => false

export function useViewportTier(): ViewportTier {
  const narrow = useSyncExternalStore(subscribeNarrow, getNarrow, getServer)
  const touchPrimary = useSyncExternalStore(subscribeTouch, getTouch, getServer)
  return { narrow, touchPrimary }
}

/** CSS variable carrying the height the browser actually shows (v0.155.1). */
export const VISIBLE_VIEWPORT_HEIGHT_VAR = '--app-visible-height'

/**
 * Publish the VISIBLE viewport height as a CSS variable (PRP-0171 follow-up, v0.155.1).
 *
 * `100vh` on iPad / iPhone Safari includes the tab bar and toolbar, so a `h-screen`
 * page is taller than what is shown and its bottom (the composer) sits under the
 * browser chrome; `100dvh` does not follow the on-screen keyboard on iOS either.
 * `window.visualViewport` reports what is actually visible. Multiplying by `scale`
 * keeps a pinch-zoom from shrinking the layout; on a desktop browser the value equals
 * the window height, so nothing changes there. Consumers fall back to `100dvh`.
 */
export function useVisibleViewportHeight(): void {
  useEffect(() => {
    if (typeof window === 'undefined') return
    const root = document.documentElement
    const vv = window.visualViewport
    const update = () => {
      const height = vv ? vv.height * vv.scale : window.innerHeight
      root.style.setProperty(VISIBLE_VIEWPORT_HEIGHT_VAR, `${Math.round(height)}px`)
    }
    update()
    vv?.addEventListener('resize', update)
    window.addEventListener('resize', update)
    window.addEventListener('orientationchange', update)
    return () => {
      vv?.removeEventListener('resize', update)
      window.removeEventListener('resize', update)
      window.removeEventListener('orientationchange', update)
      root.style.removeProperty(VISIBLE_VIEWPORT_HEIGHT_VAR)
    }
  }, [])
}
