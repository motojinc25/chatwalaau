/**
 * PermissionsDisabledBanner (CTR-0100 part 2, PRP-0067, UDR-0043 D3).
 *
 * Persistent banner rendered in the SessionSidebar header whenever
 * `GET /api/auth/status` reports `tool_approval_mode === "skip"`. The
 * banner is unconditionally visible while skip mode is active so the
 * operator can never accidentally forget that the safety belt is off.
 */

import { ShieldOff } from 'lucide-react'

export function PermissionsDisabledBanner() {
  return (
    <div
      role="alert"
      className="mb-3 flex items-start gap-2 rounded-md border border-red-500/60 bg-red-50/70 px-2.5 py-2 text-xs leading-snug text-red-900 dark:bg-red-950/30 dark:text-red-200">
      <ShieldOff className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <div>
        <div className="font-semibold">Tool approval is DISABLED.</div>
      </div>
    </div>
  )
}
