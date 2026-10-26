// AI Discussion bar (CTR-0276, PRP-0232 2.7): status, round and budget of the chat's latest
// run, the moderator's question while it waits, and only the actions its status allows. The
// person's decision is shown apart from the AI's recommendation (UDR-0215 D10).
import { Check, CircleHelp, Lightbulb, Loader2, Pause, Play, Repeat, Square, Users, X } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { DiscussionController } from '@/hooks/useDiscussion'
import { cn } from '@/lib/utils'

const STATUS_LABELS: Record<string, string> = {
  created: 'Starting',
  running: 'Discussing',
  waiting_human: 'Waiting for you',
  paused: 'Paused',
  summarizing: 'Summarizing',
  completed: 'Completed',
  cancelled: 'Cancelled',
  failed: 'Failed',
}

const REASONS: Record<string, string> = {
  human_finished: 'you asked for the summary',
  max_rounds: 'all rounds were held',
  budget: 'the token budget ran out',
  time: 'the time per phase ran out',
  cancelled: 'it was cancelled',
}

const PAUSE_REASONS: Record<string, string> = {
  user: 'You paused it.',
  interrupted: 'The connection closed while a participant was speaking. Nothing unfinished was saved.',
  error: 'It stopped on an error.',
}

const VERDICT_LABELS = { adopt: 'Adopted', hold: 'On hold', rediscuss: 'To be discussed again' } as const

export function DiscussionBar({ discussion, compact }: { discussion: DiscussionController; compact?: boolean }) {
  const run = discussion.latest
  const [askTarget, setAskTarget] = useState('')
  const [askText, setAskText] = useState('')
  if (!run) return null

  const status = discussion.streaming && run.status !== 'summarizing' ? 'running' : run.status
  const used = run.budget.used_tokens
  const total = run.budget.token_budget
  const reserve = run.budget.summary_reserve
  const usedPct = Math.min(100, Math.round((used / Math.max(total, 1)) * 100))
  const reservePct = Math.min(100, Math.round((reserve / Math.max(total, 1)) * 100))
  const roundShown = run.phase.startsWith('round:') ? Number(run.phase.slice(6)) : run.round
  const disabled = discussion.busy
  // A read-only surface (/popup, /sidebar) shows the state but offers no action (PRP-0232 2.7).
  const readOnly = Boolean(compact)

  return (
    <section
      aria-label="AI discussion"
      className="mx-auto mb-2 w-full max-w-3xl rounded-lg border bg-card px-3 py-2 text-sm shadow-xs"
      data-testid="discussion-bar">
      <div className="flex flex-wrap items-center gap-2">
        <Users className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        <span className="font-medium">AI discussion</span>
        <span
          className={cn(
            'rounded-full px-2 py-px text-[11px] font-medium',
            status === 'waiting_human' && 'bg-primary/10 text-primary',
            status === 'running' || status === 'summarizing' || status === 'created'
              ? 'bg-blue-500/10 text-blue-700 dark:text-blue-300'
              : '',
            status === 'paused' && 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
            status === 'completed' && 'bg-green-500/10 text-green-700 dark:text-green-300',
            (status === 'cancelled' || status === 'failed') && 'bg-muted text-muted-foreground',
          )}
          data-testid="discussion-status">
          {(status === 'running' || status === 'summarizing') && (
            <Loader2 className="mr-1 inline h-3 w-3 animate-spin" aria-hidden="true" />
          )}
          {STATUS_LABELS[status] ?? status}
        </span>
        <span className="text-xs text-muted-foreground">
          Round {Math.min(roundShown, run.rounds)} / {run.rounds}
        </span>
        <div
          className="ml-auto flex items-center gap-2 text-[11px] text-muted-foreground"
          title={`${used.toLocaleString()} of ${total.toLocaleString()} tokens used; ${reserve.toLocaleString()} kept for the summary`}>
          <div className="relative h-1.5 w-24 overflow-hidden rounded-full bg-muted" aria-hidden="true">
            <div className="absolute inset-y-0 left-0 bg-primary/70" style={{ width: `${usedPct}%` }} />
            <div className="absolute inset-y-0 right-0 bg-amber-500/40" style={{ width: `${reservePct}%` }} />
          </div>
          <span className="tabular-nums">{usedPct}% of budget</span>
        </div>
      </div>

      {discussion.error && (
        <div role="alert" className="mt-2 flex items-start gap-2 text-xs text-red-600">
          <span className="flex-1">{discussion.error}</span>
          <button type="button" onClick={discussion.dismissError} aria-label="Dismiss" className="shrink-0">
            <X className="h-3 w-3" />
          </button>
        </div>
      )}

      {!readOnly && (status === 'running' || status === 'summarizing') && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {status === 'summarizing' ? 'The moderator writes the summary.' : 'The participants are speaking.'}
          </span>
          <Button
            size="sm"
            variant="outline"
            className="ml-auto h-7"
            onClick={() => void discussion.pause()}
            disabled={disabled}>
            <Pause className="mr-1 h-3 w-3" />
            Pause
          </Button>
        </div>
      )}

      {!readOnly && discussion.waiting && run.pending_request && (
        <div className="mt-2 space-y-2" data-testid="discussion-question">
          <p className="flex items-start gap-1.5">
            <CircleHelp className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" aria-hidden="true" />
            <span>
              <span className="font-medium">Moderator: </span>
              {run.pending_request.question}
            </span>
          </p>
          {run.advisory?.suggest && (
            <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300">
              <Lightbulb className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
              The moderator suggests summarizing{run.advisory.reason ? `: ${run.advisory.reason}` : '.'}
            </p>
          )}
          <p className="text-xs text-muted-foreground">Type your input in the box below, or:</p>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" className="h-7" onClick={() => void discussion.respond('continue')} disabled={disabled}>
              <Play className="mr-1 h-3 w-3" />
              Continue
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-7"
              onClick={() => void discussion.respond('finish')}
              disabled={disabled}>
              <Square className="mr-1 h-3 w-3" />
              Summarize and finish
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7"
              onClick={() => void discussion.cancel()}
              disabled={disabled}>
              Cancel discussion
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Participant to ask"
              className="h-7 rounded-md border bg-background px-2 text-xs"
              value={askTarget}
              onChange={(e) => setAskTarget(e.target.value)}>
              <option value="">Ask a participant...</option>
              {run.participants.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name}
                </option>
              ))}
            </select>
            {askTarget && (
              <>
                <Input
                  aria-label="Question for the participant"
                  className="h-7 min-w-0 flex-1 text-xs"
                  value={askText}
                  onChange={(e) => setAskText(e.target.value)}
                  placeholder="Your question"
                />
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7"
                  disabled={disabled || !askText.trim()}
                  onClick={() => {
                    void discussion.respond('ask', askText.trim(), askTarget).then((ok) => {
                      if (ok) {
                        setAskText('')
                        setAskTarget('')
                      }
                    })
                  }}>
                  Ask
                </Button>
              </>
            )}
          </div>
        </div>
      )}

      {!readOnly && !discussion.streaming && (run.status === 'paused' || run.status === 'created') && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {(run.pause_reason && PAUSE_REASONS[run.pause_reason]) || ''}
            {run.error ? ` ${run.error}` : ''}
          </span>
          <Button size="sm" className="ml-auto h-7" onClick={() => void discussion.resume()} disabled={disabled}>
            <Play className="mr-1 h-3 w-3" />
            Continue
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7"
            onClick={() => void discussion.cancel()}
            disabled={disabled}>
            Cancel discussion
          </Button>
        </div>
      )}

      {run.status === 'completed' && (
        <div className="mt-2 space-y-2">
          <p className="text-xs text-muted-foreground">
            Ended because {REASONS[run.termination_reason ?? ''] ?? 'it finished'}. The summary above is the AI&apos;s
            recommendation; the decision is yours.
          </p>
          {run.human_decision ? (
            <p className="flex items-center gap-1.5 text-sm" data-testid="discussion-decision">
              <Check className="h-3.5 w-3.5 text-green-600" aria-hidden="true" />
              <span className="font-medium">Your decision:</span> {VERDICT_LABELS[run.human_decision.verdict]}
              {run.human_decision.note ? ` -- ${run.human_decision.note}` : ''}
            </p>
          ) : (
            !readOnly && (
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" className="h-7" onClick={() => void discussion.decide('adopt')} disabled={disabled}>
                  Adopt
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7"
                  onClick={() => void discussion.decide('hold')}
                  disabled={disabled}>
                  Hold
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7"
                  onClick={() => void discussion.decide('rediscuss')}
                  disabled={disabled}>
                  Re-discuss
                </Button>
              </div>
            )
          )}
        </div>
      )}

      {!readOnly && !discussion.streaming && !discussion.open && (
        <div className="mt-2 flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-7"
            onClick={() => void discussion.continueDiscussion()}
            disabled={disabled}>
            <Repeat className="mr-1 h-3 w-3" />
            Continue discussion
          </Button>
          <span className="text-xs text-muted-foreground">Starts a new discussion with the same participants.</span>
        </div>
      )}

      {readOnly && discussion.open && (
        <p className="mt-2 text-xs text-muted-foreground">
          Open this chat in the main window to continue the discussion.
        </p>
      )}
    </section>
  )
}
