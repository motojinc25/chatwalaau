// New AI discussion dialog (CTR-0276, PRP-0232 2.7). The person sets the topic, the wanted
// outcome, the constraints, the seats and the rounds; Start creates the discussion chat and the
// chat page then runs its opening.
import { Loader2, Plus, Trash2, Users } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { speakerColor } from '@/components/DiscussionParts'
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
import { type DiscussionOptions, discussionApi, type SeatRequest } from '@/lib/discussionApi'
import { cn } from '@/lib/utils'

interface PromptAgentRow {
  id: string
  name: string
  display_name?: string
  source: 'core' | 'custom'
  loaded: boolean
  error?: string | null
  warnings?: string[]
}

interface Seat {
  key: string
  /** `preset:<id>` or `agent:<id>` */
  choice: string
  /** A role seat's model and reasoning ('' = default); an agent seat uses the agent's own. */
  model: string
  effort: string
}

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
  const [options, setOptions] = useState<DiscussionOptions | null>(null)
  const [agents, setAgents] = useState<PromptAgentRow[]>([])
  const [topic, setTopic] = useState('')
  const [goal, setGoal] = useState('')
  const [constraints, setConstraints] = useState('')
  const [rounds, setRounds] = useState(2)
  const [seats, setSeats] = useState<Seat[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setError(null)
    Promise.all([
      discussionApi.options(),
      fetch('/api/agents')
        .then((r) => (r.ok ? r.json() : { agents: [] }))
        .catch(() => ({ agents: [] })),
    ])
      .then(([opts, inventory]) => {
        if (cancelled) return
        setOptions(opts)
        setRounds(opts.limits.default_rounds)
        setSeats(opts.default_seats.map((id, i) => ({ key: `s${i}`, choice: `preset:${id}`, model: '', effort: '' })))
        setAgents(
          ((inventory.agents ?? []) as PromptAgentRow[]).filter(
            (a) => a.source === 'custom' && a.loaded && !a.error && !(a.warnings && a.warnings.length > 0),
          ),
        )
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : 'Could not load the options'))
    return () => {
      cancelled = true
    }
  }, [open])

  const maxSeats = options?.limits.max_participants ?? 4
  const roundsChoices = useMemo(
    () => Array.from({ length: options?.limits.max_rounds ?? 5 }, (_, i) => i + 1),
    [options],
  )
  const noModel = options !== null && options.models.length === 0
  const canStart = topic.trim().length > 0 && seats.length >= 2 && !submitting && !noModel

  const updateSeat = (key: string, patch: Partial<Seat>) =>
    setSeats((prev) => prev.map((s) => (s.key === key ? { ...s, ...patch } : s)))

  /** Reasoning efforts the seat's model offers (empty: the model has no reasoning setting). */
  const effortsOf = (seat: Seat): string[] => options?.reasoning?.[seat.model || options.default_model]?.allowed ?? []
  /** What "Default" means for a role seat: the role default when the model offers it. */
  const defaultEffortOf = (seat: Seat): string => {
    const allowed = effortsOf(seat)
    const roleDefault = options?.role_default_effort ?? 'low'
    if (allowed.includes(roleDefault)) return roleDefault
    return options?.reasoning?.[seat.model || options.default_model]?.default ?? ''
  }

  const start = async () => {
    if (!canStart) return
    setSubmitting(true)
    setError(null)
    try {
      const participants: SeatRequest[] = seats.map((s) => {
        const [kind, id] = s.choice.split(/:(.*)/s)
        // A Prompt agent seat sends neither: the server uses the agent's own model and reasoning.
        if (kind === 'agent') return { kind: 'agent', agent_id: id }
        return { kind: 'preset', preset: id, model: s.model || undefined, effort: s.effort || undefined }
      })
      const { thread_id } = await discussionApi.create({
        topic: topic.trim(),
        goal: goal.trim(),
        constraints: constraints.trim(),
        participants,
        rounds,
        folder_id: folderId ?? null,
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
            AI participants discuss your topic for a few rounds. They have no tools. You confirm after each step and
            decide at the end.
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
          <div className="space-y-1.5 text-sm">
            <div className="flex items-center justify-between">
              <span className="font-medium">Participants</span>
              <span className="text-xs text-muted-foreground">
                {seats.length} of at most {maxSeats}
              </span>
            </div>
            {seats.map((seat, index) => {
              const preset = seat.choice.startsWith('preset:')
                ? options?.presets.find((p) => `preset:${p.id}` === seat.choice)
                : undefined
              return (
                <div key={seat.key} className="flex flex-wrap items-center gap-2 rounded-md border px-2 py-1.5">
                  <span
                    className={cn('inline-block h-2 w-2 shrink-0 rounded-full', speakerColor(preset?.color).dot)}
                    aria-hidden="true"
                  />
                  <select
                    aria-label={`Participant ${index + 1}`}
                    className="h-8 min-w-0 flex-1 rounded-md border bg-background px-2 text-sm"
                    value={seat.choice}
                    onChange={(e) =>
                      updateSeat(seat.key, {
                        choice: e.target.value,
                        ...(e.target.value.startsWith('agent:') ? { model: '', effort: '' } : {}),
                      })
                    }>
                    <optgroup label="Roles">
                      {options?.presets.map((p) => (
                        <option key={p.id} value={`preset:${p.id}`}>
                          {p.display_name}
                        </option>
                      ))}
                    </optgroup>
                    {agents.length > 0 && (
                      <optgroup label="Prompt agents">
                        {agents.map((a) => (
                          <option key={a.id} value={`agent:${a.id}`}>
                            {a.display_name || a.name}
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                  {preset ? (
                    <>
                      <select
                        aria-label={`Model of participant ${index + 1}`}
                        className="h-8 w-40 min-w-0 rounded-md border bg-background px-2 text-sm"
                        value={seat.model}
                        onChange={(e) => {
                          const model = e.target.value
                          const allowed = options?.reasoning?.[model || options.default_model]?.allowed ?? []
                          updateSeat(seat.key, { model, ...(allowed.includes(seat.effort) ? {} : { effort: '' }) })
                        }}>
                        <option value="">Default model</option>
                        {options?.models.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                      <select
                        aria-label={`Reasoning of participant ${index + 1}`}
                        className="h-8 w-32 min-w-0 rounded-md border bg-background px-2 text-sm"
                        value={seat.effort}
                        disabled={effortsOf(seat).length === 0}
                        title={
                          effortsOf(seat).length === 0 ? 'This model has no reasoning setting' : 'Reasoning effort'
                        }
                        onChange={(e) => updateSeat(seat.key, { effort: e.target.value })}>
                        <option value="">
                          {effortsOf(seat).length === 0 ? 'No reasoning' : `Default (${defaultEffortOf(seat)})`}
                        </option>
                        {effortsOf(seat).map((level) => (
                          <option key={level} value={level}>
                            {level}
                          </option>
                        ))}
                      </select>
                    </>
                  ) : (
                    <span className="text-xs text-muted-foreground">Uses the agent&apos;s model and reasoning</span>
                  )}
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    disabled={seats.length <= 2}
                    onClick={() => setSeats((prev) => prev.filter((s) => s.key !== seat.key))}
                    aria-label={`Remove participant ${index + 1}`}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                  {preset && <p className="basis-full pl-4 text-xs text-muted-foreground">{preset.charter}</p>}
                </div>
              )
            })}
            <Button
              variant="outline"
              size="sm"
              disabled={seats.length >= maxSeats || !options}
              onClick={() =>
                setSeats((prev) => [
                  ...prev,
                  { key: `s${Date.now()}`, choice: 'preset:user_advocate', model: '', effort: '' },
                ])
              }>
              <Plus className="mr-1 h-3.5 w-3.5" />
              Add participant
            </Button>
          </div>
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
          {noModel && (
            <p role="alert" className="text-xs text-red-600">
              No chat model is configured. Add a model in Model Settings first.
            </p>
          )}
          {error && (
            <p role="alert" className="text-xs text-red-600">
              {error}
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
