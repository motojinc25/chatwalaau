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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Token usage</DialogTitle>
          <DialogDescription>
            What this turn consumed, and what the next message starts from.
            {usage.model ? ` Model: ${usage.model}.` : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="text-sm">
          {turn && (
            <section>
              <h3 className="mb-1 text-xs font-medium text-muted-foreground uppercase">This turn (cumulative)</h3>
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

          <section className={turn ? 'mt-3 border-t pt-2' : ''}>
            <h3 className="mb-1 text-xs font-medium text-muted-foreground uppercase">Last call</h3>
            <Row label="Input" value={usage.input_token_count} />
            <Row label="Output" value={usage.output_token_count} />
          </section>

          {base !== undefined && (
            <section className="mt-3 border-t pt-2">
              <div className="flex items-baseline justify-between gap-6 py-1">
                <span title="What the next message carries as context">Context base</span>
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
