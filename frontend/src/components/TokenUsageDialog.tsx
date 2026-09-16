import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { UsageInfo } from '@/types/chat'

interface TokenUsageDialogProps {
  usage: UsageInfo
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Per-turn token detail (CTR-0030, PRP-0157, UDR-0135).
 *
 * Presentation only: every number arrives computed from the CTR-0009 usage event.
 * The dialog performs no arithmetic beyond formatting and percentage display, so
 * the two axes it shows -- the turn CUMULATIVE (billing) and the context BASE
 * (occupancy) -- cannot drift from what the backend measured.
 *
 * A row whose key is absent is OMITTED, never rendered as 0 (UDR-0135 D7): a
 * provider that does not report cache tokens has not reported zero of them.
 */
function formatCount(value: number | undefined): string | null {
  return value === undefined ? null : value.toLocaleString()
}

function Row({
  label,
  value,
  indent = false,
  hint,
}: {
  label: string
  value: number | undefined
  indent?: boolean
  hint?: string
}) {
  const formatted = formatCount(value)
  if (formatted === null) return null
  return (
    <div className="flex items-baseline justify-between gap-6 py-1">
      <span className={indent ? 'pl-4 text-muted-foreground' : ''} title={hint}>
        {label}
      </span>
      <span className="tabular-nums">{formatted}</span>
    </div>
  )
}

export function TokenUsageDialog({ usage, open, onOpenChange }: TokenUsageDialogProps) {
  const turn = usage.turn
  const base = usage.context_base_tokens
  const max = usage.max_context_tokens
  const percent = base !== undefined && max ? Math.min(Math.round((base / max) * 100), 100) : null
  // A Declarative Workflow run (PRP-0170): `turn` is the total over every node, the
  // context fields describe the node closest to its window, and the breakdown is listed.
  const nodes = usage.workflow_nodes
  const isWorkflow = nodes !== undefined
  const peak = isWorkflow ? nodes.find((n) => n.context_base_tokens === base && n.model === usage.model) : undefined

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Token usage</DialogTitle>
          <DialogDescription>
            {isWorkflow
              ? 'What this workflow run consumed, across all of its nodes.'
              : 'What this turn consumed, and what the next message starts from.'}
            {!isWorkflow && usage.model ? ` Model: ${usage.model}.` : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="text-sm">
          {turn && (
            <section>
              <h3 className="mb-1 text-xs font-medium text-muted-foreground uppercase">
                {isWorkflow ? 'This run (all nodes)' : 'This turn (cumulative)'}
              </h3>
              <Row
                label="Model calls"
                value={turn.model_calls}
                hint="Every model call of this turn, including tool-loop steps"
              />
              <Row
                label="Input (uncached)"
                value={turn.uncached_input_token_count ?? turn.input_token_count}
                hint="Input tokens billed at the full rate"
              />
              <Row
                label="Cache read"
                value={turn.cache_read_input_token_count}
                hint="Prompt tokens served from cache"
              />
              <Row
                label="Cache write"
                value={turn.cache_creation_input_token_count}
                hint="Prompt tokens written to cache"
              />
              <Row label="Output" value={turn.output_token_count} />
              <Row label="reasoning" value={turn.reasoning_output_token_count} indent hint="Included in Output" />
            </section>
          )}

          {isWorkflow && nodes.length > 0 && (
            <section className={turn ? 'mt-3 border-t pt-2' : ''}>
              <h3 className="mb-1 text-xs font-medium text-muted-foreground uppercase">Per node</h3>
              {nodes.map((n, i) => (
                <div
                  // Executions of the same node in a loop are distinct rows in run order.
                  // biome-ignore lint/suspicious/noArrayIndexKey: rows have no stable id of their own
                  key={`${n.node ?? n.agent ?? 'node'}-${i}`}
                  className="flex items-baseline justify-between gap-4 py-1">
                  <span className="min-w-0 truncate" title={[n.agent, n.model].filter(Boolean).join(' / ')}>
                    {n.label ?? n.node ?? n.agent ?? 'node'}
                    {n.agent && (n.label ?? n.node) ? (
                      <span className="pl-2 text-xs text-muted-foreground">{n.agent}</span>
                    ) : null}
                  </span>
                  <span className="shrink-0 tabular-nums">
                    {(n.turn?.input_token_count ?? 0).toLocaleString()} in /{' '}
                    {(n.turn?.output_token_count ?? 0).toLocaleString()} out
                  </span>
                </div>
              ))}
            </section>
          )}

          {!isWorkflow && (
            <section className={turn ? 'mt-3 border-t pt-2' : ''}>
              <h3 className="mb-1 text-xs font-medium text-muted-foreground uppercase">Last call</h3>
              <Row label="Input" value={usage.input_token_count} />
              <Row label="Output" value={usage.output_token_count} />
            </section>
          )}

          {base !== undefined && (
            <section className="mt-3 border-t pt-2">
              <div className="flex items-baseline justify-between gap-6 py-1">
                <span
                  title={
                    isWorkflow
                      ? 'The node whose context came closest to its window'
                      : 'What the next message carries as context'
                  }>
                  {isWorkflow
                    ? `Peak node context${peak ? ` (${peak.label ?? peak.node ?? peak.agent})` : ''}`
                    : 'Context base'}
                </span>
                <span className="tabular-nums">
                  {base.toLocaleString()}
                  {max ? ` / ${max.toLocaleString()}` : ''}
                  {percent !== null ? ` (${percent}%)` : ''}
                </span>
              </div>
            </section>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
