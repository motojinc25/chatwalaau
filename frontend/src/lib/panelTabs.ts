/**
 * Adaptive panel tab titles (PRP-0213, UDR-0195 D10 / D11).
 *
 * The tab strip measures what its tabs need and picks a level by its OWN width, not
 * by fixed breakpoints: `full` (icon + label on every tab), `compact` (label on the
 * active tab only) or `icons` (icons only). Every tab keeps its name as `title` and
 * `aria-label` at every level; first-letter labels are never used.
 */

export type TabLevel = 'full' | 'compact' | 'icons'

/**
 * The level for an `available` width, given each tab's measured width with its label
 * (`full[i]`) and as an icon only (`icon[i]`), and the active tab's index.
 */
export function chooseTabLevel(available: number, full: number[], icon: number[], active: number): TabLevel {
  const sum = (values: number[]) => values.reduce((total, value) => total + value, 0)
  if (available <= 0 || full.length === 0) return 'full'
  if (sum(full) <= available) return 'full'
  const index = active >= 0 && active < full.length ? active : 0
  if (sum(icon) - (icon[index] ?? 0) + (full[index] ?? 0) <= available) return 'compact'
  return 'icons'
}

/** Whether tab `index` shows its label at `level`. */
export function showsLabel(level: TabLevel, index: number, active: number): boolean {
  return level === 'full' || (level === 'compact' && index === active)
}
