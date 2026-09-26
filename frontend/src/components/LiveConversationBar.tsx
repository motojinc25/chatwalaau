import { AudioLines, Ear, EarOff, Loader2, SendHorizontal, Square } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { WaveformBars } from '@/components/WaveformVisualizer'
import type { LiveState } from '@/hooks/useLiveVoice'
import { cn } from '@/lib/utils'

/**
 * The composer's control row while a Live conversation runs (CTR-0228 / CTR-0221,
 * PRP-0188, UDR-0170 D9).
 *
 * Like Voice Input's recording row it takes the whole control row, and its bars are
 * the MICROPHONE level -- but in the brand teal, never Voice Input's red, so the two
 * modes cannot be confused at a glance. The state, the remaining time, mute and stop
 * are the only controls; everything else in the composer waits until Live ends.
 */
export const LIVE_BAR_CLASS = 'bg-cyan-500'

interface LiveConversationBarProps {
  state: LiveState
  levels: number[]
  muted: boolean
  working: boolean
  /** Step 3: queued + running delegations, shown as "Working (n)". */
  workingCount?: number
  remainingSeconds: number | null
  onStop: () => void
  onToggleMute: () => void
  /** Step 3: typing during Live. Shown when there is text to send. */
  canSend?: boolean
  onSend?: () => void
  barHeight: number
  controlClassName: string
}

function formatRemaining(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

export function LiveConversationBar({
  state,
  levels,
  muted,
  working,
  workingCount = 0,
  remainingSeconds,
  onStop,
  onToggleMute,
  canSend = false,
  onSend,
  barHeight,
  controlClassName,
}: LiveConversationBarProps) {
  const label =
    state === 'connecting'
      ? 'Connecting'
      : state === 'closing'
        ? 'Ending'
        : working
          ? `Working (${Math.max(1, workingCount)})`
          : muted
            ? 'Muted'
            : 'Live'
  const busy = state === 'connecting' || state === 'closing' || working
  return (
    <div className="flex w-full items-center gap-2" aria-live="polite">
      <span
        className="flex shrink-0 items-center gap-1 rounded-full border border-cyan-500/60 px-2 py-0.5 text-[11px] font-medium text-cyan-600 dark:text-cyan-400"
        title={`Live conversation: ${label}`}>
        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <AudioLines className="h-3 w-3" />}
        {label}
      </span>
      <WaveformBars data={muted ? levels.map(() => 0) : levels} barHeight={barHeight} barClassName={LIVE_BAR_CLASS} />
      {remainingSeconds !== null && (
        <span
          className="shrink-0 tabular-nums text-[11px] text-muted-foreground"
          title="Time left in this Live conversation">
          {formatRemaining(remainingSeconds)}
        </span>
      )}
      <button
        type="button"
        onClick={onToggleMute}
        disabled={state !== 'live'}
        className={cn(
          'inline-flex shrink-0 items-center justify-center rounded-md transition-colors',
          controlClassName,
          muted ? 'text-red-500 hover:text-red-600' : 'text-muted-foreground hover:text-foreground',
          'disabled:pointer-events-none disabled:opacity-50',
        )}
        aria-pressed={muted}
        aria-label={muted ? 'Unmute: let the assistant hear you' : 'Mute: stop the assistant hearing you'}
        title={muted ? 'Unmute: let the assistant hear you' : 'Mute: stop the assistant hearing you'}>
        {/* Ear, not a microphone: the composer's Voice Input (dictation) owns the mic
            glyph, and two different actions must not share one icon (v0.169.0). */}
        {muted ? <EarOff className="h-4 w-4" /> : <Ear className="h-4 w-4" />}
      </button>
      {onSend && canSend && (
        <Button
          size="icon"
          className={cn('shrink-0', controlClassName)}
          onClick={onSend}
          aria-label="Send to the Live conversation"
          title="Send to the Live conversation">
          <SendHorizontal className="h-4 w-4" />
        </Button>
      )}
      <Button
        variant="destructive"
        size="icon"
        className={cn('shrink-0', controlClassName)}
        onClick={onStop}
        disabled={state === 'closing'}
        aria-label="Stop Live conversation"
        title="Stop Live conversation">
        <Square className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}
