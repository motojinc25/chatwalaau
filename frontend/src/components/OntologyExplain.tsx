// The Explain tab of the Ontology Manager (PRP-0211, UDR-0193 D14 / D15): why an inferred
// statement or an inconsistency holds -- the chain of rules down to the stated statements.
// The backend matches the entailment rules backwards on the stored result; this pane only
// shows the proof.

import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react'
import { useState } from 'react'
import type { Term } from '@/lib/ontologyModel'
import { cn } from '@/lib/utils'

export interface ExplainStatement {
  s: Term
  p: Term
  o: Term
  graph: Term | null
}

/** What to explain: an inferred statement, or a stored inconsistency finding (its key). */
export type ExplainRequest =
  | { mode: 'rdfs' | 'owl-rl'; statement: ExplainStatement; label: string }
  | { mode: 'rdfs' | 'owl-rl'; finding: string; label: string }

export interface ExplainNode {
  id: string
  kind: 'stated' | 'axiom' | 'inferred' | 'inconsistency'
  statement: ExplainStatement | null
  text: string | null
  rule: string | null
  rule_name: string | null
  premises: string[]
}

export interface ExplainResult {
  mode: string
  mode_label: string
  target: { text?: string; message?: string; statement?: ExplainStatement }
  found: boolean
  root: string | null
  nodes: Record<string, ExplainNode>
  stated: (ExplainStatement & { text: string })[]
  truncated: boolean
  notices: { code: string; message: string }[]
}

/** Where each proof node is first shown (pre-order); later occurrences say "see above". */
function firstPaths(root: string, nodes: Record<string, ExplainNode>): Map<string, string> {
  const first = new Map<string, string>()
  const walk = (id: string, path: string) => {
    if (first.has(id)) return
    first.set(id, path)
    for (const child of nodes[id]?.premises ?? []) walk(child, `${path}/${child}`)
  }
  walk(root, root)
  return first
}

function ProofNode(props: {
  id: string
  path: string
  nodes: Record<string, ExplainNode>
  first: Map<string, string>
  depth: number
  onSelect: (term: Term) => void
}) {
  const node = props.nodes[props.id]
  const [open, setOpen] = useState(props.depth < 3)
  if (!node) return null
  const repeated = props.first.get(props.id) !== props.path
  const leaf = node.kind === 'stated' || node.kind === 'axiom'
  const subject = node.statement?.s
  return (
    <li className="py-0.5" data-explain-kind={node.kind}>
      <div className="flex items-start gap-1">
        {leaf || repeated ? (
          <span className="mt-0.5 inline-block w-3.5 shrink-0" />
        ) : (
          <button
            type="button"
            className="mt-0.5 shrink-0 text-zinc-500"
            aria-label={open ? 'Collapse' : 'Expand'}
            onClick={() => setOpen(!open)}>
            {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          </button>
        )}
        <div className="min-w-0 flex-1">
          {node.kind === 'stated' && subject ? (
            <button
              type="button"
              className="break-all text-left font-mono text-[11px] text-blue-700 hover:underline"
              title="Select it"
              onClick={() => props.onSelect(subject)}>
              {node.text}
            </button>
          ) : (
            <span
              className={cn(
                'break-all font-mono text-[11px]',
                node.kind === 'axiom' ? 'text-zinc-400' : node.kind === 'inconsistency' ? 'text-red-700' : '',
              )}>
              {node.text ?? 'Inconsistency'}
            </span>
          )}
          <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[10px]">
            {node.kind === 'stated' && <span className="rounded bg-emerald-50 px-1 text-emerald-700">Stated</span>}
            {node.kind === 'axiom' && (
              <span className="rounded bg-zinc-100 px-1 text-zinc-500">Holds for every graph</span>
            )}
            {node.rule && (
              <span className="rounded bg-indigo-50 px-1 text-indigo-700" title={node.rule_name ?? undefined}>
                {node.rule}
              </span>
            )}
            {node.rule_name && <span className="text-zinc-500">{node.rule_name}</span>}
            {repeated && !leaf && <span className="text-zinc-400">(see above)</span>}
          </div>
        </div>
      </div>
      {!leaf && !repeated && open && node.premises.length > 0 && (
        <ul className="ml-3 border-l pl-2">
          {node.premises.map((child) => (
            <ProofNode
              key={`${props.path}/${child}`}
              id={child}
              path={`${props.path}/${child}`}
              nodes={props.nodes}
              first={props.first}
              depth={props.depth + 1}
              onSelect={props.onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

export function ExplainPane(props: {
  request: ExplainRequest | null
  result: ExplainResult | null
  loading: boolean
  error: string | null
  /** A plain explanation given without asking the server (a derived inverse edge). */
  note?: string | null
  onSelect: (term: Term) => void
}) {
  if (!props.request) {
    return (
      <p className="p-3 text-xs text-zinc-500">
        Choose Explain on an inconsistency in Checks, Why? on an inferred row of a search answer, or an inferred item on
        the canvas to see the rules and the stated statements behind it.
      </p>
    )
  }
  const root = props.result?.root ?? null
  const first = root && props.result ? firstPaths(root, props.result.nodes) : new Map<string, string>()
  return (
    <section aria-label="Explanation" className="min-h-0 flex-1 overflow-auto p-2 text-xs">
      <p className="text-[10px] uppercase tracking-wide text-zinc-500">Why does this hold?</p>
      <p className="mb-2 break-all font-mono text-[11px] font-medium">{props.request.label}</p>
      {props.loading && (
        <p className="flex items-center gap-1 text-zinc-500">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Looking for the rules that give it...
        </p>
      )}
      {props.error && <p className="text-red-600">{props.error}</p>}
      {props.note && <p className="text-zinc-700">{props.note}</p>}
      {props.result && !props.loading && (
        <>
          <p className="mb-1 text-[10px] text-zinc-500">{props.result.mode_label}</p>
          {props.result.found && root ? (
            <ul aria-label="Proof">
              <ProofNode
                id={root}
                path={root}
                nodes={props.result.nodes}
                first={first}
                depth={0}
                onSelect={props.onSelect}
              />
            </ul>
          ) : (
            props.result.notices.map((notice) => (
              <p key={notice.code} className="text-zinc-600">
                {notice.message}
              </p>
            ))
          )}
          {props.result.stated.length > 0 && (
            <div className="mt-3 border-t pt-2">
              <p className="mb-1 font-medium">Rests on {props.result.stated.length} stated statement(s)</p>
              <ul className="space-y-0.5">
                {props.result.stated.map((item) => (
                  <li key={`${item.text}|${item.graph ? JSON.stringify(item.graph) : ''}`}>
                    <button
                      type="button"
                      className="break-all text-left font-mono text-[11px] text-blue-700 hover:underline"
                      onClick={() => props.onSelect(item.s)}>
                      {item.text}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {props.result.truncated && (
            <p className="mt-2 text-[10px] text-amber-700">The proof was cut at its bounds.</p>
          )}
        </>
      )}
    </section>
  )
}
