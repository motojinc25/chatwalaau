/**
 * The inspector's Checks tab (CTR-0173 v11, PRP-0206 / UDR-0188).
 *
 * Lists the findings `runChecks` computes over the whole model, grouped per check:
 * warnings open, information collapsed. Findings never block a save and are never
 * stored (D1). The only fixes that change data are single statement edits -- declare
 * a class, remove one "is a" link of a cycle (D3); everything else selects its
 * subject. "Ignore" is remembered in this browser only (D5).
 */

import { ChevronRight, Eye, EyeOff } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { fieldLabel } from '@/components/OntologyInspector'
import { Button } from '@/components/ui/button'
import {
  CHECKS,
  type CheckCode,
  type CheckEdge,
  compactIri,
  type Finding,
  type PrefixDecl,
  type Term,
  termKey,
} from '@/lib/ontologyModel'
import { cn } from '@/lib/utils'

// Per-browser memory of ignored findings, never saved in the ontology (UDR-0188 D5).
const IGNORED_CHECKS_STORAGE_KEY = 'chatwalaau.ontology.ignoredChecks'

/** What "Ignore" remembers for a finding: its check and its key. */
export function findingId(finding: Finding): string {
  return `${finding.code}|${finding.key}`
}

function readIgnored(ontologyId: string): Set<string> {
  try {
    const raw = localStorage.getItem(IGNORED_CHECKS_STORAGE_KEY)
    const all: unknown = raw ? JSON.parse(raw) : {}
    const list = all && typeof all === 'object' ? (all as Record<string, unknown>)[ontologyId] : undefined
    return new Set(Array.isArray(list) ? list.filter((v): v is string => typeof v === 'string') : [])
  } catch {
    return new Set()
  }
}

function writeIgnored(ontologyId: string, ignored: Set<string>): void {
  try {
    const raw = localStorage.getItem(IGNORED_CHECKS_STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : {}
    const all = parsed && typeof parsed === 'object' ? (parsed as Record<string, string[]>) : {}
    if (ignored.size === 0) delete all[ontologyId]
    else all[ontologyId] = [...ignored]
    localStorage.setItem(IGNORED_CHECKS_STORAGE_KEY, JSON.stringify(all))
  } catch {
    // storage blocked: the choice lasts for this session only
  }
}

/** The ignored findings of one ontology, in this browser. */
export function useIgnoredChecks(ontologyId: string | null) {
  const [ignored, setIgnored] = useState<Set<string>>(() => (ontologyId ? readIgnored(ontologyId) : new Set()))
  useEffect(() => {
    setIgnored(ontologyId ? readIgnored(ontologyId) : new Set())
  }, [ontologyId])
  const update = useCallback(
    (id: string, on: boolean) => {
      setIgnored((prev) => {
        const next = new Set(prev)
        if (on) next.add(id)
        else next.delete(id)
        if (ontologyId) writeIgnored(ontologyId, next)
        return next
      })
    },
    [ontologyId],
  )
  return { ignored, ignore: (id: string) => update(id, true), unignore: (id: string) => update(id, false) }
}

export function OntologyChecksPane(props: {
  findings: Finding[]
  ignored: Set<string>
  onIgnore: (id: string) => void
  onUnignore: (id: string) => void
  prefixes: PrefixDecl[]
  readOnly: boolean
  onSelect: (term: Term) => void
  onDeclare: (finding: Finding) => void
  onDeclareAll: (namespace: string, classes: string[]) => void
  onRemoveEdge: (edge: CheckEdge) => void
}) {
  const [showIgnored, setShowIgnored] = useState(false)
  const visible = useMemo(
    () => props.findings.filter((f) => showIgnored || !props.ignored.has(findingId(f))),
    [props.findings, props.ignored, showIgnored],
  )
  const byCode = useMemo(() => {
    const map = new Map<CheckCode, Finding[]>()
    for (const f of visible) map.set(f.code, [...(map.get(f.code) ?? []), f])
    return map
  }, [visible])
  const hidden = props.findings.length - props.findings.filter((f) => !props.ignored.has(findingId(f))).length
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b p-2 text-[11px] text-zinc-600">
        <span>
          Findings never block saving. Most of them are legal RDFS / OWL -- they are worth a look, not errors.
        </span>
        <label className="flex shrink-0 items-center gap-1" title="Also list the findings you ignored (this browser)">
          <input type="checkbox" checked={showIgnored} onChange={(e) => setShowIgnored(e.target.checked)} />
          Show ignored{hidden > 0 ? ` (${hidden})` : ''}
        </label>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {CHECKS.filter((check) => byCode.has(check.code)).map((check) => (
          <CheckGroup
            {...props}
            key={check.code}
            title={check.title}
            severity={check.severity}
            defaultOpen={check.severity === 'warning'}
            findings={byCode.get(check.code) ?? []}
          />
        ))}
        {visible.length === 0 && <p className="p-3 text-xs text-zinc-400">No findings.</p>}
      </div>
    </div>
  )
}

function CheckGroup(props: {
  title: string
  severity: 'warning' | 'info'
  defaultOpen: boolean
  findings: Finding[]
  ignored: Set<string>
  onIgnore: (id: string) => void
  onUnignore: (id: string) => void
  prefixes: PrefixDecl[]
  readOnly: boolean
  onSelect: (term: Term) => void
  onDeclare: (finding: Finding) => void
  onDeclareAll: (namespace: string, classes: string[]) => void
  onRemoveEdge: (edge: CheckEdge) => void
}) {
  const [open, setOpen] = useState(props.defaultOpen)
  // Declare all: per OWN namespace only; other vocabularies are declared one at a time (D3).
  const ownNamespaces = useMemo(() => {
    const map = new Map<string, string[]>()
    for (const f of props.findings) {
      if (f.code !== 'undeclared_class' || !f.own || !f.namespace || props.ignored.has(findingId(f))) continue
      map.set(f.namespace, [...(map.get(f.namespace) ?? []), f.key])
    }
    return map
  }, [props.findings, props.ignored])
  return (
    <div className="border-b">
      <button
        type="button"
        className="flex w-full items-center gap-1 px-2 py-1.5 text-left text-[11px] font-medium text-zinc-800 hover:bg-zinc-50"
        aria-expanded={open}
        onClick={() => setOpen(!open)}>
        <ChevronRight className={cn('h-3 w-3 shrink-0 transition-transform', open && 'rotate-90')} />
        <span className="min-w-0 flex-1 truncate">
          {props.title} ({props.findings.length})
        </span>
        <span
          className={cn(
            'shrink-0 rounded px-1 text-[10px]',
            props.severity === 'warning' ? 'bg-amber-100 text-amber-800' : 'bg-sky-50 text-sky-700',
          )}>
          {props.severity}
        </span>
      </button>
      {open && (
        <div className="space-y-1 px-2 pb-2">
          {!props.readOnly &&
            [...ownNamespaces].map(([namespace, classes]) => (
              <div key={namespace} className="flex items-center gap-1 text-[10px] text-zinc-500">
                <span className="min-w-0 flex-1 truncate" title={namespace}>
                  own namespace {namespace}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 shrink-0 text-[10px]"
                  onClick={() => props.onDeclareAll(namespace, classes)}>
                  Declare all ({classes.length})
                </Button>
              </div>
            ))}
          {props.findings.map((f) => (
            <FindingRow key={findingId(f)} finding={f} {...props} />
          ))}
        </div>
      )}
    </div>
  )
}

function FindingRow(props: {
  finding: Finding
  ignored: Set<string>
  onIgnore: (id: string) => void
  onUnignore: (id: string) => void
  prefixes: PrefixDecl[]
  readOnly: boolean
  onSelect: (term: Term) => void
  onDeclare: (finding: Finding) => void
  onRemoveEdge: (edge: CheckEdge) => void
}) {
  const { finding: f } = props
  const id = findingId(f)
  const isIgnored = props.ignored.has(id)
  const name = (term: Term) => (term.type === 'iri' ? compactIri(term.value, props.prefixes) : termKey(term))
  return (
    <div className={cn('rounded border px-1.5 py-1 text-[11px]', isIgnored && 'opacity-50')}>
      <p className="text-zinc-700">{f.message}</p>
      <div className="mt-0.5 flex flex-wrap items-center gap-1">
        {f.subjects.map((term, i) => (
          <button
            key={termKey(term)}
            type="button"
            className="max-w-[180px] truncate rounded bg-zinc-100 px-1 text-[10px] text-blue-700 hover:underline"
            title={termKey(term)}
            onClick={() => props.onSelect(term)}>
            {i === 1 && f.code === 'undeclared_class' ? `used by ${name(term)}` : name(term)}
          </button>
        ))}
        {f.code === 'undeclared_class' && f.namespace && !f.own && (
          <span className="text-[10px] text-zinc-400" title={f.namespace}>
            other vocabulary
          </span>
        )}
        <span className="flex-1" />
        {!props.readOnly && f.code === 'undeclared_class' && (
          <Button variant="outline" size="sm" className="h-6 text-[10px]" onClick={() => props.onDeclare(f)}>
            {f.own ? 'Declare as class' : 'Declare as class...'}
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-1 text-[10px] text-zinc-500"
          title={isIgnored ? 'Show this finding again' : 'Hide this finding (remembered in this browser)'}
          onClick={() => (isIgnored ? props.onUnignore(id) : props.onIgnore(id))}>
          {isIgnored ? <Eye className="mr-0.5 h-3 w-3" /> : <EyeOff className="mr-0.5 h-3 w-3" />}
          {isIgnored ? 'Unignore' : 'Ignore'}
        </Button>
      </div>
      {f.edges && f.edges.length > 0 && (
        <div className="mt-1 space-y-0.5">
          <span className={fieldLabel}>{f.code === 'self_subclass' ? 'Link' : 'Links in the cycle'}</span>
          {f.edges.map((edge) => (
            <div key={`${edge.sub}|${edge.sup}`} className="flex items-center gap-1 text-[10px] text-zinc-600">
              <span className="min-w-0 flex-1 truncate">
                {compactIri(edge.sub, props.prefixes)} {edge.p.endsWith('subClassOf') ? 'is a' : 'is a subproperty of'}{' '}
                {compactIri(edge.sup, props.prefixes)}
              </span>
              {!props.readOnly && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 shrink-0 text-[10px]"
                  title="Remove this one statement"
                  onClick={() => props.onRemoveEdge(edge)}>
                  Remove
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
