/**
 * Shared session datetime formatting (CTR-0016 v5, PRP-0112 Part 3 / UDR-0091 D11+D12).
 *
 * There used to be two independent formatters -- one in SessionSidebar, one in
 * SessionSearchDialog -- displaying the same field of the same object. Two
 * formatters can drift; one cannot. Every surface that renders a session
 * timestamp MUST import from here (UDR-0091 D12).
 */

/**
 * Format a session ISO timestamp as `YYYY/MM/DD HH:mm` in the viewer's LOCAL time.
 *
 * Fully locale-INDEPENDENT (UDR-0091 D11). Composed from the local date parts
 * rather than `toLocaleString`, deliberately:
 *
 * - `toLocaleString` pins the hour with `hourCycle: 'h23'` but still lets the
 *   locale choose the DATE order, so an `en-US` browser renders `07/13/2026, 15:45`
 *   while `ja-JP` renders `2026/07/13 15:45`. The same session then looks different
 *   on two machines, which is exactly the drift D11 exists to remove.
 * - 24-hour, and never `24:00` (a real `hour12: false` hazard in some locales).
 * - Seconds are deliberately dropped: no sidebar decision depends on them, and they
 *   consumed the horizontal budget the message/image count icons need. This is the
 *   one intentional information loss in PRP-0112.
 *
 * The TIME ZONE stays local -- a chat's timestamp should read as the user's own
 * clock; only the field ORDER is fixed.
 *
 * An empty or unparseable input yields an empty string, never `Invalid Date`.
 */
export function formatSessionDateTime(iso: string): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  const y = date.getFullYear()
  const mo = pad(date.getMonth() + 1)
  const d = pad(date.getDate())
  const h = pad(date.getHours())
  const mi = pad(date.getMinutes())
  return `${y}/${mo}/${d} ${h}:${mi}`
}
