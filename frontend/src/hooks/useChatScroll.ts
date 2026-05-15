import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'

// CTR-0092 Chat Scroll Behavior (PRP-0055)
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
 */
export function useChatScroll(streamingKey: string): UseChatScrollResult {
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLDivElement>(null)
  const autoscrollRef = useRef(true)
  const [showButton, setShowButton] = useState(false)
  const [bottomSpacerHeightPx, setBottomSpacerHeightPx] = useState<number>(96)

  // Auto-scroll on streaming delta (only when user has not interrupted).
  // Using ref for autoscroll to avoid re-renders disrupting the effect.
  const prevKeyRef = useRef('')
  if (prevKeyRef.current !== streamingKey) {
    prevKeyRef.current = streamingKey
    requestAnimationFrame(() => {
      const el = scrollRef.current
      if (el && autoscrollRef.current) {
        el.scrollTop = el.scrollHeight
      }
    })
  }

  // Track user scroll intent. A single near-bottom threshold governs
  // both "user is reading earlier text" and "user voluntarily returned".
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return

    const updateAutoscroll = () => {
      const near = isNearBottom(el)
      autoscrollRef.current = near
      setShowButton(!near)
    }

    const onUserIntent = () => {
      // Browser updates scrollTop synchronously by the time these
      // events fire, so a direct check is sufficient.
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

    // Initial state
    updateAutoscroll()

    return () => {
      el.removeEventListener('scroll', onUserIntent)
      el.removeEventListener('wheel', onUserIntent)
      el.removeEventListener('touchmove', onUserIntent)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  // Observe ChatInput height so the bottom spacer keeps the final
  // visible message above the floating input even when the input grows.
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
    const el = scrollRef.current
    if (!el) return
    autoscrollRef.current = true
    el.scrollTop = el.scrollHeight
    setShowButton(false)
  }, [])

  return {
    scrollRef,
    inputRef,
    showScrollToBottomButton: showButton,
    bottomSpacerHeightPx,
    scrollToBottom,
  }
}
