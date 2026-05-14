import { Square } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface WaveformVisualizerProps {
  data: number[]
  onStop: () => void
  className?: string
}

export function WaveformVisualizer({ data, onStop, className }: WaveformVisualizerProps) {
  return (
    <div className={cn('flex items-center gap-3 rounded-lg border bg-background px-3 py-1', className)}>
      <div className="flex flex-1 items-center justify-center gap-[2px]" style={{ height: 20 }}>
        {Array.from(data, (value, i) => {
          const key = `b${i}`
          return (
            <div
              key={key}
              className="w-[3px] rounded-full bg-red-500 transition-all duration-75"
              style={{ height: `${Math.max(3, value * 32)}px` }}
            />
          )
        })}
      </div>
      <Button
        variant="destructive"
        size="icon"
        className="h-8 w-8 shrink-0"
        onClick={onStop}
        aria-label="Stop recording">
        <Square className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}
