// New AI discussion dialog (CTR-0276, PRP-0232 2.7). The person sets the topic, the wanted
// outcome, the constraints, the seats and the rounds; Start creates the discussion chat and the
// chat page then runs its opening.
import { Loader2, Users } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import {
  CodeFolderOption,
  defaultTools,
  RunOptionToggles,
  type Seat,
  SeatEditor,
  SpeakerSelectionOption,
  seatRequests,
  useSeatOptions,
  withCodeAnalyst,
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
import { Input } from '@/components/ui/input'
import { discussionApi } from '@/lib/discussionApi'

const textareaClass =
  'min-h-[64px] w-full resize-y rounded-md border bg-background px-3 py-2 text-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  folderId?: string | null
  /** The discussion chat was created: open it. */
  onCreated: (threadId: string) => void
}

export function DiscussionSetupDialog({ open, onOpenChange, folderId, onCreated }: Props) {
  const { options, agents, error: optionsError } = useSeatOptions(open)
  const [topic, setTopic] = useState('')
  const [goal, setGoal] = useState('')
  const [constraints, setConstraints] = useState('')
  const [rounds, setRounds] = useState(2)
  const [seats, setSeats] = useState<Seat[]>([])
  const [auto, setAuto] = useState(false)
  const [report, setReport] = useState(true)
  const [selectionMode, setSelectionMode] = useState<'all' | 'decision'>('all')
  // PRP-0236 (UDR-0219 D1): the workspace folder whose code the discussion analyses (null = none).
  const [codeFolder, setCodeFolder] = useState<string | null>(null)
  const [seatsFull, setSeatsFull] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setError(null)
    setCodeFolder(null)
    setSeatsFull(false)
    if (!options) return
    setRounds(options.limits.default_rounds)
    // Operator defaults (PRP-0235): the decision model chooses the speakers when it is available, and
    // every seat starts with all the research tools.
    setSelectionMode(options.speaker_selection_available ? 'decision' : 'all')
    setSeats(
      options.default_seats.map((id, i) => ({
        key: `s${i}`,
        choice: `preset:${id}`,
        model: '',
        effort: '',
        tools: defaultTools(options),
      })),
    )
  }, [open, options])

  const roundsChoices = useMemo(
    () => Array.from({ length: options?.limits.max_rounds ?? 5 }, (_, i) => i + 1),
    [options],
  )
  const noModel = options !== null && options.models.length === 0
  const canStart = topic.trim().length > 0 && seats.length >= 2 && !submitting && !noModel

  // Choosing a folder adds the Code analyst with the Code tool (PRP-0236 Q6).
  const chooseFolder = (folder: string | null) => {
    setCodeFolder(folder)
    if (folder === null) {
      setSeatsFull(false)
      return
    }
    const next = withCodeAnalyst(seats, options)
    setSeats(next.seats)
    setSeatsFull(next.full)
  }

  const start = async () => {
    if (!canStart) return
    setSubmitting(true)
    setError(null)
    try {
      const participants = seatRequests(seats, options, codeFolder)
      const { thread_id } = await discussionApi.create({
        topic: topic.trim(),
        goal: goal.trim(),
        constraints: constraints.trim(),
        participants,
        rounds,
        folder_id: folderId ?? null,
        auto,
        report,
        speaker_selection: options?.speaker_selection_available ? selectionMode : 'all',
        ...(codeFolder !== null ? { code_folder: codeFolder } : {}),
      })
      onOpenChange(false)
      setTopic('')
      setGoal('')
      setConstraints('')
      onCreated(thread_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The discussion could not be created')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] w-[min(720px,calc(100vw-2rem))] max-w-none flex-col gap-3 sm:max-w-none">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="h-4 w-4" aria-hidden="true" />
            New AI discussion
          </DialogTitle>
          <DialogDescription>
            AI participants discuss your topic for a few rounds. A participant may look things up with the research
            tools you give it; nothing is changed. You confirm after each step (or only once) and decide at the end.
          </DialogDescription>
        </DialogHeader>
        {/* The gutter is reserved whether or not the scrollbar shows, so the width never changes, and
            the side padding keeps the inputs' left edge and focus rings inside the scroll area. */}
        <div className="-mx-1 min-h-0 flex-1 space-y-3 overflow-y-auto overflow-x-hidden px-1 pb-1 [scrollbar-gutter:stable]">
          <label className="block space-y-1 text-sm">
            <span className="font-medium">Topic</span>
            <textarea
              aria-label="Topic"
              className={textareaClass}
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="What should be discussed?"
              maxLength={2000}
            />
          </label>
          <div className="space-y-1 text-sm">
            <label htmlFor="discussion-goal" className="block font-medium">
              Wanted outcome
            </label>
            <Input
              id="discussion-goal"
              aria-label="Wanted outcome"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="For example: a plan for the first version"
              maxLength={2000}
            />
          </div>
          <label className="block space-y-1 text-sm">
            <span className="font-medium">Constraints and assumptions</span>
            <textarea
              aria-label="Constraints and assumptions"
              className={textareaClass}
              value={constraints}
              onChange={(e) => setConstraints(e.target.value)}
              placeholder="Budget, time, what must not change..."
              maxLength={4000}
            />
          </label>
          {options?.code_available && <CodeFolderOption value={codeFolder} onChange={chooseFolder} full={seatsFull} />}
          <SeatEditor options={options} agents={agents} seats={seats} onChange={setSeats} codeFolder={codeFolder} />
          <label className="flex items-center gap-2 text-sm">
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
            <span className="text-xs text-muted-foreground">
              Budget {options ? options.limits.token_budget.toLocaleString() : '-'} tokens per discussion
            </span>
          </label>
          <SpeakerSelectionOption
            value={options?.speaker_selection_available ? selectionMode : 'all'}
            available={Boolean(options?.speaker_selection_available)}
            onChange={setSelectionMode}
          />
          <RunOptionToggles auto={auto} report={report} onAuto={setAuto} onReport={setReport} />
          {noModel && (
            <p role="alert" className="text-xs text-red-600">
              No chat model is configured. Add a model in Model Settings first.
            </p>
          )}
          {(error || optionsError) && (
            <p role="alert" className="text-xs text-red-600">
              {error || optionsError}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => void start()} disabled={!canStart}>
            {submitting && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            Start discussion
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
