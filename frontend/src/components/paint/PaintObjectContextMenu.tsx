/**
 * Shared object context menu (CTR-0160 v3, PRP-0125 / UDR-0108 D6).
 *
 * The SAME item set is offered from the layers-panel row and from a right-click
 * on the canvas, so there is one vocabulary for object manipulation. Built on
 * `@radix-ui/react-context-menu`, which is already a dependency (added by
 * PRP-0091 for the File Explorer tree) -- no new package, and the interaction
 * matches the File Explorer.
 */
import {
  ArrowDown,
  ArrowDownToLine,
  ArrowUp,
  ArrowUpToLine,
  Clipboard,
  Copy,
  CopyPlus,
  Eye,
  EyeOff,
  Lock,
  Scissors,
  Trash2,
  Unlock,
} from 'lucide-react'
import type { ReactNode } from 'react'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu'

export interface PaintObjectActions {
  onDuplicate: () => void
  onCut: () => void
  onCopy: () => void
  onPaste: () => void
  onBringToFront: () => void
  onBringForward: () => void
  onSendBackward: () => void
  onSendToBack: () => void
  onToggleLock: () => void
  onToggleVisible: () => void
  onDelete: () => void
}

interface PaintObjectContextMenuProps extends PaintObjectActions {
  children: ReactNode
  /** False when the menu is opened over empty canvas -- only Paste applies. */
  hasTarget: boolean
  canPaste: boolean
  locked: boolean
  visible: boolean
  asChild?: boolean
  onOpen?: (event: React.MouseEvent) => void
}

const shortcut = 'ml-auto pl-6 text-[10px] tabular-nums text-zinc-400'

export function PaintObjectContextMenu({
  children,
  hasTarget,
  canPaste,
  locked,
  visible,
  asChild,
  onOpen,
  onDuplicate,
  onCut,
  onCopy,
  onPaste,
  onBringToFront,
  onBringForward,
  onSendBackward,
  onSendToBack,
  onToggleLock,
  onToggleVisible,
  onDelete,
}: PaintObjectContextMenuProps) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild={asChild} onContextMenu={onOpen}>
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent className="w-52">
        <ContextMenuItem disabled={!hasTarget} onSelect={onDuplicate}>
          <CopyPlus className="mr-2 h-3.5 w-3.5" /> Duplicate <span className={shortcut}>Ctrl+D</span>
        </ContextMenuItem>
        <ContextMenuItem disabled={!hasTarget} onSelect={onCut}>
          <Scissors className="mr-2 h-3.5 w-3.5" /> Cut <span className={shortcut}>Ctrl+X</span>
        </ContextMenuItem>
        <ContextMenuItem disabled={!hasTarget} onSelect={onCopy}>
          <Copy className="mr-2 h-3.5 w-3.5" /> Copy <span className={shortcut}>Ctrl+C</span>
        </ContextMenuItem>
        <ContextMenuItem disabled={!canPaste} onSelect={onPaste}>
          <Clipboard className="mr-2 h-3.5 w-3.5" /> Paste <span className={shortcut}>Ctrl+V</span>
        </ContextMenuItem>

        <ContextMenuSeparator />

        <ContextMenuItem disabled={!hasTarget} onSelect={onBringToFront}>
          <ArrowUpToLine className="mr-2 h-3.5 w-3.5" /> Bring to front
        </ContextMenuItem>
        <ContextMenuItem disabled={!hasTarget} onSelect={onBringForward}>
          <ArrowUp className="mr-2 h-3.5 w-3.5" /> Bring forward
        </ContextMenuItem>
        <ContextMenuItem disabled={!hasTarget} onSelect={onSendBackward}>
          <ArrowDown className="mr-2 h-3.5 w-3.5" /> Send backward
        </ContextMenuItem>
        <ContextMenuItem disabled={!hasTarget} onSelect={onSendToBack}>
          <ArrowDownToLine className="mr-2 h-3.5 w-3.5" /> Send to back
        </ContextMenuItem>

        <ContextMenuSeparator />

        <ContextMenuItem disabled={!hasTarget} onSelect={onToggleLock}>
          {locked ? <Unlock className="mr-2 h-3.5 w-3.5" /> : <Lock className="mr-2 h-3.5 w-3.5" />}
          {locked ? 'Unlock' : 'Lock'}
        </ContextMenuItem>
        <ContextMenuItem disabled={!hasTarget} onSelect={onToggleVisible}>
          {visible ? <EyeOff className="mr-2 h-3.5 w-3.5" /> : <Eye className="mr-2 h-3.5 w-3.5" />}
          {visible ? 'Hide' : 'Show'}
        </ContextMenuItem>

        <ContextMenuSeparator />

        <ContextMenuItem
          disabled={!hasTarget}
          onSelect={onDelete}
          className="text-red-600 focus:bg-red-50 focus:text-red-700">
          <Trash2 className="mr-2 h-3.5 w-3.5" /> Delete <span className={shortcut}>Del</span>
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}
