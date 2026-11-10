// The right side of the decision log (CTR-0276, PRP-0249, UDR-0230): the selected run in full, with
// the person's decision before and apart from the AI's recommendation.
import { useEffect, useState } from 'react'
import { REASONS } from '@/components/DiscussionBar'
import { DiscussionExportActions } from '@/components/DiscussionExport'
import { HighlightedText } from '@/components/HighlightedText'
import { Button } from '@/components/ui/button'
import { usePrivacyScreen } from '@/hooks/usePrivacyScreen'
import { formatSessionDateTime } from '@/lib/datetime'
import { type DecisionLogItem, discussionApi } from '@/lib/discussionApi'
import { cn } from '@/lib/utils'
import type { DiscussionFinal, DiscussionRun } from '@/types/chat'

const VERDICT_BADGES: Record<string, { label: string; className: string }> = {
  adopt: { label: 'Adopted', className: 'bg-green-500/10 text-green-700 dark:text-green-300' },
  hold: { label: 'On hold', className: 'bg-amber-500/10 text-amber-700 dark:text-amber-300' },
  rediscuss: { label: 'Re-discuss', className: 'bg-blue-500/10 text-blue-700 dark:text-blue-300' },
}

const FINAL_LISTS: { field: keyof Omit<DiscussionFinal, 'recommendation'>; label: string }[] = [
  { field: 'agreements', label: 'Agreements' },
  { field: 'dissent', label: 'Dissent' },
  { field: 'open_issues', label: 'Open issues' },
  { field: 'next_actions', label: 'Next actions' },
]

export function VerdictBadge({ verdict }: { verdict: string | null }) {
  const badge = verdict ? VERDICT_BADGES[verdict] : undefined
  return (
    <span
      className={cn(
        'whitespace-nowrap rounded-full px-2 py-px text-[11px] font-medium',
        badge?.className ?? 'bg-muted text-muted-foreground',
      )}>
      {badge?.label ?? 'Not decided'}
    </span>
  )
}

function SectionTitle({ children }: { children: string }) {
  return <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{children}</h4>
}

/** Rendered with `key` set to the row, so moving to another row starts from the shortened text. */
export function DiscussionDecisionDetail({
  item,
  query,
  onOpenChat,
}: {
  item: DecisionLogItem
  query: string
  onOpenChat: () => void
}) {
  const { enabled: redacted, redact } = usePrivacyScreen()
  const [run, setRun] = useState<DiscussionRun | null>(null)
  const [readError, setReadError] = useState<string | null>(null)
  const id = `${item.thread_id}:${item.run_id}`

  useEffect(() => {
    let current = true
    discussionApi
      .run(item.thread_id, item.run_id)
      .then((res) => {
        if (current) setRun(res.run)
      })
      .catch((err) => {
        if (current) setReadError(err instanceof Error ? err.message : 'The discussion could not be read')
      })
    return () => {
      current = false
    }
  }, [item.thread_id, item.run_id])

  const topic = run?.brief.topic || item.topic || item.title
  const note = run ? (run.human_decision?.note ?? '') : item.note
  const recommendation = run ? (run.final?.recommendation ?? '') : item.recommendation
  const reason = REASONS[item.termination_reason ?? '']

  return (
    <div className="min-w-0 flex-1 overflow-y-auto p-5" data-testid="decision-log-detail">
      <h3 className="whitespace-pre-wrap break-words text-base font-semibold">
        <HighlightedText text={redact(topic, `decision-detail-topic:${id}`)} query={query} redacted={redacted} />
      </h3>
      <p className="mb-4 mt-0.5 text-[11px] text-muted-foreground">
        {redact(item.title, `decision-detail-title:${id}`)}
        {item.parent_run_id && ' - continued'}
      </p>

      {readError && (
        <p role="alert" className="mb-3 text-xs text-amber-700 dark:text-amber-400">
          Showing a shortened copy: {readError}
        </p>
      )}

      <section className="mb-4 rounded-md border p-3" data-testid="decision-log-your-decision">
        <SectionTitle>Your decision</SectionTitle>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <VerdictBadge verdict={item.verdict} />
          {item.decided_at && <span className="tabular-nums">{formatSessionDateTime(item.decided_at)}</span>}
          {item.decided_by && <span>{item.decided_by}</span>}
        </div>
        {note && (
          <p className="mt-2 whitespace-pre-wrap break-words text-sm">
            <HighlightedText text={redact(note, `decision-detail-note:${id}`)} query={query} redacted={redacted} />
          </p>
        )}
      </section>

      <section className="mb-4" data-testid="decision-log-recommendation">
        <SectionTitle>AI recommendation</SectionTitle>
        <p className="whitespace-pre-wrap break-words text-sm">
          {recommendation ? <HighlightedText text={recommendation} query={query} redacted={redacted} /> : '-'}
        </p>
      </section>

      {FINAL_LISTS.map(({ field, label }) => {
        const lines = run?.final?.[field] ?? []
        if (lines.length === 0) return null
        return (
          <section key={field} className="mb-4">
            <SectionTitle>{label}</SectionTitle>
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {lines.map((line) => (
                <li key={line} className="whitespace-pre-wrap break-words">
                  {line}
                </li>
              ))}
            </ul>
          </section>
        )
      })}

      <p className="mb-3 text-[11px] text-muted-foreground">
        Rounds: {item.rounds ?? '-'}
        {reason && ` - Ended because ${reason}`}
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" onClick={onOpenChat}>
          Open chat
        </Button>
        <DiscussionExportActions threadId={item.thread_id} runId={item.run_id} compact />
      </div>
    </div>
  )
}
