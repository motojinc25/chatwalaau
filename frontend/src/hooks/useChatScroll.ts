import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

// CTR-0092 Chat Scroll Behavior (PRP-0055, v4 per PRP-0058)
export const NEAR_BOTTOM_PX = 64
export const INPUT_GAP_PX = 24

interface UseChatScrollResult {
  scrollRef: RefObject<HTMLDivElement | null>
  inputRef: RefObject<HTMLDivElement | null>
  showScrollToBottomButton: boolean
  bottomSpacerHeightPx: number
  scrollToBottom: () => void
}

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - NEAR_BOTTOM_PX
}

/**
 * CTR-0092: Chat scroll strategy. Drives auto-scroll on streaming
 * deltas, suspends it on user upward scroll intent, exposes a
 * Scroll-to-Bottom trigger, and observes ChatInput height so the
 * scroll container reserves a clean spacer above the floating input.
 *
 * `streamingKey` should be a string that changes on every streaming
 * delta (message length / last content length / tool-call count etc.)
 * so the effect knows when to re-apply auto-scroll.
 *
 * v4 timing model (PRP-0058 follow-up #2):
 * - Body autoscroll lives in useLayoutEffect on streamingKey, so it
 *   runs AFTER React commits the new content (scrollHeight is current)
 *   and BEFORE the browser paints (no flicker).
 * - Spacer-settle re-anchor lives in useLayoutEffect on
 *   bottomSpacerHeightPx, same timing rationale.
 * - scrollToBottom() defers the actual scrollTop assignment to
 *   requestAnimationFrame so it runs AFTER any pending state updates
 *   (e.g., the setMessages queued by ChatPanel.handleSend) commit.
 *   This is critical: assigning scrollTop = scrollHeight while React
 *   has new content pending would scroll to the OLD scrollHeight, and
 *   the async scroll event that follows would compute isNearBottom
 *   against the NEW (bigger) scrollHeight, flip autoscrollRef to
 *   false, and block every subsequent streaming-delta autoscroll.
 */
export function useChatScroll(streamingKey: string): UseChatScrollResult {
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLDivElement>(null)
  const autoscrollRef = useRef(true)
  const [showButton, setShowButton] = useState(false)
  const [bottomSpacerHeightPx, setBottomSpacerHeightPx] = useState<number>(96)

  // Body autoscroll on streaming delta. useLayoutEffect runs synchronously
  // after DOM mutations (scrollHeight reflects the new content) and before
  // the browser paints. Gated on autoscrollRef so user-suspended scroll is
  // preserved.
  // biome-ignore lint/correctness/useExhaustiveDependencies: streamingKey is the re-fire trigger; its value is intentionally not read inside the effect body (scrollHeight from the DOM already reflects the new content the key encodes)
  useLayoutEffect(() => {
    if (!autoscrollRef.current) return
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [streamingKey])

  // PRP-0058 UX-3 (v4): re-anchor at bottom when the spacer settles after
  // mount or grows mid-session. Without this, the body autoscroll uses the
  // default spacer (96 px), then the ResizeObserver-driven spacer growth
  // leaves scroll position above the new bottom (browsers do NOT re-anchor
  // when scrollHeight grows). Gated on autoscrollRef.
  // biome-ignore lint/correctness/useExhaustiveDependencies: bottomSpacerHeightPx is the re-fire trigger; its value is intentionally not read inside the effect (the new scrollHeight already reflects the new spacer height via the DOM)
  useLayoutEffect(() => {
    if (!autoscrollRef.current) return
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [bottomSpacerHeightPx])

  // Track user scroll intent. A single near-bottom threshold governs both
  // "user is reading earlier text" and "user voluntarily returned".
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return

    const updateAutoscroll = () => {
      const near = isNearBottom(el)
      autoscrollRef.current = near
      setShowButton(!near)
    }

    const onUserIntent = () => {
      updateAutoscroll()
    }

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'PageUp' || e.key === 'Home' || e.key === 'ArrowUp') {
        onUserIntent()
      }
    }

    el.addEventListener('scroll', onUserIntent, { passive: true })
    el.addEventListener('wheel', onUserIntent, { passive: true })
    el.addEventListener('touchmove', onUserIntent, { passive: true })
    window.addEventListener('keydown', onKey)

    // PRP-0058 UX-2/UX-3 root cause fix (v3): no initial updateAutoscroll()
    // call. At mount, scrollTop=0 and scrollHeight=full content height; for
    // any tall session that would pin autoscrollRef=false BEFORE the body
    // autoscroll could fire. autoscrollRef starts true via useRef(true);
    // the synthetic scroll event from the body useLayoutEffect's
    // scrollTop assignment drives updateAutoscroll() to true (we ARE near
    // the bottom right after the assignment lands). showButton default is
    // already false via useState(false), so no initial setState is needed.

    return () => {
      el.removeEventListener('scroll', onUserIntent)
      el.removeEventListener('wheel', onUserIntent)
      el.removeEventListener('touchmove', onUserIntent)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  // Observe ChatInput height so the bottom spacer keeps the final visible
  // message above the floating input even when the input grows.
  useEffect(() => {
    const node = inputRef.current
    if (!node || typeof ResizeObserver === 'undefined') return

    const measure = () => {
      const h = node.getBoundingClientRect().height
      setBottomSpacerHeightPx(Math.max(0, h) + INPUT_GAP_PX)
    }

    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(node)
    return () => ro.disconnect()
  }, [])

  const scrollToBottom = useCallback(() => {
    // Synchronous: re-arm autoscroll so the body useLayoutEffect that fires
    // on the next streamingKey change (e.g., from a just-called
    // sendMessage) actually scrolls. showButton clears immediately for UI
    // feedback.
    autoscrollRef.current = true
    setShowButton(false)
    // PRP-0058 follow-up #2: defer the actual scroll to rAF so any pending
    // React render commits first. Otherwise we scroll to the OLD
    // scrollHeight (before the queued setMessages applies), and the async
    // scroll event sees the NEW (bigger) scrollHeight and flips
    // autoscrollRef back to false via updateAutoscroll, killing every
    // subsequent streaming-delta autoscroll.
    requestAnimationFrame(() => {
      const el = scrollRef.current
      if (!el) return
      el.scrollTop = el.scrollHeight
    })
  }, [])

  return {
    scrollRef,
    inputRef,
    showScrollToBottomButton: showButton,
    bottomSpacerHeightPx,
    scrollToBottom,
  }
}
