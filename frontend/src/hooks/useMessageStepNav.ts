import { type RefObject, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { SCROLL_TOP_OFFSET_PX } from '@/hooks/useMessageNavigator'
import { type MessageSpan, readingIndex, STEP_EDGE_SLACK_PX } from '@/lib/messageStep'
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

// Upper bound (ms) for a step's smooth scroll; `scrollend` normally ends it sooner.
const STEP_SETTLE_MS = 1000

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
  const orderedIds = useMemo(() => messages.map((m) => m.id), [messages])
  // Re-subscribe the scrollspy when the rendered message set changes, not only its
  // size: the same count with different ids (an edit, a Live marker replaced by its
  // answer) mounts new nodes the observer would never see.
  const idsKey = orderedIds.join('|')

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
  // viewport is the "current" message; it drives which buttons are shown. The band
  // starts STEP_EDGE_SLACK_PX BELOW the landing line: a step puts the target's top
  // on that line, so the message above it ends there, and IntersectionObserver
  // reports an edge-adjacent target as intersecting -- it would win as "topmost".
  // biome-ignore lint/correctness/useExhaustiveDependencies: idsKey is the re-fire trigger; a changed message set mounts DOM nodes the observer must re-observe.
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
      { root, rootMargin: `-${SCROLL_TOP_OFFSET_PX + STEP_EDGE_SLACK_PX}px 0px -60% 0px`, threshold: 0 },
    )
    for (const node of nodes) io.observe(node)
    return () => io.disconnect()
  }, [scrollRef, idsKey])

  // Fall back to the first message before the scrollspy has resolved a node, so
  // the cluster is never shown with both buttons dead.
  const resolvedIndex = currentId ? orderedIds.indexOf(currentId) : -1
  const currentIndex = resolvedIndex >= 0 ? resolvedIndex : 0

  // The target of a step whose smooth scroll is still running. A second click in
  // that window steps from it: measuring would still find the message being left.
  const pendingRef = useRef<{ id: string; timer: number } | null>(null)
  const clearPending = useCallback(() => {
    if (pendingRef.current) window.clearTimeout(pendingRef.current.timer)
    pendingRef.current = null
  }, [])
  useEffect(() => {
    const root = scrollRef.current
    if (!root) return
    root.addEventListener('scrollend', clearPending)
    // A user's own scroll ends the step: the next one starts from what they read.
    root.addEventListener('wheel', clearPending, { passive: true })
    root.addEventListener('touchmove', clearPending, { passive: true })
    return () => {
      root.removeEventListener('scrollend', clearPending)
      root.removeEventListener('wheel', clearPending)
      root.removeEventListener('touchmove', clearPending)
      clearPending()
    }
  }, [scrollRef, clearPending])

  const scrollToId = useCallback(
    (id: string) => {
      const root = scrollRef.current
      if (!root) return
      const node = Array.from(root.querySelectorAll<HTMLElement>('[data-message-id]')).find(
        (n) => n.dataset.messageId === id,
      )
      if (!node) return
      const delta = node.getBoundingClientRect().top - root.getBoundingClientRect().top
      const top = Math.max(0, root.scrollTop + delta - SCROLL_TOP_OFFSET_PX)
      clearPending()
      // Only a scroll that will actually move is pending (no scrollend otherwise).
      if (Math.abs(top - root.scrollTop) >= 1) {
        pendingRef.current = { id, timer: window.setTimeout(clearPending, STEP_SETTLE_MS) }
      }
      root.scrollTo({ top, behavior: 'smooth' })
      setCurrentId(id)
    },
    [scrollRef, clearPending],
  )

  const canPrev = currentIndex > 0
  const canNext = currentIndex < orderedIds.length - 1

  // A step starts from the message MEASURED at the reading line when the button is
  // pressed, not from the scrollspy state: the observer reports asynchronously
  // (mid smooth-scroll it can still name the message just left), so a step taken
  // from it could skip a message or repeat one. While a step is still scrolling,
  // the next one starts from its target; the scrollspy value is the fallback when
  // nothing can be measured.
  const measuredIndex = useCallback((): number => {
    const pending = pendingRef.current ? orderedIds.indexOf(pendingRef.current.id) : -1
    if (pending >= 0) return pending
    const root = scrollRef.current
    if (!root) return currentIndex
    const rootTop = root.getBoundingClientRect().top
    const nodes = Array.from(root.querySelectorAll<HTMLElement>('[data-message-id]'))
    const spans: MessageSpan[] = nodes.map((node) => {
      const rect = node.getBoundingClientRect()
      return { top: rect.top - rootTop, bottom: rect.bottom - rootTop }
    })
    const index = readingIndex(spans, SCROLL_TOP_OFFSET_PX)
    const id = index >= 0 ? nodes[index].dataset.messageId : undefined
    const resolved = id ? orderedIds.indexOf(id) : -1
    return resolved >= 0 ? resolved : currentIndex
  }, [scrollRef, orderedIds, currentIndex])

  const stepPrev = useCallback(() => {
    const index = measuredIndex()
    if (index > 0) scrollToId(orderedIds[index - 1])
  }, [measuredIndex, orderedIds, scrollToId])

  const stepNext = useCallback(() => {
    const index = measuredIndex()
    if (index < orderedIds.length - 1) scrollToId(orderedIds[index + 1])
  }, [measuredIndex, orderedIds, scrollToId])

  return {
    isAvailable: isOverflowing && messageCount >= MIN_MESSAGES_TO_STEP,
    canPrev,
    canNext,
    stepPrev,
    stepNext,
  }
}
