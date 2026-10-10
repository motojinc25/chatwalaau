/**
 * Ontology inspector (CTR-0173 v5, PRP-0198 / PRP-0200, UDR-0180 D9, UDR-0182).
 *
 * Every statement of the ontology is visible and editable here: the "All
 * statements" list of a selected resource, the Resources tab listing every
 * resource the canvas does not draw, the typed Term editor (IRI / blank node /
 * literal with datatype, language and direction / RDF 1.2 triple term), and the
 * Document dialog for the kept declarations (prefixes, base, VERSION).
 *
 * In a dataset each statement shows its graph and the statement editor can move
 * it to another graph (UDR-0182 D1). Every statement row offers "Add annotation",
 * which creates a reifier with an IRI (UDR-0182 D8).
 *
 * Long lists render in pages ("Show more") instead of all at once; there is no
 * virtualization dependency.
 */

import { ChevronRight, Info, Link2, MessageSquarePlus, Pencil, Plus, Trash2, TriangleAlert, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  asTriple,
  blankLabels,
  blankReferenceCounts,
  compactIri,
  type Diagnostic,
  type Direction,
  expandIri,
  findResource,
  type GraphTerm,
  graphKey,
  inspectorResources,
  isIndividual,
  listItems,
  literal,
  type OntologyDocument,
  type OntologyModel,
  type PrefixDecl,
  pickLiteralIndex,
  RDF_DIR_LANG_STRING,
  RDF_LANG_STRING,
  RDF_REIFIES,
  RDF_TYPE,
  RDFS_LABEL,
  RDFS_SUB_PROPERTY_OF,
  type Resource,
  type Role,
  resourceKey,
  type Statement,
  type StatementInput,
  type Term,
  termKey,
  usesUndeclaredClass,
  XSD,
  XSD_STRING,
} from '@/lib/ontologyModel'
import { cn } from '@/lib/utils'

const PAGE = 100
const MAX_NEST = 4

export const fieldLabel = 'mb-1 block text-[10px] font-medium uppercase tracking-wide text-zinc-500'
export const fieldInput = 'w-full rounded-md border bg-transparent px-2 py-1.5 text-xs'

const ROLE_LABEL: Record<Role, string> = {
  entity: 'Entity',
  object_property: 'Object property',
  datatype_property: 'Datatype property',
  other: 'Resource',
}

const COMMON_DATATYPES = [
  XSD_STRING,
  `${XSD}integer`,
  `${XSD}decimal`,
  `${XSD}double`,
  `${XSD}boolean`,
  `${XSD}date`,
  `${XSD}dateTime`,
  `${XSD}anyURI`,
  'http://www.w3.org/1999/02/22-rdf-syntax-ns#HTML',
  'http://www.w3.org/1999/02/22-rdf-syntax-ns#XMLLiteral',
  'http://www.w3.org/1999/02/22-rdf-syntax-ns#JSON',
]

// ---- Shared context ------------------------------------------------------------------

export interface InspectorContext {
  model: OntologyModel
  prefixes: PrefixDecl[]
  diagnostics: Diagnostic[]
  readOnly: boolean
  onSelectResource: (key: string) => void
  /** The named graphs (empty for a single-graph ontology: no graph UI is shown). */
  graphs?: GraphTerm[]
  /** The graph new statements go into (null = the default graph). */
  targetGraph?: GraphTerm | null
  /** "Add annotation" on statement `index` of the resource `key` (UDR-0182 D8). */
  onAnnotate?: (key: string, index: number) => void
  /** IRIs used as classes but not declared in this ontology (not drawn; UDR-0185 D5). */
  undeclared?: Set<string>
}

function iriText(value: string, prefixes: PrefixDecl[]): string {
  return compactIri(value, prefixes)
}

/** How a graph is named in the UI: its rdfs:label in the default graph, else its prefixed name. */
export function graphLabel(g: GraphTerm | null | undefined, model: OntologyModel, prefixes: PrefixDecl[]): string {
  if (!g) return 'default graph'
  if (g.type === 'bnode') return `_:${g.value}`
  const resource = findResource(model, termKey(g))
  if (resource) {
    const defaultOnly = { ...resource, statements: resource.statements.filter((s) => !s.g) }
    const index = pickLiteralIndex(defaultOnly, RDFS_LABEL)
    if (index >= 0) return (defaultOnly.statements[index].o as { value: string }).value
  }
  return compactIri(g.value, prefixes)
}

/** A graph picker: the default graph plus every named graph. */
function GraphSelect(props: {
  value: GraphTerm | null
  ctx: InspectorContext
  onChange: (g: GraphTerm | null) => void
}) {
  const graphs = props.ctx.graphs ?? []
  return (
    <select
      className="rounded-md border bg-transparent px-1 py-1 text-xs"
      aria-label="Graph"
      value={graphKey(props.value)}
      onChange={(e) => props.onChange(graphs.find((g) => termKey(g) === e.target.value) ?? null)}>
      <option value="">default graph</option>
      {graphs.map((g) => (
        <option key={termKey(g)} value={termKey(g)}>
          {graphLabel(g, props.ctx.model, props.ctx.prefixes)}
        </option>
      ))}
    </select>
  )
}

// ---- Term view (read-only rendering) -----------------------------------------------------

export function TermView({ term, ctx, depth = 0 }: { term: Term; ctx: InspectorContext; depth?: number }) {
  const refs = useMemo(() => blankReferenceCounts(ctx.model), [ctx.model])
  if (term.type === 'iri') {
    const target = findResource(ctx.model, termKey(term))
    const text = iriText(term.value, ctx.prefixes)
    return target ? (
      <button
        type="button"
        className="break-all text-left text-blue-700 hover:underline"
        title={term.value}
        onClick={() => ctx.onSelectResource(termKey(term))}>
        {text}
      </button>
    ) : (
      <span className="break-all text-zinc-700" title={term.value}>
        {text}
      </span>
    )
  }
  if (term.type === 'bnode') {
    const items = listItems(ctx.model, term)
    if (items && depth < MAX_NEST) {
      return (
        <span className="text-zinc-700">
          ({' '}
          {items.map((item, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: list positions are the identity
            <span key={i}>
              {i > 0 ? ', ' : ''}
              <TermView term={item} ctx={ctx} depth={depth + 1} />
            </span>
          ))}{' '}
          )
        </span>
      )
    }
    const nested = findResource(ctx.model, termKey(term))
    if (nested && refs.get(termKey(term)) === 1 && depth < MAX_NEST) {
      return (
        <span className="inline-block w-full rounded border border-dashed px-1.5 py-1">
          <span className="text-[10px] text-zinc-400">[ _:{term.value} ]</span>
          {nested.statements.map((s, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: statement positions are the identity
            <span key={i} className="block pl-2">
              <span className="text-zinc-500">{iriText(s.p, ctx.prefixes)}</span>{' '}
              <TermView term={s.o} ctx={ctx} depth={depth + 1} />
            </span>
          ))}
        </span>
      )
    }
    return nested ? (
      <button
        type="button"
        className="text-blue-700 hover:underline"
        onClick={() => ctx.onSelectResource(termKey(term))}>
        _:{term.value}
      </button>
    ) : (
      <span className="text-zinc-700">_:{term.value}</span>
    )
  }
  if (term.type === 'triple') {
    return (
      <span className="text-zinc-700">
        {'<<( '}
        <TermView term={term.s} ctx={ctx} depth={depth + 1} /> <span>{iriText(term.p, ctx.prefixes)}</span>{' '}
        <TermView term={term.o} ctx={ctx} depth={depth + 1} />
        {' )>>'}
      </span>
    )
  }
  const plain = !term.language && term.datatype === XSD_STRING
  return (
    <span className="break-words">
      <span className="text-emerald-800" dir={term.direction ?? 'auto'}>
        &quot;{term.value}&quot;
      </span>
      {term.language && (
        <span className="ml-1 rounded bg-sky-50 px-1 text-[10px] text-sky-700">
          @{term.language}
          {term.direction ? `--${term.direction}` : ''}
        </span>
      )}
      {!plain && !term.language && (
        <span className="ml-1 rounded bg-zinc-100 px-1 text-[10px] text-zinc-600">
          {iriText(term.datatype, ctx.prefixes)}
        </span>
      )}
    </span>
  )
}

// ---- Term editor -----------------------------------------------------------------------

type TermKind = Term['type']

function emptyTerm(kind: TermKind): Term {
  switch (kind) {
    case 'iri':
      return { type: 'iri', value: '' }
    case 'bnode':
      return { type: 'bnode', value: '' }
    case 'literal':
      return literal('')
    case 'triple':
      return { type: 'triple', s: { type: 'iri', value: '' }, p: '', o: literal('') }
  }
}

function IriInput(props: {
  value: string
  prefixes: PrefixDecl[]
  onChange: (value: string) => void
  placeholder?: string
  ariaLabel: string
}) {
  const [text, setText] = useState(props.value ? compactIri(props.value, props.prefixes) : '')
  const resolved = expandIri(text, props.prefixes)
  return (
    <div className="min-w-0 flex-1">
      <input
        className={cn(fieldInput, text && !resolved && 'border-red-400')}
        value={text}
        aria-label={props.ariaLabel}
        placeholder={props.placeholder ?? 'prefix:name or full IRI'}
        onChange={(e) => {
          setText(e.target.value)
          props.onChange(expandIri(e.target.value, props.prefixes) ?? '')
        }}
      />
      {text && !resolved && <span className="text-[10px] text-red-600">Unknown prefix or not an absolute IRI</span>}
    </div>
  )
}

export function TermEditor(props: {
  value: Term
  onChange: (term: Term) => void
  model: OntologyModel
  prefixes: PrefixDecl[]
  subjectOnly?: boolean
  depth?: number
}) {
  const { value } = props
  const depth = props.depth ?? 0
  const kinds: TermKind[] = props.subjectOnly
    ? ['iri', 'bnode']
    : depth >= 2
      ? ['iri', 'bnode', 'literal']
      : ['iri', 'bnode', 'literal', 'triple']
  const labels = useMemo(() => [...blankLabels(props.model)].sort(), [props.model])
  return (
    <div className="space-y-1">
      <select
        className="rounded-md border bg-transparent px-1 py-1 text-xs"
        aria-label="Term kind"
        value={value.type}
        onChange={(e) => props.onChange(emptyTerm(e.target.value as TermKind))}>
        {kinds.map((kind) => (
          <option key={kind} value={kind}>
            {kind === 'iri' ? 'IRI' : kind === 'bnode' ? 'Blank node' : kind === 'literal' ? 'Literal' : 'Triple term'}
          </option>
        ))}
      </select>
      {value.type === 'iri' && (
        <IriInput
          value={value.value}
          prefixes={props.prefixes}
          ariaLabel="IRI"
          onChange={(v) => props.onChange({ type: 'iri', value: v })}
        />
      )}
      {value.type === 'bnode' && (
        <div className="flex items-center gap-1">
          <input
            className={fieldInput}
            list="ontology-bnode-labels"
            aria-label="Blank node label"
            placeholder="label (letters, digits, _ -)"
            value={value.value}
            onChange={(e) => props.onChange({ type: 'bnode', value: e.target.value.trim() })}
          />
          <datalist id="ontology-bnode-labels">
            {labels.map((label) => (
              <option key={label} value={label} />
            ))}
          </datalist>
          <Button
            variant="outline"
            size="sm"
            className="h-7 shrink-0 text-[10px]"
            onClick={() => {
              let label = ''
              do label = `n${Math.random().toString(36).slice(2, 10)}`
              while (labels.includes(label))
              props.onChange({ type: 'bnode', value: label })
            }}>
            New
          </Button>
        </div>
      )}
      {value.type === 'literal' && <LiteralEditor value={value} prefixes={props.prefixes} onChange={props.onChange} />}
      {value.type === 'triple' && (
        <div className="space-y-1 rounded border border-dashed p-1.5">
          <span className="text-[10px] text-zinc-500">{'<<( subject predicate object )>>'}</span>
          <TermEditor
            value={value.s}
            model={props.model}
            prefixes={props.prefixes}
            subjectOnly
            depth={depth + 1}
            onChange={(s) => props.onChange({ ...value, s })}
          />
          <IriInput
            value={value.p}
            prefixes={props.prefixes}
            ariaLabel="Triple term predicate"
            placeholder="predicate"
            onChange={(p) => props.onChange({ ...value, p })}
          />
          <TermEditor
            value={value.o}
            model={props.model}
            prefixes={props.prefixes}
            depth={depth + 1}
            onChange={(o) => props.onChange({ ...value, o })}
          />
        </div>
      )}
    </div>
  )
}

function LiteralEditor(props: {
  value: Extract<Term, { type: 'literal' }>
  prefixes: PrefixDecl[]
  onChange: (term: Term) => void
}) {
  const { value } = props
  const tagged = Boolean(value.language)
  const update = (patch: { value?: string; datatype?: string; language?: string; direction?: Direction | '' }) => {
    const language = patch.language !== undefined ? patch.language.trim() : (value.language ?? '')
    const direction = patch.direction !== undefined ? patch.direction : (value.direction ?? '')
    const lexical = patch.value ?? value.value
    if (language) {
      props.onChange(literal(lexical, { language, direction: direction || undefined }))
    } else {
      const datatype =
        patch.datatype ??
        (value.datatype === RDF_LANG_STRING || value.datatype === RDF_DIR_LANG_STRING ? XSD_STRING : value.datatype)
      props.onChange(literal(lexical, { datatype }))
    }
  }
  return (
    <div className="space-y-1">
      <textarea
        className={fieldInput}
        rows={2}
        aria-label="Literal value"
        dir={value.direction ?? 'auto'}
        value={value.value}
        onChange={(e) => update({ value: e.target.value })}
      />
      <div className="flex items-center gap-1">
        <input
          className={cn(fieldInput, 'w-20 flex-none')}
          aria-label="Language tag"
          placeholder="lang"
          value={value.language ?? ''}
          onChange={(e) => update({ language: e.target.value })}
        />
        <select
          className="rounded-md border bg-transparent px-1 py-1 text-xs disabled:opacity-50"
          aria-label="Base direction"
          disabled={!tagged}
          value={value.direction ?? ''}
          onChange={(e) => update({ direction: e.target.value as Direction | '' })}>
          <option value="">no direction</option>
          <option value="ltr">ltr</option>
          <option value="rtl">rtl</option>
        </select>
      </div>
      {!tagged && (
        <div>
          <input
            className={fieldInput}
            list="ontology-datatypes"
            aria-label="Datatype"
            placeholder="datatype IRI"
            value={value.datatype}
            onChange={(e) => update({ datatype: expandIri(e.target.value, props.prefixes) ?? e.target.value })}
          />
          <datalist id="ontology-datatypes">
            {COMMON_DATATYPES.map((dt) => (
              <option key={dt} value={dt}>
                {compactIri(dt, props.prefixes)}
              </option>
            ))}
          </datalist>
        </div>
      )}
    </div>
  )
}

// ---- Statement editor (predicate + object) ------------------------------------------------

function StatementEditor(props: {
  initial: Statement | null
  ctx: InspectorContext
  onSave: (statement: StatementInput) => void
  onCancel: () => void
}) {
  const { ctx } = props
  const [predicate, setPredicate] = useState(props.initial?.p ?? '')
  const [object, setObject] = useState<Term>(props.initial?.o ?? literal(''))
  // An existing statement starts in its own graph; a new one in the target graph.
  const [graph, setGraph] = useState<GraphTerm | null>(
    props.initial ? (props.initial.g ?? null) : (ctx.targetGraph ?? null),
  )
  const valid = Boolean(predicate) && termComplete(object)
  const showGraph = (ctx.graphs ?? []).length > 0
  return (
    <div className="space-y-1.5 rounded border bg-zinc-50 p-2">
      <IriInput
        value={predicate}
        prefixes={ctx.prefixes}
        ariaLabel="Predicate"
        placeholder="predicate (prefix:name or IRI)"
        onChange={setPredicate}
      />
      <TermEditor value={object} model={ctx.model} prefixes={ctx.prefixes} onChange={setObject} />
      {showGraph && (
        <div className="flex items-center gap-1 text-[10px] text-zinc-500">
          <span>Graph</span>
          <GraphSelect value={graph} ctx={ctx} onChange={setGraph} />
        </div>
      )}
      <div className="flex justify-end gap-1">
        <Button variant="outline" size="sm" className="h-6 text-xs" onClick={props.onCancel}>
          Cancel
        </Button>
        <Button
          size="sm"
          className="h-6 text-xs"
          disabled={!valid}
          onClick={() => props.onSave({ p: predicate, o: object, g: showGraph ? graph : undefined })}>
          Apply
        </Button>
      </div>
    </div>
  )
}

function termComplete(term: Term): boolean {
  if (term.type === 'iri' || term.type === 'bnode') return Boolean(term.value)
  if (term.type === 'literal') return true
  return termComplete(term.s) && Boolean(term.p) && termComplete(term.o)
}

// ---- Statement list -----------------------------------------------------------------

export function StatementList(props: {
  resourceKey: string
  ctx: InspectorContext
  /** Predicates the Detail form already shows (marked, not hidden). */
  formPredicates?: Set<string>
  onEdit: (index: number, statement: StatementInput | null) => void
  onAdd: (statement: StatementInput) => void
}) {
  const { ctx } = props
  const resource = findResource(ctx.model, props.resourceKey)
  const showGraph = (ctx.graphs ?? []).length > 0
  const [editing, setEditing] = useState<number | 'new' | null>(null)
  const [limit, setLimit] = useState(PAGE)
  const reifiedBy = useMemo(() => {
    const map = new Map<string, string[]>()
    for (const r of ctx.model.resources) {
      for (const s of r.statements) {
        if (s.p === RDF_REIFIES && s.o.type === 'triple') {
          const key = termKey(s.o)
          map.set(key, [...(map.get(key) ?? []), resourceKey(r)])
        }
      }
    }
    return map
  }, [ctx.model])
  const diagnosed = useMemo(
    () => new Set(ctx.diagnostics.map((d) => `${termKey(d.s)} ${d.p} ${termKey(d.o)}`)),
    [ctx.diagnostics],
  )
  if (!resource) return <p className="text-[11px] text-zinc-400">No statements.</p>
  const statements = resource.statements
  return (
    <div className="space-y-1">
      {statements.slice(0, limit).map((statement, index) => {
        const reifiers = reifiedBy.get(termKey(asTriple(resource.term, statement))) ?? []
        const warn = diagnosed.has(`${termKey(resource.term)} ${statement.p} ${termKey(statement.o)}`)
        const undeclared = ctx.undeclared ? usesUndeclaredClass(ctx.undeclared, resource.term, statement) : false
        return editing === index ? (
          <StatementEditor
            // biome-ignore lint/suspicious/noArrayIndexKey: the statement index is the edit target
            key={index}
            initial={statement}
            ctx={ctx}
            onCancel={() => setEditing(null)}
            onSave={(next) => {
              setEditing(null)
              props.onEdit(index, next)
            }}
          />
        ) : (
          <div
            // biome-ignore lint/suspicious/noArrayIndexKey: statements are positional
            key={index}
            className="group flex items-start gap-1 rounded border px-1.5 py-1 text-[11px] leading-snug">
            <div className="min-w-0 flex-1">
              <span className="font-medium text-zinc-600" title={statement.p}>
                {statement.p === RDF_TYPE ? 'a' : iriText(statement.p, ctx.prefixes)}
              </span>
              {props.formPredicates?.has(statement.p) && (
                <span className="ml-1 rounded bg-zinc-100 px-1 text-[9px] text-zinc-500">in form</span>
              )}{' '}
              <TermView term={statement.o} ctx={ctx} />
              {showGraph && (
                <span
                  className={cn(
                    'ml-1 rounded px-1 text-[9px]',
                    statement.g ? 'bg-teal-50 text-teal-700' : 'bg-zinc-100 text-zinc-500',
                  )}
                  title={statement.g ? `In the named graph ${termKey(statement.g)}` : 'In the default graph'}>
                  {graphLabel(statement.g, ctx.model, ctx.prefixes)}
                </span>
              )}
              {warn && (
                <span
                  className="ml-1 inline-flex items-center text-[10px] text-amber-600"
                  title="The value is not a valid lexical form of its datatype (kept as written)">
                  <TriangleAlert className="mr-0.5 h-3 w-3" /> ill-typed
                </span>
              )}
              {undeclared && (
                <span
                  className="ml-1 inline-flex items-center rounded bg-sky-50 px-1 text-[10px] text-sky-700"
                  title="Not declared as a class in this ontology (no rdf:type rdfs:Class or owl:Class), so the canvas does not draw it">
                  <Info className="mr-0.5 h-3 w-3" /> not declared
                </span>
              )}
              {reifiers.length > 0 && (
                <button
                  type="button"
                  className="ml-1 inline-flex items-center rounded bg-violet-50 px-1 text-[10px] text-violet-700 hover:underline"
                  title="This statement is annotated (reified); open the reifier"
                  onClick={() => ctx.onSelectResource(reifiers[0])}>
                  <Link2 className="mr-0.5 h-3 w-3" /> annotated{reifiers.length > 1 ? ` x${reifiers.length}` : ''}
                </button>
              )}
            </div>
            {!ctx.readOnly && (
              <div className="flex shrink-0 items-center opacity-0 transition-opacity group-hover:opacity-100">
                {ctx.onAnnotate && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-5 w-5 text-zinc-500"
                    aria-label="Add annotation"
                    title="Add annotation (a reifier: rdf:reifies this statement)"
                    onClick={() => ctx.onAnnotate?.(props.resourceKey, index)}>
                    <MessageSquarePlus className="h-3 w-3" />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-5 w-5 text-zinc-500"
                  aria-label="Edit statement"
                  onClick={() => setEditing(index)}>
                  <Pencil className="h-3 w-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-5 w-5 text-zinc-500 hover:text-red-600"
                  aria-label="Delete statement"
                  onClick={() => props.onEdit(index, null)}>
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            )}
          </div>
        )
      })}
      {statements.length > limit && (
        <Button variant="ghost" size="sm" className="h-6 w-full text-xs" onClick={() => setLimit(limit + PAGE)}>
          Show more ({statements.length - limit} more)
        </Button>
      )}
      {!ctx.readOnly &&
        (editing === 'new' ? (
          <StatementEditor
            initial={null}
            ctx={ctx}
            onCancel={() => setEditing(null)}
            onSave={(statement) => {
              setEditing(null)
              props.onAdd(statement)
            }}
          />
        ) : (
          <Button variant="outline" size="sm" className="h-6 w-full text-xs" onClick={() => setEditing('new')}>
            <Plus className="mr-1 h-3 w-3" /> Add statement
          </Button>
        ))}
    </div>
  )
}

/** A collapsible "All statements" block for the Detail forms. */
export function AllStatements(props: {
  resourceKey: string
  ctx: InspectorContext
  formPredicates: Set<string>
  onEdit: (index: number, statement: StatementInput | null) => void
  onAdd: (statement: StatementInput) => void
}) {
  const [open, setOpen] = useState(false)
  const count = findResource(props.ctx.model, props.resourceKey)?.statements.length ?? 0
  return (
    <div>
      <button
        type="button"
        className={cn(fieldLabel, 'flex items-center gap-1 hover:text-zinc-700')}
        aria-expanded={open}
        onClick={() => setOpen(!open)}>
        <ChevronRight className={cn('h-3 w-3 transition-transform', open && 'rotate-90')} />
        All statements ({count})
      </button>
      {open && (
        <StatementList
          resourceKey={props.resourceKey}
          ctx={props.ctx}
          formPredicates={props.formPredicates}
          onEdit={props.onEdit}
          onAdd={props.onAdd}
        />
      )}
    </div>
  )
}

// ---- Generic resource detail -----------------------------------------------------------

export function ResourceDetail(props: {
  resourceKey: string
  ctx: InspectorContext
  onEdit: (index: number, statement: StatementInput | null) => void
  onAdd: (statement: StatementInput) => void
  onDelete: () => void
}) {
  const resource = findResource(props.ctx.model, props.resourceKey)
  if (!resource) {
    return <p className="p-4 text-xs text-zinc-500">This resource no longer has any statements.</p>
  }
  const types = resource.statements.filter((s) => s.p === RDF_TYPE).map((s) => s.o)
  // Super-properties (rdfs:subPropertyOf; shown, edited as statements -- UDR-0185 D6).
  const supers = [
    ...new Set(
      resource.statements
        .filter((s) => s.p === RDFS_SUB_PROPERTY_OF && s.o.type === 'iri')
        .map((s) => (s.o as { value: string }).value),
    ),
  ].sort()
  return (
    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
      <div>
        <span className={fieldLabel}>{ROLE_LABEL[resource.role]}</span>
        <div className="break-all text-[11px] text-zinc-700">
          {resource.term.type === 'iri' ? (
            <span title={resource.term.value}>{iriText(resource.term.value, props.ctx.prefixes)}</span>
          ) : (
            `_:${(resource.term as { value: string }).value}`
          )}
        </div>
        {types.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1 text-[10px]">
            {types.map((t) => (
              <span key={termKey(t)} className="rounded bg-zinc-100 px-1">
                <TermView term={t} ctx={props.ctx} />
              </span>
            ))}
          </div>
        )}
      </div>
      {supers.length > 0 && (
        <div>
          <span className={fieldLabel}>Super-properties</span>
          <div className="flex flex-wrap gap-1">
            {supers.map((value) => (
              <button
                key={value}
                type="button"
                className="max-w-full truncate rounded border px-1.5 py-0.5 text-[11px] text-zinc-600 hover:bg-zinc-50"
                title={value}
                onClick={() => props.ctx.onSelectResource(termKey({ type: 'iri', value }))}>
                {iriText(value, props.ctx.prefixes)}
              </button>
            ))}
          </div>
        </div>
      )}
      <div>
        <span className={fieldLabel}>Statements</span>
        <StatementList resourceKey={props.resourceKey} ctx={props.ctx} onEdit={props.onEdit} onAdd={props.onAdd} />
      </div>
      {!props.ctx.readOnly && (
        <Button variant="destructive" size="sm" className="h-7 w-full text-xs" onClick={props.onDelete}>
          <Trash2 className="mr-1 h-3 w-3" /> Delete all statements of this resource
        </Button>
      )}
    </div>
  )
}

// ---- Resources tab ------------------------------------------------------------------

export function ResourcesPane(props: {
  ctx: InspectorContext
  selectedKey: string | null
  onCreate: () => void
  /** Open the Data view, where individuals are listed (UDR-0187 D8). */
  onOpenData?: () => void
}) {
  const { ctx } = props
  const [query, setQuery] = useState('')
  // 'individual' lists the individuals here too; every other choice leaves them to the Data view.
  const [role, setRole] = useState<Role | 'all' | 'individual'>('all')
  // '*' = every graph, '' = the default graph, else a graph's term key (UDR-0182 D1).
  const [graph, setGraph] = useState('*')
  const [limit, setLimit] = useState(PAGE)
  const graphs = ctx.graphs ?? []
  const everything = useMemo(() => inspectorResources(ctx.model), [ctx.model])
  const individuals = useMemo(() => everything.filter(isIndividual), [everything])
  const listed = useMemo(
    () => (role === 'individual' ? individuals : everything.filter((r) => !isIndividual(r))),
    [role, everything, individuals],
  )
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return listed.filter((r) => {
      if (role !== 'all' && role !== 'individual' && r.role !== role) return false
      if (graph !== '*' && !r.statements.some((s) => graphKey(s.g) === graph)) return false
      if (!needle) return true
      return resourceSearchText(r, ctx.prefixes).includes(needle)
    })
  }, [listed, query, role, graph, ctx.prefixes])
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 space-y-1.5 border-b p-2">
        <div className="flex items-center gap-1">
          <input
            className={fieldInput}
            placeholder="Filter by name, type or value"
            aria-label="Filter resources"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              aria-label="Clear filter"
              onClick={() => setQuery('')}>
              <X className="h-3 w-3" />
            </Button>
          )}
        </div>
        <div className="flex items-center justify-between gap-1">
          <select
            className="rounded-md border bg-transparent px-1 py-1 text-xs"
            aria-label="Filter by role"
            value={role}
            onChange={(e) => setRole(e.target.value as Role | 'all' | 'individual')}>
            <option value="all">All roles</option>
            <option value="other">Resources</option>
            <option value="individual">Individuals</option>
            <option value="object_property">Object properties</option>
            <option value="datatype_property">Datatype properties</option>
            <option value="entity">Entities</option>
          </select>
          {graphs.length > 0 && (
            <select
              className="min-w-0 rounded-md border bg-transparent px-1 py-1 text-xs"
              aria-label="Filter by graph"
              value={graph}
              onChange={(e) => setGraph(e.target.value)}>
              <option value="*">All graphs</option>
              <option value="">default graph</option>
              {graphs.map((g) => (
                <option key={termKey(g)} value={termKey(g)}>
                  {graphLabel(g, ctx.model, ctx.prefixes)}
                </option>
              ))}
            </select>
          )}
          {!ctx.readOnly && (
            <Button variant="outline" size="sm" className="h-6 text-xs" onClick={props.onCreate}>
              <Plus className="mr-1 h-3 w-3" /> Resource
            </Button>
          )}
        </div>
        <p className="text-[10px] text-zinc-500">
          {role === 'individual'
            ? `${filtered.length} of ${listed.length} individuals (also listed per class in the Data view).`
            : `${filtered.length} of ${listed.length} resources not drawn on the canvas and not individuals (untyped resources, other vocabularies, reifiers, shared blank nodes).`}
        </p>
        {role !== 'individual' && individuals.length > 0 && (
          <p className="flex items-center gap-1 text-[10px] text-emerald-700">
            <Info className="h-3 w-3 shrink-0" />
            {individuals.length} individual{individuals.length === 1 ? ' is' : 's are'} in the Data view.
            {props.onOpenData && (
              <button type="button" className="underline hover:text-emerald-900" onClick={props.onOpenData}>
                Open
              </button>
            )}
          </p>
        )}
        {(ctx.undeclared?.size ?? 0) > 0 && (
          <p
            className="flex items-start gap-1 text-[10px] text-sky-700"
            title={[...(ctx.undeclared ?? [])].slice(0, 20).join(', ')}>
            <Info className="mt-px h-3 w-3 shrink-0" />
            {ctx.undeclared?.size} class{ctx.undeclared?.size === 1 ? ' is' : 'es are'} used (subClassOf, domain or
            range) but not declared in this ontology, so the canvas does not draw{' '}
            {ctx.undeclared?.size === 1 ? 'it' : 'them'}. Their statements are marked "not declared".
          </p>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {filtered.slice(0, limit).map((r) => {
          const key = resourceKey(r)
          return (
            <button
              key={key}
              type="button"
              className={cn(
                'block w-full border-b px-2 py-1.5 text-left text-[11px]',
                props.selectedKey === key ? 'bg-blue-50' : 'hover:bg-zinc-50',
              )}
              onClick={() => ctx.onSelectResource(key)}>
              <div className="truncate font-medium text-zinc-800">
                {r.term.type === 'iri'
                  ? iriText(r.term.value, ctx.prefixes)
                  : `_:${(r.term as { value: string }).value}`}
              </div>
              <div className="truncate text-[10px] text-zinc-500">
                {typeSummary(r, ctx.prefixes)} - {r.statements.length} statement{r.statements.length === 1 ? '' : 's'}
              </div>
            </button>
          )
        })}
        {filtered.length > limit && (
          <Button variant="ghost" size="sm" className="h-7 w-full text-xs" onClick={() => setLimit(limit + PAGE)}>
            Show more ({filtered.length - limit} more)
          </Button>
        )}
        {filtered.length === 0 && <p className="p-3 text-xs text-zinc-400">Nothing to list.</p>}
      </div>
    </div>
  )
}

function typeSummary(resource: Resource, prefixes: PrefixDecl[]): string {
  const types = resource.statements
    .filter((s) => s.p === RDF_TYPE && s.o.type === 'iri')
    .map((s) => compactIri((s.o as { value: string }).value, prefixes))
  if (resource.statements.some((s) => s.p === RDF_REIFIES)) types.push('reifier')
  return types.length > 0 ? types.join(', ') : ROLE_LABEL[resource.role]
}

function resourceSearchText(resource: Resource, prefixes: PrefixDecl[]): string {
  const parts = [termKey(resource.term)]
  if (resource.term.type === 'iri') parts.push(compactIri(resource.term.value, prefixes))
  for (const s of resource.statements) {
    parts.push(compactIri(s.p, prefixes))
    if (s.o.type === 'literal' || s.o.type === 'iri') parts.push(s.o.value)
  }
  return parts.join(' ').toLowerCase()
}

// ---- New resource dialog ---------------------------------------------------------------

export function NewResourceDialog(props: {
  open: boolean
  model: OntologyModel
  prefixes: PrefixDecl[]
  onCancel: () => void
  onCreate: (term: Term, statement: Statement) => void
}) {
  const [subject, setSubject] = useState<Term>({ type: 'iri', value: '' })
  const [predicate, setPredicate] = useState(RDF_TYPE)
  const [object, setObject] = useState<Term>({ type: 'iri', value: '' })
  const valid = termComplete(subject) && Boolean(predicate) && termComplete(object)
  return (
    <Dialog open={props.open} onOpenChange={(o) => !o && props.onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New resource</DialogTitle>
          <DialogDescription>A resource exists through its statements: give it a first one.</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <div>
            <span className={fieldLabel}>Subject</span>
            <TermEditor
              value={subject}
              model={props.model}
              prefixes={props.prefixes}
              subjectOnly
              onChange={setSubject}
            />
          </div>
          <div>
            <span className={fieldLabel}>Predicate</span>
            <IriInput value={predicate} prefixes={props.prefixes} ariaLabel="Predicate" onChange={setPredicate} />
          </div>
          <div>
            <span className={fieldLabel}>Object</span>
            <TermEditor value={object} model={props.model} prefixes={props.prefixes} onChange={setObject} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={props.onCancel}>
            Cancel
          </Button>
          <Button disabled={!valid} onClick={() => props.onCreate(subject, { p: predicate, o: object })}>
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---- Add annotation dialog (UDR-0182 D8) ------------------------------------------------

/**
 * Create a reifier for one statement: an IRI (minted as `<base>r-<8 hex>`, editable),
 * `rdf:reifies <<( s p o )>>` in the statement's graph, and an optional first annotation.
 */
export function AnnotateDialog(props: {
  open: boolean
  ctx: InspectorContext
  subject: Term | null
  statement: Statement | null
  suggestedIri: string
  onCancel: () => void
  onCreate: (reifierIri: string, annotation: { p: string; o: Term } | null) => void
}) {
  const { ctx } = props
  const [reifier, setReifier] = useState(props.suggestedIri)
  const [predicate, setPredicate] = useState('')
  const [object, setObject] = useState<Term>(literal(''))
  const [lastOpen, setLastOpen] = useState(false)
  if (props.open !== lastOpen) {
    // Re-seed each time the dialog opens (derived state during render).
    setLastOpen(props.open)
    if (props.open) {
      setReifier(props.suggestedIri)
      setPredicate('')
      setObject(literal(''))
    }
  }
  const taken = Boolean(reifier) && Boolean(findResource(ctx.model, termKey({ type: 'iri', value: reifier })))
  const withAnnotation = Boolean(predicate)
  const valid = Boolean(reifier) && !taken && (!withAnnotation || termComplete(object))
  return (
    <Dialog open={props.open} onOpenChange={(o) => !o && props.onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add annotation</DialogTitle>
          <DialogDescription>
            A reifier names this statement (rdf:reifies) so you can say things about it, such as its source or validity.
            It is added to the statement&apos;s graph.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {props.subject && props.statement && (
            <div className="rounded border bg-zinc-50 px-2 py-1 text-[11px]">
              <TermView
                term={{ type: 'triple', s: props.subject, p: props.statement.p, o: props.statement.o }}
                ctx={ctx}
              />
              {(ctx.graphs ?? []).length > 0 && (
                <span className="ml-1 rounded bg-teal-50 px-1 text-[9px] text-teal-700">
                  {graphLabel(props.statement.g, ctx.model, ctx.prefixes)}
                </span>
              )}
            </div>
          )}
          <div>
            <label htmlFor="annotation-reifier" className={fieldLabel}>
              Reifier IRI
            </label>
            <input
              id="annotation-reifier"
              className={cn(fieldInput, taken && 'border-red-400')}
              value={reifier}
              onChange={(e) => setReifier(expandIri(e.target.value, ctx.prefixes) ?? e.target.value.trim())}
            />
            {taken && <span className="text-[10px] text-red-600">This IRI is already used by a resource.</span>}
          </div>
          <div>
            <span className={fieldLabel}>First annotation (optional)</span>
            <IriInput
              value={predicate}
              prefixes={ctx.prefixes}
              ariaLabel="Annotation predicate"
              placeholder="predicate, e.g. dct:source"
              onChange={setPredicate}
            />
            {withAnnotation && (
              <div className="mt-1">
                <TermEditor value={object} model={ctx.model} prefixes={ctx.prefixes} onChange={setObject} />
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={props.onCancel}>
            Cancel
          </Button>
          <Button
            disabled={!valid}
            onClick={() => props.onCreate(reifier, withAnnotation ? { p: predicate, o: object } : null)}>
            Add annotation
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---- Document dialog (prefixes / base / VERSION; UDR-0180 D6) -----------------------------

export function DocumentDialog(props: {
  open: boolean
  document: OntologyDocument
  readOnly: boolean
  usesRdf12: boolean
  onCancel: () => void
  onApply: (document: OntologyDocument) => void
}) {
  const [draft, setDraft] = useState<OntologyDocument>(props.document)
  const [lastOpen, setLastOpen] = useState(false)
  if (props.open !== lastOpen) {
    // Re-seed the draft each time the dialog opens (derived state during render).
    setLastOpen(props.open)
    if (props.open) setDraft(props.document)
  }
  const names = draft.prefixes.map((p) => p.prefix)
  const duplicate = names.find((n, i) => names.indexOf(n) !== i)
  const badIri = draft.prefixes.some((p) => !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(p.iri))
  const badBase = Boolean(draft.base) && !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(draft.base ?? '')
  const invalid = duplicate !== undefined || badIri || badBase
  return (
    <Dialog open={props.open} onOpenChange={(o) => !o && props.onCancel()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Document declarations</DialogTitle>
          <DialogDescription>
            Prefixes, base and VERSION are written at the top of the Turtle (or TriG) file. Comments and the original
            layout are not kept.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] space-y-3 overflow-y-auto">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label htmlFor="ontology-doc-version" className={fieldLabel}>
                VERSION
              </label>
              <input
                id="ontology-doc-version"
                className={fieldInput}
                placeholder="none (e.g. 1.2)"
                disabled={props.readOnly}
                value={draft.version ?? ''}
                onChange={(e) => setDraft({ ...draft, version: e.target.value.trim() || null })}
              />
            </div>
            <div>
              <label htmlFor="ontology-doc-base" className={fieldLabel}>
                Base
              </label>
              <input
                id="ontology-doc-base"
                className={cn(fieldInput, badBase && 'border-red-400')}
                placeholder="none"
                disabled={props.readOnly}
                value={draft.base ?? ''}
                onChange={(e) => setDraft({ ...draft, base: e.target.value.trim() || null })}
              />
            </div>
          </div>
          {props.usesRdf12 && draft.version !== '1.2' && (
            <p className="flex items-start gap-1 text-[11px] text-amber-700">
              <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
              This ontology uses RDF 1.2 features (triple terms or directional strings) but does not declare VERSION
              &quot;1.2&quot;. Saving still works.
            </p>
          )}
          <div>
            <span className={fieldLabel}>Prefixes</span>
            <div className="space-y-1">
              {draft.prefixes.map((decl, index) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional while editing
                <div key={index} className="flex items-center gap-1">
                  <input
                    className={cn(fieldInput, 'w-24 flex-none', duplicate === decl.prefix && 'border-red-400')}
                    aria-label="Prefix name"
                    disabled={props.readOnly}
                    value={decl.prefix}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        prefixes: draft.prefixes.map((p, i) =>
                          i === index ? { ...p, prefix: e.target.value.trim() } : p,
                        ),
                      })
                    }
                  />
                  <span className="text-xs text-zinc-400">:</span>
                  <input
                    className={cn(fieldInput, !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(decl.iri) && 'border-red-400')}
                    aria-label="Namespace IRI"
                    disabled={props.readOnly}
                    value={decl.iri}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        prefixes: draft.prefixes.map((p, i) =>
                          i === index ? { ...p, iri: e.target.value.trim() } : p,
                        ),
                      })
                    }
                  />
                  {!props.readOnly && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-6 w-6 shrink-0 text-zinc-500 hover:text-red-600"
                      aria-label={`Remove prefix ${decl.prefix}`}
                      onClick={() => setDraft({ ...draft, prefixes: draft.prefixes.filter((_, i) => i !== index) })}>
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  )}
                </div>
              ))}
              {!props.readOnly && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 w-full text-xs"
                  onClick={() => setDraft({ ...draft, prefixes: [...draft.prefixes, { prefix: '', iri: '' }] })}>
                  <Plus className="mr-1 h-3 w-3" /> Add prefix
                </Button>
              )}
            </div>
            {duplicate !== undefined && (
              <p className="mt-1 text-[10px] text-red-600">The prefix &quot;{duplicate}&quot; is declared twice.</p>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={props.onCancel}>
            Cancel
          </Button>
          {!props.readOnly && (
            <Button disabled={invalid} onClick={() => props.onApply(draft)}>
              Apply
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
