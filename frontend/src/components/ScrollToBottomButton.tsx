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
//
// v0.117.3: hidden with the `inert` attribute, NOT `aria-hidden`. Clicking the
// button resumes autoscroll, which flips `visible` false on the button that still
// holds DOM focus; `aria-hidden` on a focused element is a spec violation the
// browser refuses to honor ("Blocked aria-hidden on an element because its
// descendant retained focus"). `inert` hides the subtree from assistive technology
// AND blurs the element via focus fixup. `tabIndex` remains as a fallback for
// engines without `inert`. Same reasoning as MessageStepButton, its cluster sibling.
export function ScrollToBottomButton({ visible, onClick, className }: ScrollToBottomButtonProps) {
  return (
    <Button
      variant="secondary"
      size="icon"
      onClick={onClick}
      aria-label="Scroll to bottom"
      inert={!visible}
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
