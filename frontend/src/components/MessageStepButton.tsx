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
 */
export function MessageStepButton({ direction, visible, onClick }: MessageStepButtonProps) {
  const isPrev = direction === 'prev'
  return (
    <Button
      variant="secondary"
      size="icon"
      onClick={onClick}
      aria-label={isPrev ? 'Previous message' : 'Next message'}
      aria-hidden={!visible}
      tabIndex={visible ? 0 : -1}
      className={cn(
        'h-9 w-9 rounded-full border bg-background shadow-md transition-opacity duration-120',
        visible ? 'opacity-100' : 'pointer-events-none opacity-0',
      )}>
      {isPrev ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
    </Button>
  )
}
