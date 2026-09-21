import { CircleAlert, CircleCheck, ListTodo, Loader2, Maximize2, Repeat } from 'lucide-react'
import { useState } from 'react'
import { HarnessTodoDialog } from '@/components/HarnessTodoDialog'
import { harnessDoneCount, harnessStateText } from '@/lib/harnessProgress'
import { cn } from '@/lib/utils'
import type { HarnessProgress } from '@/types/chat'

/**
 * Harness run progress indicator (PRP-0181, CTR-0197 v2, UDR-0163 D4).
 *
 * Rendered inside the assistant message, above the answer text, so it appears on every
 * surface that renders the message (/chat, /popup, /sidebar, phone). It makes the
 * agent loop's automatic continuation visible -- which iteration of how many, how many
 * tasks are done, and why the turn ended -- and opens the Todo dialog. The loop's own
 * injected messages ("Progress so far", "Continue working ...") no longer reach the
 * answer text (UDR-0163 D3); this indicator is where that information lives now.
 */
export function HarnessProgressPanel({
  progress,
  live,
  agentName,
  className,
}: {
  progress: HarnessProgress
  live: boolean
  agentName?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const done = harnessDoneCount(progress)
  const total = progress.todos.length
  const running = progress.state === 'running'
  const problem = progress.state === 'cap_reached' || progress.state === 'stopped'
  return (
    <div
      className={cn(
        'rounded-md border p-2 text-xs',
        problem ? 'border-amber-500/40 bg-amber-500/10' : 'bg-muted/40',
        className,
      )}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-muted-foreground">
        <span className="inline-flex items-center gap-1 font-medium">
          {running ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : progress.state === 'completed' ? (
            <CircleCheck className="h-3.5 w-3.5 text-primary" />
          ) : problem ? (
            <CircleAlert className="h-3.5 w-3.5 text-amber-600" />
          ) : (
            <ListTodo className="h-3.5 w-3.5" />
          )}
          Tasks {done}/{total} done
        </span>
        {progress.iteration > 1 || running ? (
          <span className="inline-flex items-center gap-1" title="The agent loop continues while tasks are open">
            <Repeat className="h-3 w-3" />
            auto-continue {progress.iteration}/{progress.max_iterations}
          </span>
        ) : null}
        {progress.mode && <span>{progress.mode}</span>}
        <button
          type="button"
          onClick={() => setOpen(true)}
          title="Show the task list"
          className="ml-auto inline-flex items-center gap-1 rounded px-1 py-0.5 text-[10px] hover:bg-accent hover:text-foreground">
          <Maximize2 className="h-3 w-3" />
          Tasks
        </button>
      </div>
      {!running && (
        <p className={cn('mt-1', problem && 'text-amber-700 dark:text-amber-400')}>{harnessStateText(progress)}</p>
      )}
      <HarnessTodoDialog open={open} onOpenChange={setOpen} progress={progress} live={live} agentName={agentName} />
    </div>
  )
}
