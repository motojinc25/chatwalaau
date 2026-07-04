import { ChevronsDown } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface ScrollToBottomButtonProps {
  visible: boolean
  onClick: () => void
  className?: string
}

// CTR-0092 Chat Scroll Behavior (PRP-0055): floating affordance shown
// when autoscroll has been suspended.
export function ScrollToBottomButton({ visible, onClick, className }: ScrollToBottomButtonProps) {
  return (
    <Button
      variant="secondary"
      size="icon"
      onClick={onClick}
      aria-label="Scroll to bottom"
      aria-hidden={!visible}
      tabIndex={visible ? 0 : -1}
      className={cn(
        'h-9 w-9 rounded-full border bg-background shadow-md transition-opacity duration-120',
        visible ? 'opacity-100' : 'pointer-events-none opacity-0',
        className,
      )}>
      <ChevronsDown className="h-4 w-4" />
    </Button>
  )
}
