// The seat editor of the AI discussion dialogs (CTR-0276): the New AI discussion dialog (PRP-0232)
// and the Continue dialog (PRP-0233, UDR-0216 D4) use the same component, so a seat follows the same
// rules in both: a role seat has a model and a reasoning effort, a Prompt agent seat uses the agent's own.
import { Plus, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { speakerColor } from '@/components/DiscussionParts'
import { Button } from '@/components/ui/button'
import {
  type DiscussionOptions,
  type DiscussionToolId,
  discussionApi,
  type SeatRequest,
  type SpeakerSelectionMode,
} from '@/lib/discussionApi'
import { cn } from '@/lib/utils'
import type { DiscussionParticipant } from '@/types/chat'

export interface PromptAgentRow {
  id: string
  name: string
  display_name?: string
  source: 'core' | 'custom'
  loaded: boolean
  error?: string | null
  warnings?: string[]
}

export interface Seat {
  key: string
  /** `preset:<id>` or `agent:<id>` */
  choice: string
  /** A role seat's model and reasoning ('' = default); an agent seat uses the agent's own. */
  model: string
  effort: string
  /** Research tools of the seat (PRP-0234, UDR-0217 D1). */
  tools: DiscussionToolId[]
}

/** The discussion options and the usable Prompt agents, loaded while a dialog is open. */
export function useSeatOptions(open: boolean) {
  const [options, setOptions] = useState<DiscussionOptions | null>(null)
  const [agents, setAgents] = useState<PromptAgentRow[]>([])
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

  return { options, agents, error }
}

/** The seats of a run, as the editor shows them (a continuation starts from the source run's seats). */
export function seatsFromParticipants(participants: DiscussionParticipant[]): Seat[] {
  return participants.map((p, i) =>
    p.source === 'agent' && p.agent_id
      ? { key: `s${i}`, choice: `agent:${p.agent_id}`, model: '', effort: '', tools: toolsOf(p.tools) }
      : {
          key: `s${i}`,
          choice: `preset:${p.preset ?? p.role}`,
          model: p.model ?? '',
          effort: p.effort ?? '',
          tools: toolsOf(p.tools),
        },
  )
}

const toolsOf = (tools: string[] | undefined): DiscussionToolId[] => (tools ?? []) as DiscussionToolId[]

/** A new seat's tools: every available research tool (operator default, PRP-0235). A tool the seat's
 *  model cannot run (web search) stays off on screen and is never sent (see `usableTools`). */
export function defaultTools(options: DiscussionOptions | null): DiscussionToolId[] {
  return (options?.tools ?? []).filter((t) => t.available).map((t) => t.id)
}

/** The seat's tools that its model can run (web search depends on the model). */
export function usableTools(seat: Seat, options: DiscussionOptions | null): DiscussionToolId[] {
  return seat.tools.filter((t) => toolUsable(t, seat, options))
}

function toolUsable(tool: DiscussionToolId, seat: Seat, options: DiscussionOptions | null): boolean {
  const entry = options?.tools?.find((t) => t.id === tool)
  if (!entry?.available) return false
  if (tool !== 'web_search') return true
  // An agent seat's model is the agent's own (unknown here); the server checks it on create.
  if (seat.choice.startsWith('agent:')) return true
  return (options?.web_search_models ?? []).includes(seat.model || (options?.default_model ?? ''))
}

/** The request body for the seats. */
export function seatRequests(seats: Seat[], options: DiscussionOptions | null): SeatRequest[] {
  return seats.map((s) => {
    const [kind, id] = s.choice.split(/:(.*)/s)
    // Only the tools the seat's model can run: a model change may have left web search behind.
    const usable = usableTools(s, options)
    const tools = usable.length > 0 ? usable : undefined
    // A Prompt agent seat sends neither model nor effort: the server uses the agent's own.
    if (kind === 'agent') return { kind: 'agent', agent_id: id, tools }
    return { kind: 'preset', preset: id, model: s.model || undefined, effort: s.effort || undefined, tools }
  })
}

interface RunOptionProps {
  auto: boolean
  report: boolean
  onAuto: (auto: boolean) => void
  onReport: (report: boolean) => void
}

/** Auto mode and the full report (PRP-0234, UDR-0217 D4 / D5), the same in both dialogs. */
export function RunOptionToggles({ auto, report, onAuto, onReport }: RunOptionProps) {
  return (
    <div className="space-y-1 text-sm">
      <label className="flex items-start gap-2">
        <input type="checkbox" className="mt-1" checked={auto} onChange={(e) => onAuto(e.target.checked)} />
        <span>
          Run the rounds without asking
          <span className="block text-xs text-muted-foreground">
            The opening, the rounds and the summary run one after another without waiting. A moderator suggestion to
            summarize is only shown; Pause hands control back to you.
          </span>
        </span>
      </label>
      <label className="flex items-start gap-2">
        <input type="checkbox" className="mt-1" checked={report} onChange={(e) => onReport(e.target.checked)} />
        <span>
          Write a full report with the summary
          <span className="block text-xs text-muted-foreground">
            Background, positions, disagreements, options and risks, after the summary card. One more model call.
          </span>
        </span>
      </label>
    </div>
  )
}

interface SelectionProps {
  value: SpeakerSelectionMode
  available: boolean
  onChange: (mode: SpeakerSelectionMode) => void
}

/** Who speaks in a round (PRP-0235, UDR-0218 D4), the same in both dialogs. */
export function SpeakerSelectionOption({ value, available, onChange }: SelectionProps) {
  return (
    <fieldset className="space-y-1 text-sm" data-testid="discussion-speaker-selection">
      <legend className="font-medium">Who speaks in a round</legend>
      <label className="flex items-start gap-2">
        <input
          type="radio"
          name="speaker-selection"
          className="mt-1"
          checked={value === 'all'}
          onChange={() => onChange('all')}
        />
        <span>Everyone</span>
      </label>
      <label className={cn('flex items-start gap-2', !available && 'opacity-50')}>
        <input
          type="radio"
          name="speaker-selection"
          className="mt-1"
          checked={value === 'decision'}
          disabled={!available}
          onChange={() => onChange('decision')}
        />
        <span>
          Chosen by the decision model
          <span className="block text-xs text-muted-foreground">
            {available
              ? 'The decision model scores each participant; the best-suited speak, in seat order. Everyone gives an opening view, a participant left out is brought back, and every choice is shown.'
              : 'Needs a decision model in the model catalog.'}
          </span>
        </span>
      </label>
    </fieldset>
  )
}

interface EditorProps {
  options: DiscussionOptions | null
  agents: PromptAgentRow[]
  seats: Seat[]
  onChange: (seats: Seat[]) => void
}

export function SeatEditor({ options, agents, seats, onChange }: EditorProps) {
  const maxSeats = options?.limits.max_participants ?? 4

  const updateSeat = (key: string, patch: Partial<Seat>) =>
    onChange(seats.map((s) => (s.key === key ? { ...s, ...patch } : s)))

  /** Reasoning efforts the seat's model offers (empty: the model has no reasoning setting). */
  const effortsOf = (seat: Seat): string[] => options?.reasoning?.[seat.model || options.default_model]?.allowed ?? []
  /** What "Default" means for a role seat: the role default when the model offers it. */
  const defaultEffortOf = (seat: Seat): string => {
    const allowed = effortsOf(seat)
    const roleDefault = options?.role_default_effort ?? 'low'
    if (allowed.includes(roleDefault)) return roleDefault
    return options?.reasoning?.[seat.model || options.default_model]?.default ?? ''
  }

  return (
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
                  title={effortsOf(seat).length === 0 ? 'This model has no reasoning setting' : 'Reasoning effort'}
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
              onClick={() => onChange(seats.filter((s) => s.key !== seat.key))}
              aria-label={`Remove participant ${index + 1}`}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
            {preset && <p className="basis-full pl-4 text-xs text-muted-foreground">{preset.charter}</p>}
            {(options?.tools ?? []).some((t) => t.available) && (
              <fieldset className="flex basis-full flex-wrap items-center gap-x-3 gap-y-1 pl-4 text-xs">
                <legend className="sr-only">Tools of participant {index + 1}</legend>
                <span className="text-muted-foreground">Tools:</span>
                {(options?.tools ?? [])
                  .filter((t) => t.available)
                  .map((t) => {
                    const usable = toolUsable(t.id, seat, options)
                    return (
                      <label
                        key={t.id}
                        className={cn('flex items-center gap-1', !usable && 'opacity-50')}
                        title={usable ? undefined : "This participant's model has no web search"}>
                        <input
                          type="checkbox"
                          aria-label={`${t.label} for participant ${index + 1}`}
                          disabled={!usable}
                          checked={usable && seat.tools.includes(t.id)}
                          onChange={(e) =>
                            updateSeat(seat.key, {
                              tools: e.target.checked
                                ? [...seat.tools.filter((x) => x !== t.id), t.id]
                                : seat.tools.filter((x) => x !== t.id),
                            })
                          }
                        />
                        {t.label}
                      </label>
                    )
                  })}
              </fieldset>
            )}
          </div>
        )
      })}
      <Button
        variant="outline"
        size="sm"
        disabled={seats.length >= maxSeats || !options}
        onClick={() =>
          onChange([
            ...seats,
            {
              key: `s${Date.now()}`,
              choice: 'preset:user_advocate',
              model: '',
              effort: '',
              tools: defaultTools(options),
            },
          ])
        }>
        <Plus className="mr-1 h-3.5 w-3.5" />
        Add participant
      </Button>
    </div>
  )
}
