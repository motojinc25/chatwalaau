import { Brain, ChevronRight } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { MarkdownRenderer } from '@/components/MarkdownRenderer'
import type { ReasoningBlock } from '@/types/chat'

interface ReasoningIndicatorProps {
  reasoningBlocks: ReasoningBlock[]
}

export function ThinkingBlock({ block }: { block: ReasoningBlock }) {
  const [elapsed, setElapsed] = useState(0)
  const [expanded, setExpanded] = useState(false)
  const startRef = useRef(Date.now())

  useEffect(() => {
    if (block.status === 'thinking') {
      const timer = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startRef.current) / 1000))
      }, 1000)
      return () => clearInterval(timer)
    }
  }, [block.status])

  const isThinking = block.status === 'thinking'
  const label = isThinking ? `Thinking${elapsed > 0 ? ` (${elapsed}s)` : '...'}` : `Thought for ${elapsed}s`

  return (
    <div className="mb-1">
      <button
        type="button"
        className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        onClick={() => setExpanded(!expanded)}>
        <Brain className={`h-3.5 w-3.5 ${isThinking ? 'animate-pulse' : ''}`} />
        <span>{label}</span>
        <ChevronRight className={`h-3 w-3 transition-transform ${expanded ? 'rotate-90' : ''}`} />
      </button>
      {expanded && block.content && (
        <div className="mt-1 ml-5 max-h-48 overflow-y-auto rounded-md bg-muted/50 p-2.5 text-xs leading-relaxed text-muted-foreground [&_pre]:text-[0.7rem]">
          <MarkdownRenderer content={block.content} />
        </div>
      )}
    </div>
  )
}

export function ReasoningIndicator({ reasoningBlocks }: ReasoningIndicatorProps) {
  if (reasoningBlocks.length === 0) return null

  return (
    <div className="flex flex-col">
      {reasoningBlocks.map((block) => (
        <ThinkingBlock key={block.id} block={block} />
      ))}
    </div>
  )
}
