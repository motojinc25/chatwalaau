import * as DialogPrimitive from '@radix-ui/react-dialog'
import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * Edge overlay panel (PRP-0171, CTR-0016; PRP-0176 adds the bottom side). Built on the
 * existing @radix-ui/react-dialog primitive -- focus trap, Escape and backdrop dismissal
 * come from it -- so no new dependency is added. `side` defaults to "left", which is the
 * session sidebar's presentation; "bottom" is the narrow run-target picker (CTR-0216).
 */
const Sheet = DialogPrimitive.Root

const SheetContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & { title: string; side?: 'left' | 'bottom' }
>(({ className, children, title, side = 'left', ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/50 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
    <DialogPrimitive.Content
      ref={ref}
      aria-describedby={undefined}
      className={cn(
        'fixed z-50 flex flex-col bg-background shadow-lg duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out',
        side === 'left'
          ? 'left-0 top-[var(--app-visible-top,0px)] h-[var(--app-visible-height,100dvh)] data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left'
          : // Anchored to the BOTTOM EDGE OF THE VISIBLE VIEWPORT (not of the layout
            // viewport), so Safari's toolbar and the iOS keyboard never cover it
            // (UDR-0153 D11/D12). The distance from the layout bottom is
            // 100dvh - (visible top + visible height).
            'inset-x-0 top-auto bottom-[calc(100dvh-var(--app-visible-top,0px)-var(--app-visible-height,100dvh))] max-h-[85%] rounded-t-xl border-t data-[state=closed]:slide-out-to-bottom data-[state=open]:slide-in-from-bottom',
        className,
      )}
      {...props}>
      <DialogPrimitive.Title className="sr-only">{title}</DialogPrimitive.Title>
      {children}
    </DialogPrimitive.Content>
  </DialogPrimitive.Portal>
))
SheetContent.displayName = 'SheetContent'

export { Sheet, SheetContent }
