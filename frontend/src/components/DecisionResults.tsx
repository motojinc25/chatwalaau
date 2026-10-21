import { Scale } from 'lucide-react'
import { useMemo } from 'react'
import { DECISION_EVALUATE_TOOL, type DecisionTable, LOW_CONFIDENCE, parseDecisionResult } from '@/lib/decisionResult'
import { cn } from '@/lib/utils'
import type { ToolCall } from '@/types/chat'

/**
 * decision_evaluate answers as a table in the chat (PRP-0219, CTR-0248, operator Q5).
 *
 * One table per completed call: question, type, answer, the probability of that answer
 * (with a bar), and a detail column (other options / score position). A row whose top
 * probability is below LOW_CONFIDENCE is marked, because the model gives no reason and
 * the number is the only signal of doubt. An error result renders nothing here -- the
 * tool indicator already shows the call and its result.
 */
export function DecisionResults({ toolCalls }: { toolCalls: ToolCall[] }) {
  const tables = useMemo(
    () =>
      toolCalls
        .filter((tc) => tc.name === DECISION_EVALUATE_TOOL && tc.status === 'completed')
        .map((tc) => ({ id: tc.id, table: parseDecisionResult(tc.result) }))
        .filter((t): t is { id: string; table: DecisionTable } => t.table !== null),
    [toolCalls],
  )
  if (tables.length === 0) return null
  return (
    <div className="flex flex-col gap-2">
      {tables.map(({ id, table }) => (
        <DecisionResultTable key={id} table={table} />
      ))}
    </div>
  )
}

function DecisionResultTable({ table }: { table: DecisionTable }) {
  return (
    <figure className="my-2 w-full max-w-2xl overflow-hidden rounded-lg border bg-card" aria-label="Decision results">
      <figcaption className="flex items-center gap-1.5 border-b bg-muted/40 px-3 py-1.5 text-xs font-medium text-muted-foreground">
        <Scale className="h-3.5 w-3.5" />
        Decisions
        {table.model && (
          <span className="truncate font-mono text-[0.7rem] text-muted-foreground/70">{table.model}</span>
        )}
        {table.demo && (
          <span className="rounded bg-amber-100 px-1 text-[0.65rem] text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
            demo
          </span>
        )}
      </figcaption>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-[0.7rem] text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-1.5 font-medium">
                Question
              </th>
              <th scope="col" className="px-3 py-1.5 font-medium">
                Answer
              </th>
              <th scope="col" className="px-3 py-1.5 font-medium">
                Probability
              </th>
              <th scope="col" className="px-3 py-1.5 font-medium">
                Detail
              </th>
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row) => {
              const low = row.confidence !== null && row.confidence < LOW_CONFIDENCE
              return (
                <tr key={row.name} className="border-t align-top">
                  <td className="px-3 py-1.5">
                    <div className="font-mono text-[0.7rem]">{row.name}</div>
                    <div className="text-[0.65rem] text-muted-foreground">{row.type}</div>
                  </td>
                  <td className={cn('px-3 py-1.5 font-medium', row.unreadable && 'text-muted-foreground italic')}>
                    {row.answer}
                    {low && (
                      <span className="ml-1.5 rounded bg-amber-100 px-1 text-[0.65rem] font-normal text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                        uncertain
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-1.5">
                    {row.confidence !== null ? (
                      <div className="flex min-w-24 items-center gap-1.5">
                        <div className="h-1.5 w-16 overflow-hidden rounded bg-muted">
                          <div
                            className={cn('h-full', low ? 'bg-amber-500' : 'bg-cyan-500')}
                            style={{ width: `${Math.round(row.confidence * 100)}%` }}
                          />
                        </div>
                        <span className="tabular-nums">{Math.round(row.confidence * 100)}%</span>
                      </div>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </td>
                  <td className="max-w-56 break-words px-3 py-1.5 text-muted-foreground">{row.detail}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </figure>
  )
}
