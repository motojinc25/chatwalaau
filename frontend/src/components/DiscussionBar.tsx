// AI Discussion bar (CTR-0276, PRP-0232 2.7): status, round and budget of the chat's latest
// run, the moderator's question while it waits, and only the actions its status allows. The
// person's decision is shown apart from the AI's recommendation (UDR-0215 D10).
import { Check, CircleHelp, FastForward, Lightbulb, Loader2, Pause, Play, Repeat, Square, Users, X } from 'lucide-react'
import { useState } from 'react'
import { DiscussionContinueDialog } from '@/components/DiscussionContinueDialog'
import { DiscussionExportActions } from '@/components/DiscussionExport'
import { DiscussionSelectionTrace } from '@/components/DiscussionParts'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { DiscussionController } from '@/hooks/useDiscussion'
import { cn } from '@/lib/utils'

const TERMINAL_STATUSES = new Set(['completed', 'cancelled', 'failed'])

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

// Why auto mode handed control back (PRP-0234, UDR-0217 D4).
const AUTO_STOPPED: Record<string, string> = {
  paused: 'Auto mode stopped by Pause.',
}

const REPORT_SKIPPED: Record<string, string> = {
  budget: 'The full report was skipped: the token budget could not hold it.',
  failed: 'The full report could not be written.',
}

const VERDICT_LABELS = { adopt: 'Adopted', hold: 'On hold', rediscuss: 'To be discussed again' } as const

export function DiscussionBar({ discussion, compact }: { discussion: DiscussionController; compact?: boolean }) {
  const run = discussion.latest
  const [askTarget, setAskTarget] = useState('')
  const [askText, setAskText] = useState('')
  const [continueOpen, setContinueOpen] = useState(false)
  const [changingSpeakers, setChangingSpeakers] = useState(false)
  if (!run) return null

  // Hold and Re-discuss lead straight to the Continue dialog (PRP-0233, UDR-0216 D5).
  const decideThenContinue = async (verdict: 'hold' | 'rediscuss') => {
    if (await discussion.decide(verdict)) setContinueOpen(true)
  }

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
  // In auto mode a finish suggestion is only shown; the rounds go on (PRP-0234, UDR-0217 D4).
  const autoSuggestion = run.advisory?.suggest
    ? { reason: run.advisory.reason }
    : run.state?.suggest_finish?.value
      ? { reason: run.state.suggest_finish.reason }
      : null
  // Speaker selection (PRP-0235, UDR-0218 D4 / D5): this round's and the next round's choice.
  const selecting = run.speaker_selection === 'decision'
  const currentRound = run.phase.startsWith('round:') ? run.phase.slice(6) : null
  const currentSelection = selecting && currentRound ? run.selections?.[currentRound] : undefined
  const nextRound = run.pending_request?.phase.startsWith('round:') ? run.pending_request.phase.slice(6) : null
  const nextSelection = selecting && nextRound ? run.selections?.[nextRound] : undefined
  const chosenNext = discussion.nextSpeakers ?? nextSelection?.speakers ?? []
  const toggleNext = (id: string) => {
    const next = chosenNext.includes(id) ? chosenNext.filter((s) => s !== id) : [...chosenNext, id]
    if (next.length > 0) discussion.setNextSpeakers(run.participants.map((p) => p.id).filter((s) => next.includes(s)))
  }

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
        {run.auto && !TERMINAL_STATUSES.has(run.status) && (
          <span
            className="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2 py-px text-[11px] font-medium text-blue-700 dark:text-blue-300"
            title="The rounds run without asking; Pause hands control back"
            data-testid="discussion-auto">
            <FastForward className="h-3 w-3" aria-hidden="true" />
            Auto
          </span>
        )}
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
            {status === 'summarizing'
              ? 'The moderator writes the summary.'
              : run.auto
                ? 'The rounds run without asking. Pause to step in.'
                : 'The participants are speaking.'}
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
          {status === 'running' && currentSelection && (
            <div className="w-full">
              <DiscussionSelectionTrace
                record={currentSelection}
                participants={run.participants}
                label={`This round (${currentSelection.round})`}
              />
            </div>
          )}
          {run.auto && status === 'running' && autoSuggestion && (
            <p
              className="flex w-full items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300"
              data-testid="discussion-auto-suggestion">
              <Lightbulb className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
              The moderator suggests summarizing{autoSuggestion.reason ? `: ${autoSuggestion.reason}` : '.'} Auto mode
              keeps to the rounds; Pause to step in.
            </p>
          )}
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
          {run.auto_stopped && AUTO_STOPPED[run.auto_stopped] && (
            <p className="text-xs text-blue-700 dark:text-blue-300" data-testid="discussion-auto-stopped">
              {AUTO_STOPPED[run.auto_stopped]}
            </p>
          )}
          {run.advisory?.suggest && (
            <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300">
              <Lightbulb className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
              The moderator suggests summarizing{run.advisory.reason ? `: ${run.advisory.reason}` : '.'}
            </p>
          )}
          {selecting && nextRound && (
            <div className="space-y-1" data-testid="discussion-next-speakers">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium">Next round:</span>
                <span>
                  {run.participants
                    .filter((p) => chosenNext.includes(p.id))
                    .map((p) => p.display_name)
                    .join(', ') || 'everyone'}
                  {discussion.nextSpeakers ? ' (chosen by you)' : ''}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 px-2 text-xs"
                  onClick={() => setChangingSpeakers((v) => !v)}
                  disabled={disabled}>
                  {changingSpeakers ? 'Done' : 'Change'}
                </Button>
              </div>
              {changingSpeakers && (
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  {run.participants.map((p) => (
                    <label key={p.id} className="flex items-center gap-1">
                      <input
                        type="checkbox"
                        aria-label={`${p.display_name} speaks next round`}
                        checked={chosenNext.includes(p.id)}
                        onChange={() => toggleNext(p.id)}
                      />
                      {p.display_name}
                    </label>
                  ))}
                </div>
              )}
              {nextSelection && !discussion.nextSpeakers && (
                <DiscussionSelectionTrace
                  record={nextSelection}
                  participants={run.participants}
                  label={`Round ${nextSelection.round}`}
                />
              )}
            </div>
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
              title="Continue, then run the remaining rounds without asking"
              onClick={() => void discussion.respond('auto')}
              disabled={disabled}>
              <FastForward className="mr-1 h-3 w-3" />
              Run the rest automatically
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
            {run.report_skipped && REPORT_SKIPPED[run.report_skipped] ? ` ${REPORT_SKIPPED[run.report_skipped]}` : ''}
          </p>
          {selecting && discussion.participation.length > 0 && (
            <p className="text-xs text-muted-foreground" data-testid="discussion-participation">
              Rounds spoken:{' '}
              {discussion.participation
                .map((p) => `${p.display_name} ${p.rounds_spoken} / ${p.rounds_held}`)
                .join(', ')}
            </p>
          )}
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
                  onClick={() => void decideThenContinue('hold')}
                  disabled={disabled}>
                  Hold
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7"
                  onClick={() => void decideThenContinue('rediscuss')}
                  disabled={disabled}>
                  Re-discuss
                </Button>
              </div>
            )
          )}
        </div>
      )}

      {!readOnly && !discussion.streaming && !discussion.open && discussion.threadId && (
        <div className="mt-2">
          <DiscussionExportActions threadId={discussion.threadId} runId={run.id} />
        </div>
      )}

      {!readOnly && !discussion.streaming && !discussion.open && (
        <div className="mt-2 flex items-center gap-2">
          <Button size="sm" variant="outline" className="h-7" onClick={() => setContinueOpen(true)} disabled={disabled}>
            <Repeat className="mr-1 h-3 w-3" />
            Continue discussion
          </Button>
          <span className="text-xs text-muted-foreground">Starts a new discussion from this conclusion.</span>
        </div>
      )}

      {!readOnly && (
        <DiscussionContinueDialog
          open={continueOpen}
          onOpenChange={setContinueOpen}
          run={run}
          error={discussion.error}
          onContinue={(body) => discussion.continueDiscussion(body)}
        />
      )}

      {readOnly && discussion.open && (
        <p className="mt-2 text-xs text-muted-foreground">
          Open this chat in the main window to continue the discussion.
        </p>
      )}
    </section>
  )
}
