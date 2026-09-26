import { AudioLines, ChevronRight, Clock, Loader2, X } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { ChatMessage } from '@/types/chat'

/**
 * A Live delegation that is still queued or running (PRP-0188 step 3, CTR-0228).
 *
 * It sits in the conversation where the answer will appear -- the final answer takes
 * its place (same message id). The streamed progress (the agent's text so far and its
 * tool calls) flows underneath but stays COLLAPSED, so the chat does not fill with a
 * half-written answer; "Show progress" opens it. Cancel stops the task (a queued one
 * never starts).
 *
 * v0.169.0 (operator feedback): one session runs ONE delegation at a time, so a queued
 * task can sit there for a while. Waiting and working must not look alike -- a waiting
 * marker is amber with a clock, a working one teal with a spinner -- and the step it
 * has reached is spelled out, because "queued -> started -> done" was only implied by
 * a line of text before.
 */
const STEPS = [
  { id: 'queued', label: 'Queued' },
  { id: 'started', label: 'Working' },
  { id: 'done', label: 'Answer' },
] as const

export function LiveDelegationMarker({
  message,
  onCancel,
}: {
  message: ChatMessage
  onCancel?: (delegationId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const live = message.live
  const state = live?.state ?? 'queued'
  const waiting = state === 'queued'
  const cancelling = state === 'cancelling'
  const label = cancelling
    ? 'Cancelling...'
    : waiting
      ? 'Waiting for the previous task to finish'
      : 'Working on a task from the Live conversation'
  const reached = cancelling ? 1 : waiting ? 0 : 1
  const tools = message.toolCalls ?? []
  const hasProgress = Boolean(message.content) || tools.length > 0

  return (
    <div className="flex w-full justify-start py-2" data-live-delegation={live?.delegation_id}>
      <div
        className={cn(
          'w-full max-w-3xl rounded-lg border px-3 py-2 text-sm',
          // Waiting and working are deliberately different colours.
          cancelling
            ? 'border-muted-foreground/30 bg-muted/40'
            : waiting
              ? 'border-amber-500/50 bg-amber-500/5'
              : 'border-cyan-500/40 bg-cyan-500/5',
        )}>
        <div className="flex items-center gap-2">
          {waiting ? (
            <Clock className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
          ) : (
            <Loader2
              className={cn(
                'h-3.5 w-3.5 shrink-0 animate-spin',
                cancelling ? 'text-muted-foreground' : 'text-cyan-600 dark:text-cyan-400',
              )}
            />
          )}
          <AudioLines
            className={cn(
              'h-3.5 w-3.5 shrink-0',
              waiting ? 'text-amber-600 dark:text-amber-400' : 'text-cyan-600 dark:text-cyan-400',
            )}
          />
          <span className="min-w-0 flex-1 truncate text-muted-foreground">{label}</span>
          {tools.length > 0 && (
            <span className="shrink-0 text-[11px] text-muted-foreground">
              {tools.filter((t) => t.status === 'completed').length}/{tools.length} tools
            </span>
          )}
          {onCancel && live?.delegation_id && !cancelling && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 shrink-0 px-2 text-xs"
              onClick={() => onCancel(live.delegation_id as string)}
              aria-label="Cancel this task"
              title="Cancel this task">
              <X className="mr-1 h-3 w-3" />
              Cancel
            </Button>
          )}
        </div>

        {/* Where it has got to: queued -> working -> answer. */}
        <ol className="mt-1.5 flex items-center gap-1 text-[11px]" aria-label={`Task state: ${label}`}>
          {STEPS.map((step, i) => {
            const done = i < reached
            const current = i === reached
            return (
              <li key={step.id} className="flex items-center gap-1">
                {i > 0 && <ChevronRight className="h-3 w-3 text-muted-foreground/50" aria-hidden="true" />}
                <span
                  className={cn(
                    'rounded px-1.5 py-px',
                    current && waiting && 'bg-amber-500/15 font-medium text-amber-700 dark:text-amber-300',
                    current && !waiting && 'bg-cyan-500/15 font-medium text-cyan-700 dark:text-cyan-300',
                    done && 'text-muted-foreground line-through',
                    !current && !done && 'text-muted-foreground/60',
                  )}>
                  {step.label}
                </span>
              </li>
            )
          })}
        </ol>

        {hasProgress && (
          <div className="mt-1">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
              <ChevronRight className={cn('h-3 w-3 transition-transform', open && 'rotate-90')} />
              {open ? 'Hide progress' : 'Show progress'}
            </button>
            {open && (
              <div className="mt-1 max-h-64 overflow-y-auto rounded border bg-background/60 p-2 text-xs">
                {tools.length > 0 && (
                  <ul className="mb-1 space-y-0.5">
                    {tools.map((t) => (
                      <li key={t.id} className="flex items-center gap-1 text-muted-foreground">
                        {t.status === 'completed' ? (
                          <span className="text-emerald-600">done</span>
                        ) : (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        )}
                        <span className="font-mono">{t.name}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {message.content && (
                  <p className="whitespace-pre-wrap break-words text-muted-foreground">{message.content}</p>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
