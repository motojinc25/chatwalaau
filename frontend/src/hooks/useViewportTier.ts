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

/** CSS variables carrying the box the browser actually shows (v0.155.1 / v0.155.2). */
export const VISIBLE_VIEWPORT_HEIGHT_VAR = '--app-visible-height'
export const VISIBLE_VIEWPORT_TOP_VAR = '--app-visible-top'

/** iOS reports the rotated size late: re-measure on the next frame and after these delays. */
const ORIENTATION_SETTLE_DELAYS_MS = [100, 300, 600]

/**
 * Pin the chat surface to the VISIBLE viewport (PRP-0171 follow-ups, v0.155.1 / v0.155.2).
 *
 * - `100vh` on iPad / iPhone Safari includes the tab bar and toolbar, and `100dvh` does not
 *   follow the on-screen keyboard, so the height comes from `window.visualViewport`.
 * - When the keyboard opens, iOS scrolls the page to reveal the focused input, which moves
 *   the visible area by `visualViewport.offsetTop`. An in-flow page moves with that scroll
 *   and the conversation leaves the screen, so the surface is `position: fixed` at that
 *   offset (`--app-visible-top`) instead.
 * - On rotation iOS fires `orientationchange` / `resize` BEFORE the new size is final, so
 *   the measurement is repeated until it settles.
 * - While pinch-zoomed the layout keeps the window size (no shrinking under the finger).
 *
 * On a desktop browser the values equal the window's (top 0, window height), so nothing
 * changes there. Consumers fall back to `100dvh` / `0px`.
 */
export function useVisibleViewportHeight(): void {
  useEffect(() => {
    if (typeof window === 'undefined') return
    const root = document.documentElement
    const vv = window.visualViewport
    const timers = new Set<number>()
    let frame = 0

    const measure = () => {
      const zoomed = vv ? Math.abs(vv.scale - 1) > 0.01 : false
      const height = vv && !zoomed ? vv.height : window.innerHeight
      const top = vv && !zoomed ? Math.max(0, vv.offsetTop) : 0
      root.style.setProperty(VISIBLE_VIEWPORT_HEIGHT_VAR, `${Math.round(height)}px`)
      root.style.setProperty(VISIBLE_VIEWPORT_TOP_VAR, `${Math.round(top)}px`)
    }

    const settle = () => {
      measure()
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(measure)
      for (const delay of ORIENTATION_SETTLE_DELAYS_MS) {
        const id = window.setTimeout(() => {
          timers.delete(id)
          measure()
        }, delay)
        timers.add(id)
      }
    }

    const portrait = typeof window.matchMedia === 'function' ? window.matchMedia('(orientation: portrait)') : null

    measure()
    vv?.addEventListener('resize', measure)
    vv?.addEventListener('scroll', measure)
    window.addEventListener('resize', measure)
    window.addEventListener('orientationchange', settle)
    portrait?.addEventListener('change', settle)
    return () => {
      vv?.removeEventListener('resize', measure)
      vv?.removeEventListener('scroll', measure)
      window.removeEventListener('resize', measure)
      window.removeEventListener('orientationchange', settle)
      portrait?.removeEventListener('change', settle)
      cancelAnimationFrame(frame)
      for (const id of timers) window.clearTimeout(id)
      root.style.removeProperty(VISIBLE_VIEWPORT_HEIGHT_VAR)
      root.style.removeProperty(VISIBLE_VIEWPORT_TOP_VAR)
    }
  }, [])
}
