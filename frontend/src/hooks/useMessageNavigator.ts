import { type RefObject, useCallback, useEffect, useMemo, useState } from 'react'
import { extractPlainText } from '@/lib/extractPlainText'
import type { ChatMessage } from '@/types/chat'

// CTR-0103 Message Navigator UI (PRP-0072 / UDR-0048)
// Show once there is real back-and-forth (>= 2 user turns). Long-form chats
// often have few turns but very long answers -- exactly when navigation helps --
// so the floor is intentionally low (UDR-0048 D7, tuned from 4 to 2 per usage).
export const MIN_TURNS_TO_SHOW = 2
export const CHAT_COLUMN_MAX_PX = 768 // max-w-3xl chat column width
export const RAIL_GUTTER_MIN_PX = 48 // min right-gutter width to host the rail
export const PREVIEW_MAX_CHARS = 60 // user-message preview truncation
export const SCROLL_TOP_OFFSET_PX = 16 // gap above the target message after a jump

export interface NavTurn {
  messageId: string
  messageIndex: number
  preview: string
}

interface UseMessageNavigatorResult {
  turns: NavTurn[]
  activeId: string | null
  isAvailable: boolean
  scrollToTurn: (messageId: string) => void
}

function buildPreview(content: string): string {
  const text = extractPlainText(content ?? '')
  if (text.length <= PREVIEW_MAX_CHARS) return text
  return `${text.slice(0, PREVIEW_MAX_CHARS).trimEnd()}…`
}

/**
 * CTR-0103: builds an ordered index of USER turns, computes the responsive
 * availability gate from the measured right gutter (UDR-0048 D6), tracks the
 * active turn via IntersectionObserver (scrollspy), and exposes a smooth
 * scroll-to-turn action. Overlay-only; owns no auto-scroll policy (CTR-0092).
 *
 * Resolves user-message DOM nodes by the `data-message-role="user"` /
 * `data-message-id` attributes rendered by ChatMessageItem.
 */
export function useMessageNavigator(
  scrollRef: RefObject<HTMLDivElement | null>,
  messages: ChatMessage[],
  options: { enabled: boolean },
): UseMessageNavigatorResult {
  const { enabled } = options
  const [gutterOk, setGutterOk] = useState(false)
  const [activeId, setActiveId] = useState<string | null>(null)

  const turns = useMemo<NavTurn[]>(() => {
    const result: NavTurn[] = []
    messages.forEach((msg, index) => {
      if (msg.role !== 'user') return
      const preview = buildPreview(msg.content)
      if (!preview) return
      result.push({ messageId: msg.id, messageIndex: index, preview })
    })
    return result
  }, [messages])

  // Responsive gutter gate (UDR-0048 D6). Measure the scroll container width
  // via ResizeObserver so a sidebar open/close or window resize re-evaluates.
  useEffect(() => {
    if (!enabled) {
      setGutterOk(false)
      return
    }
    const el = scrollRef.current
    if (!el) return
    if (typeof ResizeObserver === 'undefined') {
      // Fallback: approximate the gutter test via viewport width.
      setGutterOk(window.matchMedia('(min-width: 1024px)').matches)
      return
    }
    const measure = () => {
      const gutter = (el.clientWidth - CHAT_COLUMN_MAX_PX) / 2
      setGutterOk(gutter >= RAIL_GUTTER_MIN_PX)
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [enabled, scrollRef])

  const isAvailable = enabled && gutterOk && turns.length >= MIN_TURNS_TO_SHOW

  // Scrollspy via IntersectionObserver over the user-message nodes. The active
  // turn is the topmost user node currently within the upper viewport band;
  // when none intersects, the previous active turn is retained (the user is
  // reading the answer that followed it).
  useEffect(() => {
    if (!isAvailable) {
      setActiveId(null)
      return
    }
    const root = scrollRef.current
    if (!root || typeof IntersectionObserver === 'undefined') return

    const nodes = Array.from(root.querySelectorAll<HTMLElement>('[data-message-role="user"][data-message-id]'))
    if (nodes.length === 0) return

    const visible = new Set<string>()
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const id = (entry.target as HTMLElement).dataset.messageId
          if (!id) continue
          if (entry.isIntersecting) visible.add(id)
          else visible.delete(id)
        }
        // First user turn (document order) still visible in the band.
        const next = turns.find((t) => visible.has(t.messageId))?.messageId
        if (next) setActiveId(next)
      },
      { root, rootMargin: `-${SCROLL_TOP_OFFSET_PX}px 0px -60% 0px`, threshold: 0 },
    )
    for (const node of nodes) io.observe(node)
    return () => io.disconnect()
  }, [isAvailable, turns, scrollRef])

  const scrollToTurn = useCallback(
    (messageId: string) => {
      const root = scrollRef.current
      if (!root) return
      const node = Array.from(root.querySelectorAll<HTMLElement>('[data-message-id]')).find(
        (n) => n.dataset.messageId === messageId,
      )
      if (!node) return
      // Robust against offsetParent ambiguity: scroll by the measured delta.
      const delta = node.getBoundingClientRect().top - root.getBoundingClientRect().top
      root.scrollTo({ top: Math.max(0, root.scrollTop + delta - SCROLL_TOP_OFFSET_PX), behavior: 'smooth' })
      setActiveId(messageId)
    },
    [scrollRef],
  )

  return { turns, activeId, isAvailable, scrollToTurn }
}
