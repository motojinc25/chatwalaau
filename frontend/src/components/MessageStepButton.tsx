import { ChevronDown, ChevronUp } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface MessageStepButtonProps {
  direction: 'prev' | 'next'
  visible: boolean
  onClick: () => void
}

/**
 * CTR-0168 Message Step Navigation UI (PRP-0101 / UDR-0081): a single previous /
 * next step button flanking the CTR-0092 Scroll-to-Bottom button. Styled to match
 * ScrollToBottomButton. When not applicable (at the first / last message) the
 * button reserves its space but is invisible + inert, keeping the cluster centered.
 *
 * v0.117.3: the inert half is expressed with the `inert` attribute, NOT
 * `aria-hidden`. Stepping to the last (or first) message flips `visible` false on
 * the very button the user just clicked -- which still holds DOM focus -- and
 * `aria-hidden` on a focused element is a spec violation the browser refuses to
 * honor ("Blocked aria-hidden on an element because its descendant retained
 * focus"). `inert` is the attribute for this: it hides the subtree from assistive
 * technology AND runs the focus fixup that blurs the element, so the warning
 * cannot occur. `tabIndex` is kept as a fallback for engines without `inert`.
 */
export function MessageStepButton({ direction, visible, onClick }: MessageStepButtonProps) {
  const isPrev = direction === 'prev'
  return (
    <Button
      variant="secondary"
      size="icon"
      onClick={onClick}
      aria-label={isPrev ? 'Previous message' : 'Next message'}
      inert={!visible}
      tabIndex={visible ? 0 : -1}
      className={cn(
        'h-9 w-9 rounded-full border bg-background shadow-md transition-opacity duration-120',
        visible ? 'opacity-100' : 'pointer-events-none opacity-0',
      )}>
      {isPrev ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
    </Button>
  )
}
