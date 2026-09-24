import { Square } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/**
 * Live microphone level while recording (CTR-0020).
 *
 * PRP-0186 follow-up (CTR-0221): this renders INSIDE the composer's control row now,
 * not in place of the composer. It therefore carries no chrome of its own -- no
 * border, no background, no padding -- because the composer already provides all
 * three, and a second border inside the first read as a control that had replaced the
 * message box rather than one occupying a row of it.
 *
 * Which is exactly what it used to do: while recording, ChatInput rendered this
 * component INSTEAD of the composer. The draft text stayed in state and came back
 * afterwards, but the operator watched a multi-line message they had just typed
 * disappear the moment they pressed the microphone -- indistinguishable from having
 * lost it. The composer stays mounted now and only this row changes.
 */
interface WaveformVisualizerProps {
  data: number[]
  onStop: () => void
  className?: string
  /**
   * Tallest bar, in px. The caller sizes it to its control row so starting a
   * recording never changes the composer's height (a jump there would move the chat
   * body, which reserves a spacer measured from it -- CTR-0092).
   */
  barHeight?: number
  /** Control-row sizing for the stop button, so it matches its neighbours' tap target. */
  stopClassName?: string
}

export function WaveformVisualizer({
  data,
  onStop,
  className,
  barHeight = 32,
  stopClassName,
}: WaveformVisualizerProps) {
  return (
    <div className={cn('flex w-full items-center gap-3', className)}>
      <div
        className="flex flex-1 items-center justify-center gap-[2px] overflow-hidden"
        style={{ height: barHeight }}
        // The bars are decoration; the state they report is announced by the stop
        // button's label, which is the only thing here a keyboard or screen reader
        // can act on.
        aria-hidden="true">
        {Array.from(data, (value, i) => {
          const key = `b${i}`
          return (
            <div
              key={key}
              className="w-[3px] shrink-0 rounded-full bg-red-500 transition-all duration-75"
              style={{ height: `${Math.max(3, value * barHeight)}px` }}
            />
          )
        })}
      </div>
      <Button
        variant="destructive"
        size="icon"
        className={cn('h-8 w-8 shrink-0', stopClassName)}
        onClick={onStop}
        aria-label="Stop recording"
        title="Stop recording">
        <Square className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}
