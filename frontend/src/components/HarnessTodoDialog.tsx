import { Circle, CircleCheck, Repeat } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { harnessDoneCount, harnessStateText } from '@/lib/harnessProgress'
import { cn } from '@/lib/utils'
import type { HarnessProgress } from '@/types/chat'

/**
 * Harness run Todo dialog (PRP-0181, CTR-0197 v2, UDR-0163 D4/D5).
 *
 * Opened from the inline HarnessProgressPanel. While the turn streams it re-renders
 * from every `harness_progress` snapshot; on a reloaded chat it renders the persisted
 * `usage.harness_run` record and says so -- the record is the state at the END of that
 * turn, not the agent's current list (the harness session lives in server memory).
 * Full-screen on a narrow viewport, a regular dialog otherwise.
 */
export function HarnessTodoDialog({
  open,
  onOpenChange,
  progress,
  live,
  agentName,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  progress: HarnessProgress
  /** True while the turn is still streaming; false for a finished or reloaded turn. */
  live: boolean
  agentName?: string
}) {
  const done = harnessDoneCount(progress)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto max-sm:h-[100dvh] max-sm:max-h-none sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Harness run{agentName ? `: ${agentName}` : ''}</DialogTitle>
          <DialogDescription>
            {live ? 'Live -- updates as the agent works.' : 'Tasks at the end of this turn.'}
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border bg-muted/40 px-3 py-2 text-xs">
          {progress.mode && (
            <span>
              mode: <span className="font-medium">{progress.mode}</span>
            </span>
          )}
          <span className="inline-flex items-center gap-1">
            <Repeat className="h-3 w-3" />
            iteration {progress.iteration} of {progress.max_iterations}
          </span>
          <span>
            {done}/{progress.todos.length} done
          </span>
          <span className="w-full font-medium text-muted-foreground">{harnessStateText(progress)}</span>
        </div>
        {progress.todos.length === 0 ? (
          <p className="text-sm text-muted-foreground">No tasks yet.</p>
        ) : (
          <ol className="space-y-1 text-sm">
            {progress.todos.map((todo) => (
              <li key={todo.id} className="flex items-start gap-2">
                {todo.done ? (
                  <CircleCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                ) : (
                  <Circle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                <span className="w-6 shrink-0 text-right tabular-nums text-muted-foreground">{todo.id}</span>
                <span className={cn('break-words', todo.done && 'text-muted-foreground line-through')}>
                  {todo.title}
                </span>
              </li>
            ))}
          </ol>
        )}
        {progress.todos_truncated && (
          <p className="text-xs text-muted-foreground">Only the first {progress.todos.length} tasks are shown.</p>
        )}
      </DialogContent>
    </Dialog>
  )
}
