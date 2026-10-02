/**
 * The two folder-cue switches (PRP-0196, UDR-0178 D5, CTR-0198 / CTR-0199).
 *
 * `folder_glow_enabled` and `folder_badge_enabled` are App Settings that only the SPA
 * reads. They are read once from GET /api/app-settings when the chat page mounts, and
 * re-taken from APP_SETTINGS_CHANGED_EVENT after every save on the App Settings screen,
 * so a change applies without a reload (`runtime` scope, UDR-0120 D3).
 *
 * Any failure -- no settings store, 4xx / 5xx, a network error, a missing or non-boolean
 * value -- means ON, the default. A deployment without a store keeps the feature.
 */

import { useEffect, useState } from 'react'

/** Dispatched by the App Settings screen after a successful save; `detail` is the status. */
export const APP_SETTINGS_CHANGED_EVENT = 'chatwalaau:app-settings-changed'

export interface FolderCueSettings {
  glow: boolean
  badge: boolean
}

const DEFAULTS: FolderCueSettings = { glow: true, badge: true }

function fromValues(values: Record<string, unknown> | undefined): FolderCueSettings {
  return {
    glow: typeof values?.folder_glow_enabled === 'boolean' ? values.folder_glow_enabled : DEFAULTS.glow,
    badge: typeof values?.folder_badge_enabled === 'boolean' ? values.folder_badge_enabled : DEFAULTS.badge,
  }
}

export function useFolderCueSettings(): FolderCueSettings {
  const [cue, setCue] = useState<FolderCueSettings>(DEFAULTS)

  useEffect(() => {
    let cancelled = false
    fetch('/api/app-settings')
      .then((res) => (res.ok ? (res.json() as Promise<{ settings?: Record<string, unknown> }>) : null))
      .then((status) => {
        if (!cancelled && status) setCue(fromValues(status.settings))
      })
      .catch(() => undefined)

    const onChanged = (event: Event) => {
      const status = (event as CustomEvent<{ settings?: Record<string, unknown> } | undefined>).detail
      if (status) setCue(fromValues(status.settings))
    }
    window.addEventListener(APP_SETTINGS_CHANGED_EVENT, onChanged)
    return () => {
      cancelled = true
      window.removeEventListener(APP_SETTINGS_CHANGED_EVENT, onChanged)
    }
  }, [])

  return cue
}
