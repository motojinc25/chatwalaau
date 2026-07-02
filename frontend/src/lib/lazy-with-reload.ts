import { type ComponentType, lazy } from 'react'

const RELOAD_FLAG = '__chatwalaau_chunk_reloaded'

/**
 * `React.lazy` with one-time auto-recovery from a STALE dynamic import.
 *
 * After the frontend is rebuilt / redeployed, the content-hashed chunk filenames change.
 * A browser tab still holding the previous `index.html` then requests an old chunk hash
 * that no longer exists on disk and the import rejects with "Failed to fetch dynamically
 * imported module" (HTTP 404). On that failure we force a single full reload so the
 * browser fetches the fresh index + matching chunks. A `sessionStorage` guard prevents a
 * reload loop if the asset is genuinely missing (then the original error is re-thrown so
 * the Suspense error boundary can surface it).
 */
// biome-ignore lint/suspicious/noExplicitAny: matches React.lazy's own ComponentType<any> bound.
export function lazyWithReload<T extends ComponentType<any>>(factory: () => Promise<{ default: T }>) {
  return lazy(async () => {
    try {
      const mod = await factory()
      sessionStorage.removeItem(RELOAD_FLAG)
      return mod
    } catch (err) {
      if (!sessionStorage.getItem(RELOAD_FLAG)) {
        sessionStorage.setItem(RELOAD_FLAG, '1')
        window.location.reload()
        // Keep the Suspense fallback visible while the page reloads.
        return new Promise<{ default: T }>(() => {})
      }
      throw err
    }
  })
}
