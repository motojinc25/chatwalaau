// Shared pieces of the AI Discussion UI (CTR-0276, PRP-0232): speaker colours and the
// attribution line on a discussion message (with the research tools a turn used and the full
// report's label, PRP-0234).
import { FileText, Gavel, Loader2, MessageSquareQuote, Search, User } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { DiscussionMessageMeta } from '@/types/chat'

// Full class strings, so Tailwind's scanner sees every one of them.
export const SPEAKER_COLOR_CLASSES: Record<string, { dot: string; text: string; ring: string; soft: string }> = {
  blue: {
    dot: 'bg-blue-500',
    text: 'text-blue-700 dark:text-blue-300',
    ring: 'border-blue-500/40',
    soft: 'bg-blue-500/5',
  },
  red: { dot: 'bg-red-500', text: 'text-red-700 dark:text-red-300', ring: 'border-red-500/40', soft: 'bg-red-500/5' },
  green: {
    dot: 'bg-green-500',
    text: 'text-green-700 dark:text-green-300',
    ring: 'border-green-500/40',
    soft: 'bg-green-500/5',
  },
  violet: {
    dot: 'bg-violet-500',
    text: 'text-violet-700 dark:text-violet-300',
    ring: 'border-violet-500/40',
    soft: 'bg-violet-500/5',
  },
  amber: {
    dot: 'bg-amber-500',
    text: 'text-amber-700 dark:text-amber-300',
    ring: 'border-amber-500/40',
    soft: 'bg-amber-500/5',
  },
  pink: {
    dot: 'bg-pink-500',
    text: 'text-pink-700 dark:text-pink-300',
    ring: 'border-pink-500/40',
    soft: 'bg-pink-500/5',
  },
}

export function speakerColor(color?: string) {
  return SPEAKER_COLOR_CLASSES[color ?? ''] ?? SPEAKER_COLOR_CLASSES.blue
}

export function phaseLabel(phase: string): string {
  if (phase === 'opening') return 'Opening'
  if (phase === 'framing') return 'Points'
  if (phase === 'summary') return 'Summary'
  if (phase === 'brief') return 'Topic'
  if (phase.startsWith('round:')) return `Round ${phase.slice(6)}`
  if (phase.startsWith('ask:')) return 'Answer'
  return phase
}

const TOOL_LABELS: Record<string, string> = {
  web_search: 'Web search',
  rag_search: 'Documents',
  ontology_query: 'Ontology',
}

/** "Web search x2, Documents" -- what a participant turn looked up (never the results, UDR-0217 D3). */
export function toolsUsedLabel(tools: { name: string }[] | undefined): string {
  const counts = new Map<string, number>()
  for (const t of tools ?? []) counts.set(t.name, (counts.get(t.name) ?? 0) + 1)
  return [...counts].map(([name, n]) => `${TOOL_LABELS[name] ?? name}${n > 1 ? ` x${n}` : ''}`).join(', ')
}

/** Who spoke, in which phase -- always visible, because it changes how the text is read. */
export function DiscussionSpeakerTag({ meta }: { meta: DiscussionMessageMeta }) {
  const color = speakerColor(meta.color)
  const human = meta.kind === 'human' || meta.kind === 'brief'
  return (
    <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[11px]" data-testid="discussion-speaker">
      {human ? (
        <User className="h-3 w-3 text-muted-foreground" aria-hidden="true" />
      ) : meta.kind === 'moderator' ? (
        <Gavel className={cn('h-3 w-3', color.text)} aria-hidden="true" />
      ) : meta.kind === 'report' ? (
        <FileText className={cn('h-3 w-3', color.text)} aria-hidden="true" />
      ) : (
        <span className={cn('inline-block h-2 w-2 rounded-full', color.dot)} aria-hidden="true" />
      )}
      <span className={cn('font-semibold', human ? 'text-muted-foreground' : color.text)}>
        {meta.kind === 'brief' ? 'AI discussion' : meta.display_name}
      </span>
      <span className="rounded-full border px-1.5 py-px text-muted-foreground">
        {meta.kind === 'report' ? 'Report' : phaseLabel(meta.phase)}
      </span>
      {meta.tools && meta.tools.length > 0 && (
        <span className="inline-flex items-center gap-1 text-muted-foreground" data-testid="discussion-tools-used">
          <Search className="h-3 w-3" aria-hidden="true" />
          Used: {toolsUsedLabel(meta.tools)}
        </span>
      )}
      {meta.status === 'generating' && (
        <span className="inline-flex items-center gap-1 text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
          {meta.kind === 'moderator' ? 'organizing...' : meta.kind === 'report' ? 'writing...' : 'speaking...'}
        </span>
      )}
      {meta.status === 'interrupted' && (
        <span className="inline-flex items-center gap-1 text-amber-600 dark:text-amber-400">
          <MessageSquareQuote className="h-3 w-3" aria-hidden="true" />
          interrupted -- not saved
        </span>
      )}
    </div>
  )
}
