// Continue the discussion (CTR-0276, PRP-0233, UDR-0216). A continuation starts from the previous
// conclusion and changes something: the person gives a focus (required after "Summarize and finish"
// or a decision, D5), the rounds (1 by default), whether to start with the mutual review (on when a
// conclusion is carried) and the seats (the setup dialog's editor, D4). PRP-0234: auto mode and the
// full report, as in the setup dialog (the copied seats keep their research tools).
import { Loader2, Repeat } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import {
  RunOptionToggles,
  type Seat,
  SeatEditor,
  SpeakerSelectionOption,
  seatRequests,
  seatsFromParticipants,
  useSeatOptions,
} from '@/components/DiscussionSeats'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { ContinueDiscussionRequest } from '@/lib/discussionApi'
import type { DiscussionRun } from '@/types/chat'

const textareaClass =
  'min-h-[72px] w-full resize-y rounded-md border bg-background px-3 py-2 text-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring'

/** What the new run carries over: the final summary's lists, else the last state (as the server does). */
export function carriedOf(run: DiscussionRun): { common: string[]; conflicts: string[]; missing: string[] } {
  const final = run.final
  const state = run.state
  return {
    common: (final?.agreements?.length ? final.agreements : state?.common) ?? [],
    conflicts: (final?.dissent?.length ? final.dissent : state?.conflicts) ?? [],
    missing: (final?.open_issues?.length ? final.open_issues : state?.missing) ?? [],
  }
}

/** The focus is required after "Summarize and finish" or any decision (operator Q2). */
export function focusRequired(run: DiscussionRun): boolean {
  return run.termination_reason === 'human_finished' || run.human_decision !== null
}

const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  run: DiscussionRun
  /** The last request error (shown here, since the dialog covers the bar). */
  error?: string | null
  /** Resolves true when the new run was created. */
  onContinue: (body: ContinueDiscussionRequest) => Promise<boolean>
}

export function DiscussionContinueDialog({ open, onOpenChange, run, error, onContinue }: Props) {
  const { options, agents, error: optionsError } = useSeatOptions(open)
  const initialSeats = useMemo(() => seatsFromParticipants(run.participants), [run.participants])
  const carried = useMemo(() => carriedOf(run), [run])
  const hasCarried = carried.common.length + carried.conflicts.length + carried.missing.length > 0
  const required = focusRequired(run)

  const [focus, setFocus] = useState('')
  const [rounds, setRounds] = useState(1)
  const [skipOpening, setSkipOpening] = useState(true)
  const [seats, setSeats] = useState<Seat[]>(initialSeats)
  const [showCarried, setShowCarried] = useState(false)
  const [auto, setAuto] = useState(false)
  const [report, setReport] = useState(true)
  const [selectionMode, setSelectionMode] = useState<'all' | 'decision'>(run.speaker_selection ?? 'all')
  const [submitting, setSubmitting] = useState(false)
  // A request error is shown only after this dialog's own submit, not an older one from the bar.
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!open) return
    setFocus('')
    setRounds(1)
    setSkipOpening(hasCarried)
    setSeats(initialSeats)
    setShowCarried(false)
    setAuto(false)
    setReport(true)
    setSelectionMode(run.speaker_selection ?? 'all')
    setSubmitted(false)
  }, [open, hasCarried, initialSeats, run.speaker_selection])

  const roundsChoices = useMemo(
    () => Array.from({ length: options?.limits.max_rounds ?? 5 }, (_, i) => i + 1),
    [options],
  )
  const seatsChanged = JSON.stringify(seats) !== JSON.stringify(initialSeats)
  const canContinue = (!required || focus.trim().length > 0) && seats.length >= 2 && !submitting

  const submit = async () => {
    if (!canContinue) return
    setSubmitting(true)
    setSubmitted(true)
    try {
      const ok = await onContinue({
        focus: focus.trim(),
        rounds,
        skip_opening: hasCarried && skipOpening,
        auto,
        report,
        speaker_selection: options?.speaker_selection_available ? selectionMode : 'all',
        // Unchanged seats are not sent: the new run copies the source seats as they were.
        ...(seatsChanged ? { participants: seatRequests(seats, options) } : {}),
      })
      if (ok) onOpenChange(false)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] w-[min(720px,calc(100vw-2rem))] max-w-none flex-col gap-3 sm:max-w-none">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Repeat className="h-4 w-4" aria-hidden="true" />
            Continue the discussion
          </DialogTitle>
          <DialogDescription>
            A new discussion starts from this conclusion. Say what should change, so it does not repeat the same result.
          </DialogDescription>
        </DialogHeader>
        {/* The same stable scrollbar gutter as the setup dialog. */}
        <div className="-mx-1 min-h-0 flex-1 space-y-3 overflow-y-auto overflow-x-hidden px-1 pb-1 [scrollbar-gutter:stable]">
          {run.human_decision?.verdict === 'adopt' && (
            <p className="rounded-md bg-amber-500/10 px-2 py-1.5 text-xs text-amber-800 dark:text-amber-200">
              This result was adopted. Continue only to re-check it under a changed assumption.
            </p>
          )}
          <div className="text-xs text-muted-foreground" data-testid="discussion-carried">
            {hasCarried ? (
              <>
                Carried over: {plural(carried.common.length, 'common point')},{' '}
                {plural(carried.conflicts.length, 'conflict')}, {plural(carried.missing.length, 'open issue')}{' '}
                <button
                  type="button"
                  className="underline underline-offset-2"
                  onClick={() => setShowCarried((v) => !v)}>
                  {showCarried ? 'hide' : 'show'}
                </button>
              </>
            ) : (
              'Nothing is carried over: the previous discussion left no conclusion.'
            )}
          </div>
          {showCarried && hasCarried && (
            <div className="space-y-1.5 rounded-md border px-2 py-1.5 text-xs">
              {(
                [
                  ['Common ground', carried.common],
                  ['Conflicts', carried.conflicts],
                  ['Missing information', carried.missing],
                ] as const
              ).map(([title, items]) =>
                items.length > 0 ? (
                  <div key={title}>
                    <p className="font-medium">{title}</p>
                    <ul className="list-disc pl-4">
                      {items.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </div>
                ) : null,
              )}
            </div>
          )}
          <label className="block space-y-1 text-sm">
            <span className="font-medium">
              Focus{' '}
              <span className="text-xs font-normal text-muted-foreground">
                {required ? '(required)' : '(optional)'}
              </span>
            </span>
            <textarea
              aria-label="Focus"
              className={textareaClass}
              value={focus}
              onChange={(e) => setFocus(e.target.value)}
              placeholder='What should be re-examined? For example: "only the cost objections" or "if the budget were halved"'
              maxLength={4000}
              autoFocus
            />
            <span className="block text-xs text-muted-foreground">
              {required
                ? 'The discussion was summarized or decided: say what should be re-examined.'
                : 'The previous discussion stopped early; leave it empty to carry on where it stopped.'}{' '}
              The focus ranks above the previous conclusion.
            </span>
          </label>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
            <label className="flex items-center gap-2">
              <span className="font-medium">Rounds</span>
              <select
                aria-label="Rounds"
                className="h-8 rounded-md border bg-background px-2 text-sm"
                value={rounds}
                onChange={(e) => setRounds(Number(e.target.value))}>
                {roundsChoices.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
            {hasCarried && (
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  aria-label="Start with the mutual review"
                  checked={skipOpening}
                  onChange={(e) => setSkipOpening(e.target.checked)}
                />
                <span>Start with the mutual review (skip the opening views)</span>
              </label>
            )}
          </div>
          <SeatEditor options={options} agents={agents} seats={seats} onChange={setSeats} />
          <SpeakerSelectionOption
            value={options?.speaker_selection_available ? selectionMode : 'all'}
            available={Boolean(options?.speaker_selection_available)}
            onChange={setSelectionMode}
          />
          <RunOptionToggles auto={auto} report={report} onAuto={setAuto} onReport={setReport} />
          {(optionsError || (submitted && error)) && (
            <p role="alert" className="text-xs text-red-600">
              {optionsError || error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={!canContinue}>
            {submitting && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            Continue
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
