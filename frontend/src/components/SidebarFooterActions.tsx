import { MoreHorizontal } from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

/**
 * Sidebar Footer Action Row (CTR-0220, PRP-0185, UDR-0167 D13/D14).
 *
 * The footer row of the SessionSidebar is where every management surface is launched
 * from. It was an IMPLICIT arrangement: ten manager UI contracts each said "a
 * sidebar-footer icon", and no contract owned the row, so a new launcher had to
 * describe itself relative to a neighbour ("immediately RIGHT of the Ontology icon",
 * CTR-0215). PRP-0185 adds two more members -- MCP and Skills management, moved out of
 * the chat composer -- which is what makes the row a real shared affordance with a
 * real overflow problem, so it gets an owner.
 *
 * WHY A MENU AND NOT ARROWS (D14). The row's membership is DYNAMIC: most entries are
 * probe-gated (Ontology, Webhooks, Pipeline, Files, Cron) and several are withheld on
 * a narrow viewport by ENTRY_POLICY (UDR-0153 D3), so the count changes at runtime and
 * between deployments. Scroll arrows that appear and disappear with those probes are
 * harder to learn than one control that is always in the same place; a menu also stays
 * keyboard reachable without inventing a scroll interaction.
 *
 * EVERY NODE STAYS MOUNTED, IN ONE STABLE POSITION. This is the correctness rule of
 * this component, not a detail. Each item's `node` is a manager that owns BOTH its
 * trigger and its modal, so unmounting the node closes whatever it had open. The first
 * implementation rendered overflowed nodes INSIDE `DropdownMenuContent`, and that
 * content is unmounted whenever the menu closes -- so opening App Settings from the
 * "..." menu and then switching browser windows (which makes Radix close the menu)
 * destroyed the modal mid-edit. It is the same failure UDR-0115 D1 recorded when a
 * collapsible sidebar unmounted the Declarative Agents listener.
 *
 * So overflow is PRESENTATIONAL ONLY: every node renders in the row, always, and an
 * overflowed one is merely HIDDEN (`display:none`, which keeps it mounted and keeps
 * any dialog it opened portalled on screen). The menu holds plain rows that forward a
 * click to the hidden trigger. Reaching for that trigger through the DOM is
 * deliberate: the alternative is giving three manager components an imperative open()
 * API, which would spread this component's layout problem into contracts that have
 * nothing to do with it.
 *
 * Measurement, not a fixed cap: every footer item is the same size (an h-6 w-6 icon
 * button in a gap-1 row), which is what makes a width calculation reliable here rather
 * than fragile. The caller passes items in priority order -- earliest survives inline
 * longest -- and anything that does not fit KEEPS its icon in the menu and gains the
 * label that icon could not carry alone inline.
 *
 * The row does NOT decide membership. A caller that must not render an entry (a failed
 * probe, a wide-only entry on a phone) simply does not pass it; this component never
 * consults ENTRY_POLICY itself, so surface policy stays in one place.
 */

export interface FooterAction {
  /** Stable key; also the React key. */
  id: string
  /** Human label, shown in the "..." menu once the item no longer fits inline. */
  label: string
  /**
   * The same glyph the inline trigger carries, for the menu row.
   *
   * The menu is not a different set of entries, it is the SAME row continued, so an
   * entry must not change its appearance by being collapsed: an operator who has
   * learned an icon should recognise it in the menu instead of having to re-read a
   * name. The icon cannot be lifted out of `node` -- that node is an opaque manager
   * component for four of the entries -- so the caller states it once more here.
   */
  icon?: ReactNode
  /** The trigger itself -- usually an icon Button, or a self-probing manager trigger. */
  node: ReactNode
}

/** One h-6 (24px) icon button plus the gap-1 (4px) that follows it. */
const ITEM_WIDTH = 28

interface SidebarFooterActionsProps {
  items: FooterAction[]
  className?: string
}

export function SidebarFooterActions({ items, className }: SidebarFooterActionsProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const slotRefs = useRef(new Map<string, HTMLSpanElement | null>())
  // Start by showing everything: on the first paint, before a measurement exists,
  // rendering nothing would flash an empty footer and rendering only the menu would
  // hide every launcher on a wide viewport that has room for all of them.
  const [inlineCount, setInlineCount] = useState(items.length)

  const measure = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const width = el.clientWidth
    if (width <= 0) return
    const fits = Math.floor(width / ITEM_WIDTH)
    // Everything fits: no menu, so the full width is available.
    if (fits >= items.length) {
      setInlineCount(items.length)
      return
    }
    // Otherwise the trailing menu occupies one slot of its own.
    setInlineCount(Math.max(0, Math.min(items.length, fits - 1)))
  }, [items.length])

  useEffect(() => {
    measure()
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [measure])

  /** Forward a menu selection to the still-mounted, visually hidden trigger. */
  const openFromMenu = useCallback((id: string) => {
    const slot = slotRefs.current.get(id)
    slot?.querySelector<HTMLElement>('button, [role="button"]')?.click()
  }, [])

  const overflow = items.slice(inlineCount)

  return (
    <div ref={containerRef} className={className}>
      <div className="flex items-center justify-end gap-1">
        {items.map((item, index) => (
          <span
            key={item.id}
            ref={(el) => {
              slotRefs.current.set(item.id, el)
            }}
            // HIDDEN, never unmounted (see the module note). `hidden` is display:none,
            // so the trigger keeps its DOM node -- which is what `openFromMenu` clicks
            // -- and a dialog it already opened stays portalled and visible.
            className={cn('shrink-0', index < inlineCount ? 'inline-flex' : 'hidden')}>
            {item.node}
          </span>
        ))}
        {overflow.length > 0 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 shrink-0 text-muted-foreground"
                aria-label={`More tools (${overflow.length})`}>
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              {overflow.map((item) => (
                <DropdownMenuItem key={item.id} className="gap-2" onSelect={() => openFromMenu(item.id)}>
                  {item.icon ? (
                    <span className="flex h-4 w-4 shrink-0 items-center justify-center text-muted-foreground">
                      {item.icon}
                    </span>
                  ) : null}
                  {item.label}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </div>
  )
}
