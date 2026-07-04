import { type RefObject, useCallback, useEffect, useMemo, useState } from 'react'
import { SCROLL_TOP_OFFSET_PX } from '@/hooks/useMessageNavigator'
import type { ChatMessage } from '@/types/chat'

// CTR-0168 Message Step Navigation UI (PRP-0101 / UDR-0081). Realizes the
// per-message previous/next navigation CTR-0092 explicitly deferred. Distinct
// from the CTR-0103 rail: it steps through EVERY message (user AND assistant),
// not just user turns, and is gated purely by overflow (a scrollbar is present),
// independent of the CTR-0092 Scroll-to-Bottom near-bottom gate (UDR-0081 D3).
export const MIN_MESSAGES_TO_STEP = 2

// Threshold slack (px) so a container that exactly fits is not treated as
// overflowing.
const OVERFLOW_EPS_PX = 4

export interface MessageStepNavApi {
  /** overflow AND at least MIN_MESSAGES_TO_STEP messages (UDR-0081 D3). */
  isAvailable: boolean
  canPrev: boolean
  canNext: boolean
  stepPrev: () => void
  stepNext: () => void
}

/**
 * CTR-0168: observes the chat scroll container to (a) derive `isOverflowing`
 * (ResizeObserver + scroll listener), and (b) track the current (topmost visible)
 * message via an IntersectionObserver scrollspy over the CTR-0103
 * `data-message-id` nodes. Exposes prev/next steps that smooth-scroll the
 * container to the adjacent message. Owns no auto-scroll policy (CTR-0092 remains
 * the sole owner); a step fires a scroll event which legitimately suspends
 * auto-scroll (UDR-0081 D4).
 */
export function useMessageStepNav(
  scrollRef: RefObject<HTMLDivElement | null>,
  messages: ChatMessage[],
): MessageStepNavApi {
  const [isOverflowing, setIsOverflowing] = useState(false)
  const [currentId, setCurrentId] = useState<string | null>(null)

  const messageCount = messages.length

  // (a) Overflow detection: a scrollbar is present when the content is taller than
  // the viewport. Re-run when the message list changes (streaming grows it).
  // biome-ignore lint/correctness/useExhaustiveDependencies: messageCount is the re-fire trigger; a new message grows scrollHeight (the container box does not resize), so overflow is recomputed here.
  useEffect(() => {
    const root = scrollRef.current
    if (!root) return
    const update = () => setIsOverflowing(root.scrollHeight > root.clientHeight + OVERFLOW_EPS_PX)
    update()
    const ro = new ResizeObserver(update)
    ro.observe(root)
    root.addEventListener('scroll', update, { passive: true })
    return () => {
      ro.disconnect()
      root.removeEventListener('scroll', update)
    }
  }, [scrollRef, messageCount])

  // (b) Scrollspy: the topmost message (DOM order) currently intersecting the
  // viewport is the "current" message; prev/next are its neighbors.
  // biome-ignore lint/correctness/useExhaustiveDependencies: messageCount is the re-fire trigger; new messages add DOM nodes the observer must re-observe.
  useEffect(() => {
    const root = scrollRef.current
    if (!root) return
    const nodes = Array.from(root.querySelectorAll<HTMLElement>('[data-message-id]'))
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
        const ordered = Array.from(root.querySelectorAll<HTMLElement>('[data-message-id]'))
        const topmost = ordered.find((n) => n.dataset.messageId && visible.has(n.dataset.messageId))
        if (topmost?.dataset.messageId) setCurrentId(topmost.dataset.messageId)
      },
      { root, rootMargin: `-${SCROLL_TOP_OFFSET_PX}px 0px -60% 0px`, threshold: 0 },
    )
    for (const node of nodes) io.observe(node)
    return () => io.disconnect()
  }, [scrollRef, messageCount])

  const orderedIds = useMemo(() => messages.map((m) => m.id), [messages])

  // Fall back to the first message before the scrollspy has resolved a node, so
  // the cluster is never shown with both buttons dead.
  const resolvedIndex = currentId ? orderedIds.indexOf(currentId) : -1
  const currentIndex = resolvedIndex >= 0 ? resolvedIndex : 0

  const scrollToId = useCallback(
    (id: string) => {
      const root = scrollRef.current
      if (!root) return
      const node = Array.from(root.querySelectorAll<HTMLElement>('[data-message-id]')).find(
        (n) => n.dataset.messageId === id,
      )
      if (!node) return
      const delta = node.getBoundingClientRect().top - root.getBoundingClientRect().top
      root.scrollTo({ top: Math.max(0, root.scrollTop + delta - SCROLL_TOP_OFFSET_PX), behavior: 'smooth' })
      setCurrentId(id)
    },
    [scrollRef],
  )

  const canPrev = currentIndex > 0
  const canNext = currentIndex < orderedIds.length - 1

  const stepPrev = useCallback(() => {
    if (currentIndex > 0) scrollToId(orderedIds[currentIndex - 1])
  }, [currentIndex, orderedIds, scrollToId])

  const stepNext = useCallback(() => {
    if (currentIndex < orderedIds.length - 1) scrollToId(orderedIds[currentIndex + 1])
  }, [currentIndex, orderedIds, scrollToId])

  return {
    isAvailable: isOverflowing && messageCount >= MIN_MESSAGES_TO_STEP,
    canPrev,
    canNext,
    stepPrev,
    stepNext,
  }
}
