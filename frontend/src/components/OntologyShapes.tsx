/**
 * The Shapes view (CTR-0173 v16, PRP-0212 / UDR-0194 D10-D12).
 *
 * Node shapes as tables of property shapes, for ONE graph: a declared shapes graph of
 * this ontology, or the default graph of a shapes ontology. The table edits a SHACL Core
 * subset (`lib/ontologyShapes`); every other statement of a shape is shown read-only and
 * kept. Edits are ordinary model edits, saved with Save. The shapes ontologies another
 * ontology uses are listed read-only with Open. Each node shape shows its result count
 * from the last validation; clicking it lists them in Checks.
 */

import { ChevronRight, ExternalLink, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { iri, literal, type OntologyModel, type PrefixDecl, type Term, termKey, XSD } from '@/lib/ontologyModel'
import {
  addNodeShape,
  addPropertyShape,
  booleanLiteral,
  countLiteral,
  NODE_KINDS,
  type NodeShapeView,
  type PropertyField,
  type PropertyShapeView,
  removeNodeShape,
  removePropertyShape,
  SEVERITIES,
  SH,
  type ShapesGraph,
  setField,
  setPath,
  setTarget,
  shapesOf,
  showTerm,
  TARGET_KINDS,
  type TargetKind,
  valueLiteral,
} from '@/lib/ontologyShapes'
import { cn } from '@/lib/utils'

const DATATYPES = [
  'string',
  'boolean',
  'integer',
  'decimal',
  'double',
  'date',
  'dateTime',
  'time',
  'anyURI',
  'nonNegativeInteger',
  'positiveInteger',
].map((name) => `${XSD}${name}`)

const TARGET_LABELS: Record<TargetKind, string> = {
  targetClass: 'class',
  targetNode: 'node',
  targetSubjectsOf: 'subjects of',
  targetObjectsOf: 'objects of',
}

const inputClass =
  'h-6 w-full rounded border border-zinc-200 bg-white px-1 text-[11px] focus:border-blue-400 focus:outline-none'

/** An input that commits on Enter or blur; Escape restores. */
function CommitInput(props: {
  value: string
  onCommit: (text: string) => boolean | undefined
  placeholder?: string
  list?: string
  disabled?: boolean
  title?: string
  className?: string
  ariaLabel: string
}) {
  const [text, setText] = useState(props.value)
  const [invalid, setInvalid] = useState(false)
  useEffect(() => {
    setText(props.value)
    setInvalid(false)
  }, [props.value])
  const commit = () => {
    if (text === props.value) return
    const ok = props.onCommit(text)
    setInvalid(ok === false)
  }
  return (
    <input
      className={cn(inputClass, invalid && 'border-red-400', props.className)}
      value={text}
      placeholder={props.placeholder}
      list={props.list}
      disabled={props.disabled}
      title={invalid ? 'Not understood: use a prefixed name, <iri> or a full IRI' : props.title}
      aria-label={props.ariaLabel}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') commit()
        if (e.key === 'Escape') {
          setText(props.value)
          setInvalid(false)
        }
      }}
    />
  )
}

/** Values of an `sh:in` / `sh:languageIn` / `sh:ignoredProperties` list, as typed. */
function parseTerms(text: string, expand: (text: string) => string | null, strings = false): Term[] | null {
  const tokens = text.match(/"[^"]*"|[^\s,]+/g) ?? []
  const out: Term[] = []
  for (const token of tokens) {
    if (token.startsWith('"')) {
      out.push(literal(token.slice(1, -1)))
      continue
    }
    if (strings) {
      out.push(literal(token))
      continue
    }
    const expanded = token.startsWith('<') || token.includes(':') ? expand(token) : null
    out.push(expanded ? iri(expanded) : valueLiteral(token))
  }
  return out
}

export interface ShapesViewProps {
  model: OntologyModel
  prefixes: PrefixDecl[]
  /** The graphs whose shapes can be edited here (null = the default graph). */
  graphs: ShapesGraph[]
  graph: ShapesGraph
  onGraphChange: (graph: ShapesGraph) => void
  graphLabel: (graph: ShapesGraph) => string
  readOnly: boolean
  /** The ontology's minting base (`<base>shape-<name>`). */
  base: string
  classOptions: string[]
  propertyOptions: string[]
  onUpdate: (update: (prev: OntologyModel) => OntologyModel) => void
  /** Results per node shape (N-Triples) of the last validation. */
  resultCounts: Map<string, number>
  onShowResults: (shape: Term) => void
  /** Declared shapes ontologies (read-only here). */
  shapesOntologies: { id: string; name: string; missing: boolean }[]
  onOpenOntology: (id: string) => void
  /** Show shape (from Checks): the node shape and path to bring into view. */
  highlight: { shape: string; path: string | null; nonce: number } | null
  /** The validation state line (from GET /shacl). */
  status: ReactNode
  onConfigure: () => void
}

export function ShapesView(props: ShapesViewProps) {
  const compact = useMemo(() => {
    const prefixes = props.prefixes
    return (value: string) => {
      let best: PrefixDecl | null = null
      for (const decl of prefixes) {
        if (decl.iri && value.startsWith(decl.iri) && (!best || decl.iri.length > best.iri.length)) best = decl
      }
      if (best) {
        const local = value.slice(best.iri.length)
        if (/^[A-Za-z_][\w-]*$/.test(local) || local === '') return `${best.prefix}:${local}`
      }
      return `<${value}>`
    }
  }, [props.prefixes])
  const expand = useMemo(() => {
    const prefixes = props.prefixes
    return (text: string): string | null => {
      const value = text.trim()
      if (!value) return null
      if (value.startsWith('<') && value.endsWith('>')) return value.slice(1, -1) || null
      const colon = value.indexOf(':')
      if (colon < 0) return null
      const decl = prefixes.find((p) => p.prefix === value.slice(0, colon))
      if (decl) return decl.iri + value.slice(colon + 1)
      return /^[A-Za-z][A-Za-z0-9+.-]*:\S+$/.test(value) ? value : null
    }
  }, [props.prefixes])
  const shapes = useMemo(() => shapesOf(props.model, props.graph, compact), [props.model, props.graph, compact])
  const [newName, setNewName] = useState('')
  const [newTarget, setNewTarget] = useState('')
  const listId = useMemo(() => `shapes-${Math.random().toString(36).slice(2, 8)}`, [])
  const g = props.graph
  const edit = (update: (prev: OntologyModel) => OntologyModel) => {
    if (!props.readOnly) props.onUpdate(update)
  }
  const addShape = () => {
    const name = newName.trim().replace(/\s+/g, '-')
    if (!name) return
    const shapeIri =
      /^[A-Za-z][A-Za-z0-9+.-]*:/.test(name) && expand(name) ? (expand(name) as string) : `${props.base}shape-${name}`
    const target = newTarget.trim() ? expand(newTarget) : null
    edit((prev) => addNodeShape(prev, g, shapeIri, target ?? undefined))
    setNewName('')
    setNewTarget('')
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <datalist id={`${listId}-classes`}>
        {props.classOptions.map((value) => (
          <option key={value} value={compact(value)} />
        ))}
      </datalist>
      <datalist id={`${listId}-properties`}>
        {props.propertyOptions.map((value) => (
          <option key={value} value={compact(value)} />
        ))}
      </datalist>
      <datalist id={`${listId}-datatypes`}>
        {DATATYPES.map((value) => (
          <option key={value} value={compact(value)} />
        ))}
      </datalist>
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b bg-white px-2 py-1 text-[11px] text-zinc-600">
        {props.graphs.length > 1 && (
          <label className="flex items-center gap-1">
            Shapes graph
            <select
              className="h-6 rounded border border-zinc-200 bg-white px-1 text-[11px]"
              value={props.graphs.findIndex((item) => (item ? termKey(item) : '') === (g ? termKey(g) : ''))}
              onChange={(e) => props.onGraphChange(props.graphs[Number(e.target.value)] ?? null)}>
              {props.graphs.map((item, i) => (
                <option key={item ? termKey(item) : 'default'} value={i}>
                  {props.graphLabel(item)}
                </option>
              ))}
            </select>
          </label>
        )}
        {props.graphs.length === 1 && <span className="text-zinc-500">{props.graphLabel(g)}</span>}
        <span className="min-w-0 flex-1 truncate">{props.status}</span>
        <Button variant="outline" size="sm" className="h-6 text-[11px]" onClick={props.onConfigure}>
          <ShieldCheck className="mr-1 h-3 w-3" /> Validation settings
        </Button>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-zinc-50 p-3">
        {!props.readOnly && props.graphs.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 rounded border border-dashed bg-white p-2 text-[11px]">
            <span className="font-medium text-zinc-700">New node shape</span>
            <input
              className={cn(inputClass, 'w-40')}
              placeholder="name (e.g. Book)"
              aria-label="New node shape name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && addShape()}
            />
            <span>for class</span>
            <input
              className={cn(inputClass, 'w-48')}
              placeholder="ex:Book (optional)"
              aria-label="New node shape target class"
              list={`${listId}-classes`}
              value={newTarget}
              onChange={(e) => setNewTarget(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && addShape()}
            />
            <Button size="sm" className="h-6 text-[11px]" onClick={addShape} disabled={!newName.trim()}>
              <Plus className="mr-0.5 h-3 w-3" /> Node shape
            </Button>
          </div>
        )}
        {props.graphs.length === 0 && props.shapesOntologies.length === 0 && (
          <p className="text-xs text-zinc-500">
            This ontology has no shapes. Declare a shapes graph or a shapes ontology in the validation settings.
          </p>
        )}
        {props.graphs.length > 0 && shapes.length === 0 && (
          <p className="text-xs text-zinc-500">No node shapes in this graph yet.</p>
        )}
        {shapes.map((shape) => (
          <NodeShapeCard
            key={termKey(shape.term)}
            shape={shape}
            graph={g}
            compact={compact}
            expand={expand}
            listId={listId}
            readOnly={props.readOnly}
            edit={edit}
            resultCount={props.resultCounts.get(termKey(shape.term)) ?? 0}
            onShowResults={() => props.onShowResults(shape.term)}
            highlight={props.highlight && props.highlight.shape === termKey(shape.term) ? props.highlight : null}
          />
        ))}
        {props.shapesOntologies.length > 0 && (
          <div className="rounded border bg-white p-2 text-[11px]">
            <div className="mb-1 font-medium text-zinc-700">
              Shapes ontologies used by this ontology (read-only here)
            </div>
            {props.shapesOntologies.map((item) => (
              <div key={item.id} className="flex items-center gap-2 py-0.5">
                <span className={cn('min-w-0 flex-1 truncate', item.missing && 'text-red-600')}>
                  {item.name} {item.missing ? '(missing)' : ''}
                </span>
                {!item.missing && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-6 text-[11px]"
                    onClick={() => props.onOpenOntology(item.id)}>
                    <ExternalLink className="mr-1 h-3 w-3" /> Open
                  </Button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function NodeShapeCard(props: {
  shape: NodeShapeView
  graph: ShapesGraph
  compact: (value: string) => string
  expand: (text: string) => string | null
  listId: string
  readOnly: boolean
  edit: (update: (prev: OntologyModel) => OntologyModel) => void
  resultCount: number
  onShowResults: () => void
  highlight: { shape: string; path: string | null; nonce: number } | null
}) {
  const { shape, graph: g, compact, expand, edit } = props
  const ref = useRef<HTMLDivElement>(null)
  const [newPath, setNewPath] = useState('')
  const [targetKind, setTargetKind] = useState<TargetKind>('targetClass')
  const [targetText, setTargetText] = useState('')
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll again for every Show shape (nonce)
  useEffect(() => {
    if (props.highlight) ref.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [props.highlight?.nonce])
  const severity = shape.values.severity?.[0]
  const message = shape.values.message?.[0]
  const addRow = () => {
    const pathIri = expand(newPath)
    if (!pathIri) return
    edit((prev) => addPropertyShape(prev, g, shape.term, pathIri)[0])
    setNewPath('')
  }
  const addTarget = () => {
    const value = targetText.trim()
    if (!value) return
    const expanded = expand(value)
    if (!expanded) return
    edit((prev) => setTarget(prev, g, shape.term, targetKind, iri(expanded), true))
    setTargetText('')
  }
  return (
    <div
      ref={ref}
      className={cn('rounded border bg-white text-[11px]', props.highlight && 'ring-2 ring-blue-300')}
      data-shape={termKey(shape.term)}>
      <div className="flex flex-wrap items-center gap-2 border-b px-2 py-1.5">
        <span className="font-semibold text-zinc-800" title={termKey(shape.term)}>
          {showTerm(shape.term, compact)}
        </span>
        {shape.self && <span className="rounded bg-zinc-100 px-1 text-[10px] text-zinc-500">property shape</span>}
        <span className="text-zinc-500">targets</span>
        {shape.targets.map((target) => (
          <span
            key={`${target.kind}${termKey(target.value)}`}
            className="flex items-center gap-0.5 rounded bg-indigo-50 px-1 text-indigo-800">
            {TARGET_LABELS[target.kind]} {showTerm(target.value, compact)}
            {!props.readOnly && (
              <button
                type="button"
                className="text-indigo-400 hover:text-red-600"
                aria-label={`Remove target ${showTerm(target.value, compact)}`}
                onClick={() => edit((prev) => setTarget(prev, g, shape.term, target.kind, target.value, false))}>
                x
              </button>
            )}
          </span>
        ))}
        {!props.readOnly && (
          <span className="flex items-center gap-0.5">
            <select
              className="h-6 rounded border border-zinc-200 bg-white px-0.5 text-[10px]"
              aria-label="Target kind"
              value={targetKind}
              onChange={(e) => setTargetKind(e.target.value as TargetKind)}>
              {TARGET_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {TARGET_LABELS[kind]}
                </option>
              ))}
            </select>
            <input
              className={cn(inputClass, 'w-32')}
              placeholder="ex:Book"
              aria-label="New target"
              list={targetKind === 'targetClass' ? `${props.listId}-classes` : `${props.listId}-properties`}
              value={targetText}
              onChange={(e) => setTargetText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && addTarget()}
            />
          </span>
        )}
        {!shape.self && (
          <div className="flex items-center gap-1 text-zinc-600">
            severity
            <SeveritySelect
              value={severity}
              disabled={props.readOnly}
              onChange={(value) => edit((prev) => setField(prev, g, shape.term, 'severity', value ? [iri(value)] : []))}
            />
          </div>
        )}
        <span className="flex-1" />
        <button
          type="button"
          className={cn(
            'rounded px-1.5 py-0.5 text-[10px]',
            props.resultCount > 0 ? 'bg-amber-100 text-amber-800 hover:underline' : 'bg-emerald-50 text-emerald-700',
          )}
          title="Results of the last validation; click to list them in Checks"
          onClick={props.onShowResults}>
          {props.resultCount > 0 ? `${props.resultCount} result${props.resultCount === 1 ? '' : 's'}` : 'no results'}
        </button>
        {!props.readOnly && (
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-400 hover:text-red-600"
            aria-label={`Delete ${showTerm(shape.term, compact)}`}
            title="Delete this node shape (and its property shapes)"
            onClick={() => edit((prev) => removeNodeShape(prev, g, shape.term))}>
            <Trash2 className="h-3 w-3" />
          </Button>
        )}
      </div>
      {!shape.self && (
        <div className="flex flex-wrap items-center gap-2 border-b px-2 py-1 text-zinc-600">
          <div className="flex min-w-[240px] flex-1 items-center gap-1">
            message
            <CommitInput
              ariaLabel="Node shape message"
              value={message?.type === 'literal' ? message.value : ''}
              disabled={props.readOnly}
              onCommit={(text) => {
                edit((prev) =>
                  setField(prev, g, shape.term, 'message', text.trim() ? [messageLiteral(text, message)] : []),
                )
                return true
              }}
            />
          </div>
          <label className="flex items-center gap-1" title="Only the listed properties (and ignored ones) are allowed">
            <input
              type="checkbox"
              disabled={props.readOnly}
              checked={shape.values.closed?.[0]?.type === 'literal' && shape.values.closed[0].value === 'true'}
              onChange={(e) =>
                edit((prev) => setField(prev, g, shape.term, 'closed', e.target.checked ? [booleanLiteral(true)] : []))
              }
            />
            closed
          </label>
          {shape.values.closed && (
            <div className="flex min-w-[200px] items-center gap-1">
              ignored
              <CommitInput
                ariaLabel="Ignored properties"
                value={(shape.values.ignoredProperties ?? []).map((t) => showTerm(t, compact)).join(' ')}
                disabled={props.readOnly}
                placeholder="rdf:type ex:note"
                onCommit={(text) => {
                  const terms = parseTerms(text, expand)
                  if (!terms || terms.some((t) => t.type !== 'iri')) return false
                  edit((prev) => setField(prev, g, shape.term, 'ignoredProperties', terms))
                  return true
                }}
              />
            </div>
          )}
          <label className="flex items-center gap-1" title="A deactivated shape is not validated">
            <input
              type="checkbox"
              disabled={props.readOnly}
              checked={
                shape.values.deactivated?.[0]?.type === 'literal' && shape.values.deactivated[0].value === 'true'
              }
              onChange={(e) =>
                edit((prev) =>
                  setField(prev, g, shape.term, 'deactivated', e.target.checked ? [booleanLiteral(true)] : []),
                )
              }
            />
            deactivated
          </label>
        </div>
      )}
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr className="bg-zinc-50 text-left text-[10px] uppercase tracking-wide text-zinc-500">
            <th className="w-[20%] px-2 py-1 font-medium">path</th>
            <th className="w-[20%] px-2 py-1 font-medium">value type</th>
            <th className="w-[7%] px-2 py-1 font-medium">min</th>
            <th className="w-[7%] px-2 py-1 font-medium">max</th>
            <th className="w-[24%] px-2 py-1 font-medium">other</th>
            <th className="w-[18%] px-2 py-1 font-medium">message</th>
            <th className="w-[4%]" />
          </tr>
        </thead>
        <tbody>
          {shape.properties.map((row) => (
            <PropertyRow
              key={termKey(row.term)}
              row={row}
              shape={shape}
              graph={g}
              compact={compact}
              expand={expand}
              listId={props.listId}
              readOnly={props.readOnly}
              edit={edit}
              highlighted={props.highlight !== null && props.highlight.path === row.path?.text}
            />
          ))}
        </tbody>
      </table>
      {!props.readOnly && !shape.self && (
        <div className="flex items-center gap-1 border-t px-2 py-1">
          <input
            className={cn(inputClass, 'w-48')}
            placeholder="path, e.g. ex:isbn"
            aria-label="New property shape path"
            list={`${props.listId}-properties`}
            value={newPath}
            onChange={(e) => setNewPath(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addRow()}
          />
          <Button variant="outline" size="sm" className="h-6 text-[11px]" onClick={addRow} disabled={!expand(newPath)}>
            <Plus className="mr-0.5 h-3 w-3" /> Property
          </Button>
        </div>
      )}
      {shape.readOnly.length > 0 && (
        <div className="border-t px-2 py-1 font-mono text-[10px] text-zinc-500">
          <span className="mr-1 font-sans text-zinc-400">read-only:</span>
          {shape.readOnly.join(' ; ')}
        </div>
      )}
    </div>
  )
}

function messageLiteral(text: string, previous: Term | undefined): Term {
  const language = previous?.type === 'literal' ? previous.language : undefined
  return literal(text.trim(), language ? { language } : {})
}

function SeveritySelect(props: {
  value: Term | undefined
  disabled: boolean
  onChange: (value: string | null) => void
}) {
  const current = props.value?.type === 'iri' ? props.value.value : ''
  return (
    <select
      className="h-6 rounded border border-zinc-200 bg-white px-0.5 text-[10px]"
      aria-label="Severity"
      disabled={props.disabled}
      value={current}
      onChange={(e) => props.onChange(e.target.value || null)}>
      <option value="">(Violation)</option>
      {SEVERITIES.map((name) => (
        <option key={name} value={`${SH}${name}`}>
          {name}
        </option>
      ))}
      {current && !current.startsWith(SH) && <option value={current}>{current}</option>}
    </select>
  )
}

const OTHER_FIELDS: {
  field: PropertyField
  label: string
  kind: 'term' | 'count' | 'iri' | 'list' | 'langs' | 'bool' | 'text'
}[] = [
  { field: 'name', label: 'name', kind: 'text' },
  { field: 'in', label: 'in', kind: 'list' },
  { field: 'hasValue', label: 'has value', kind: 'term' },
  { field: 'pattern', label: 'pattern', kind: 'text' },
  { field: 'flags', label: 'flags', kind: 'text' },
  { field: 'minLength', label: 'min length', kind: 'count' },
  { field: 'maxLength', label: 'max length', kind: 'count' },
  { field: 'minInclusive', label: '>=', kind: 'term' },
  { field: 'maxInclusive', label: '<=', kind: 'term' },
  { field: 'minExclusive', label: '>', kind: 'term' },
  { field: 'maxExclusive', label: '<', kind: 'term' },
  { field: 'languageIn', label: 'languages', kind: 'langs' },
  { field: 'uniqueLang', label: 'unique language', kind: 'bool' },
  { field: 'node', label: 'node shape', kind: 'iri' },
  { field: 'deactivated', label: 'deactivated', kind: 'bool' },
]

function PropertyRow(props: {
  row: PropertyShapeView
  shape: NodeShapeView
  graph: ShapesGraph
  compact: (value: string) => string
  expand: (text: string) => string | null
  listId: string
  readOnly: boolean
  edit: (update: (prev: OntologyModel) => OntologyModel) => void
  highlighted: boolean
}) {
  const { row, graph: g, compact, expand, edit } = props
  const [open, setOpen] = useState(false)
  const v = row.values
  const valueKind = v.datatype ? 'datatype' : v.class ? 'class' : v.nodeKind ? 'nodeKind' : ''
  const valueTerm = (v.datatype ?? v.class ?? v.nodeKind)?.[0]
  const set = (field: string, values: Term[]) => edit((prev) => setField(prev, g, row.term, field, values))
  const summary = OTHER_FIELDS.filter((item) => v[item.field]?.length).map((item) => {
    const values = v[item.field] ?? []
    const shown =
      item.kind === 'list' || item.kind === 'langs'
        ? `(${values.map((t) => showTerm(t, compact)).join(' ')})`
        : showTerm(values[0], compact)
    return `${item.label} ${shown}`
  })
  const message = v.message?.[0]
  return (
    <>
      <tr className={cn('border-t align-top', props.highlighted && 'bg-blue-50')}>
        <td className="px-2 py-1">
          {row.path?.editable && !props.readOnly ? (
            <CommitInput
              ariaLabel="Path"
              value={row.path.text}
              list={`${props.listId}-properties`}
              onCommit={(text) => {
                const inverse = text.trim().startsWith('^')
                const target = expand(text.trim().replace(/^\^/, ''))
                if (!target) return false
                edit((prev) => setPath(prev, g, row.term, target, inverse))
                return true
              }}
            />
          ) : (
            <span className="font-mono text-[10px]" title={row.path?.editable ? undefined : 'Complex path (read-only)'}>
              {row.path?.text ?? '(no path)'}
            </span>
          )}
        </td>
        <td className="px-2 py-1">
          <div className="flex gap-0.5">
            <select
              className="h-6 w-[70px] shrink-0 rounded border border-zinc-200 bg-white px-0.5 text-[10px]"
              aria-label="Value type kind"
              disabled={props.readOnly}
              value={valueKind}
              onChange={(e) => {
                const kind = e.target.value
                edit((prev) => {
                  let next = prev
                  for (const field of ['datatype', 'class', 'nodeKind']) next = setField(next, g, row.term, field, [])
                  if (kind === 'datatype') next = setField(next, g, row.term, 'datatype', [iri(`${XSD}string`)])
                  if (kind === 'nodeKind') next = setField(next, g, row.term, 'nodeKind', [iri(`${SH}IRI`)])
                  return next
                })
              }}>
              <option value="">any</option>
              <option value="datatype">datatype</option>
              <option value="class">class</option>
              <option value="nodeKind">node kind</option>
            </select>
            {valueKind === 'nodeKind' ? (
              <select
                className="h-6 min-w-0 flex-1 rounded border border-zinc-200 bg-white px-0.5 text-[10px]"
                aria-label="Node kind"
                disabled={props.readOnly}
                value={valueTerm?.type === 'iri' ? valueTerm.value : ''}
                onChange={(e) => set('nodeKind', [iri(e.target.value)])}>
                {NODE_KINDS.map((kind) => (
                  <option key={kind} value={`${SH}${kind}`}>
                    {kind}
                  </option>
                ))}
              </select>
            ) : valueKind ? (
              <CommitInput
                ariaLabel={valueKind === 'datatype' ? 'Datatype' : 'Class'}
                value={valueTerm ? showTerm(valueTerm, compact) : ''}
                list={valueKind === 'datatype' ? `${props.listId}-datatypes` : `${props.listId}-classes`}
                disabled={props.readOnly}
                onCommit={(text) => {
                  const target = expand(text)
                  if (!target) return false
                  set(valueKind, [iri(target)])
                  return true
                }}
              />
            ) : null}
          </div>
        </td>
        <td className="px-2 py-1">
          <CommitInput
            ariaLabel="Min count"
            value={v.minCount?.[0]?.type === 'literal' ? v.minCount[0].value : ''}
            disabled={props.readOnly}
            placeholder="-"
            onCommit={(text) => {
              if (!text.trim()) {
                set('minCount', [])
                return true
              }
              const value = countLiteral(text)
              if (!value) return false
              set('minCount', [value])
              return true
            }}
          />
        </td>
        <td className="px-2 py-1">
          <CommitInput
            ariaLabel="Max count"
            value={v.maxCount?.[0]?.type === 'literal' ? v.maxCount[0].value : ''}
            disabled={props.readOnly}
            placeholder="-"
            onCommit={(text) => {
              if (!text.trim()) {
                set('maxCount', [])
                return true
              }
              const value = countLiteral(text)
              if (!value) return false
              set('maxCount', [value])
              return true
            }}
          />
        </td>
        <td className="px-2 py-1">
          <button
            type="button"
            className="flex w-full items-start gap-0.5 text-left text-[10px] text-zinc-600 hover:text-blue-700"
            aria-expanded={open}
            onClick={() => setOpen(!open)}>
            <ChevronRight className={cn('mt-0.5 h-3 w-3 shrink-0 transition-transform', open && 'rotate-90')} />
            <span className="min-w-0 break-words">
              {[...summary, ...(v.severity ? [`severity ${showTerm(v.severity[0], compact)}`] : [])].join(', ') ||
                (row.readOnly.length ? '' : 'none')}
              {row.readOnly.length > 0 && (
                <span className="text-zinc-400">
                  {summary.length ? '; ' : ''}
                  {row.readOnly.join(' ; ')} (read-only)
                </span>
              )}
            </span>
          </button>
        </td>
        <td className="px-2 py-1">
          <CommitInput
            ariaLabel="Message"
            value={message?.type === 'literal' ? message.value : ''}
            disabled={props.readOnly}
            onCommit={(text) => {
              set('message', text.trim() ? [messageLiteral(text, message)] : [])
              return true
            }}
          />
        </td>
        <td className="px-1 py-1 text-right">
          {!props.readOnly && !props.shape.self && (
            <button
              type="button"
              className="text-zinc-400 hover:text-red-600"
              aria-label={`Delete the property shape on ${row.path?.text ?? 'no path'}`}
              title="Delete this property shape"
              onClick={() => edit((prev) => removePropertyShape(prev, g, props.shape.term, row.term))}>
              <Trash2 className="h-3 w-3" />
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="bg-zinc-50">
          <td colSpan={7} className="px-2 py-1.5">
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 md:grid-cols-3">
              <div className="flex items-center gap-1 text-[10px] text-zinc-600">
                <span className="w-20 shrink-0">severity</span>
                <SeveritySelect
                  value={v.severity?.[0]}
                  disabled={props.readOnly}
                  onChange={(value) => set('severity', value ? [iri(value)] : [])}
                />
              </div>
              {OTHER_FIELDS.map((item) => (
                <div key={item.field} className="flex items-center gap-1 text-[10px] text-zinc-600">
                  <span className="w-20 shrink-0">{item.label}</span>
                  {item.kind === 'bool' ? (
                    <input
                      type="checkbox"
                      aria-label={item.label}
                      disabled={props.readOnly}
                      checked={isTrue(v[item.field]?.[0])}
                      onChange={(e) => set(item.field, e.target.checked ? [booleanLiteral(true)] : [])}
                    />
                  ) : (
                    <CommitInput
                      ariaLabel={item.label}
                      disabled={props.readOnly}
                      value={fieldText(v[item.field], item.kind, compact)}
                      onCommit={(text) => {
                        if (!text.trim()) {
                          set(item.field, [])
                          return true
                        }
                        const parsed = parseField(text, item.kind, expand)
                        if (!parsed) return false
                        set(item.field, parsed)
                        return true
                      }}
                    />
                  )}
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function isTrue(term: Term | undefined): boolean {
  return term?.type === 'literal' && (term.value === 'true' || term.value === '1')
}

function fieldText(values: Term[] | undefined, kind: string, compact: (value: string) => string): string {
  if (!values || values.length === 0) return ''
  if (kind === 'text') return values[0].type === 'literal' ? values[0].value : showTerm(values[0], compact)
  if (kind === 'langs') return values.map((t) => (t.type === 'literal' ? t.value : showTerm(t, compact))).join(' ')
  if (kind === 'count') return values[0].type === 'literal' ? values[0].value : ''
  if (kind === 'list') return values.map((t) => showTerm(t, compact)).join(' ')
  return showTerm(values[0], compact)
}

function parseField(text: string, kind: string, expand: (text: string) => string | null): Term[] | null {
  switch (kind) {
    case 'text':
      return [literal(text.trim())]
    case 'count': {
      const value = countLiteral(text)
      return value ? [value] : null
    }
    case 'iri': {
      const target = expand(text)
      return target ? [iri(target)] : null
    }
    case 'list':
      return parseTerms(text, expand)
    case 'langs':
      return parseTerms(text, expand, true)
    default: {
      const value = text.trim()
      if (value.startsWith('"') && value.endsWith('"')) return [literal(value.slice(1, -1))]
      const target = value.startsWith('<') || /^[A-Za-z][\w-]*:/.test(value) ? expand(value) : null
      return [target ? iri(target) : valueLiteral(value)]
    }
  }
}
