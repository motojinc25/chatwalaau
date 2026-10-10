/**
 * The Data view of the Ontology Manager (CTR-0173 v10, PRP-0205 / UDR-0187).
 *
 * Individuals (the ABox) shown per class: a class tree on the left (asserted
 * rdfs:subClassOf, plus "Not declared" and "No class"), a table on the right with
 * one row per individual and one column per property (schema columns first, then
 * the predicates the rows use). A cell is a LIST of statements: adding, removing or
 * changing a value is one statement edit through the same model diff and
 * `POST /statements` as every other edit (UDR-0187 D4 / D9). Which resources are
 * individuals is decided here, in the view, never in the stored roles (D1).
 */

import { ChevronRight, Columns3, Plus, Trash2, X } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import {
  AllStatements,
  fieldInput,
  fieldLabel,
  graphLabel,
  type InspectorContext,
} from '@/components/OntologyInspector'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  type CellValue,
  type ClassTree,
  cellValues,
  classMembers,
  compactIri,
  type DataColumn,
  dataColumns,
  displayLiteral,
  expandIri,
  functionalNotice,
  type GraphScope,
  graphKey,
  type IndividualIndex,
  individualClasses,
  iri,
  lexicalNotice,
  literal,
  type OntologyModel,
  type PrefixDecl,
  RDF,
  RDF_TYPE,
  RDFS_LABEL,
  type Resource,
  relexical,
  resourceKey,
  type StatementEdit,
  type StatementInput,
  type Term,
  termKey,
  XSD,
  XSD_BOOLEAN,
} from '@/lib/ontologyModel'
import { cn } from '@/lib/utils'

const PAGE = 100
const SHOWN_VALUES = 2
const XSD_DATE = `${XSD}date`
const RDF_LANG_STRING = `${RDF}langString`

// Per-browser view preference, never saved in the ontology (UDR-0187 D3).
const HIDDEN_COLUMNS_STORAGE_KEY = 'chatwalaau.ontology.dataHiddenColumns'

function readHiddenColumns(): Set<string> {
  try {
    const raw = localStorage.getItem(HIDDEN_COLUMNS_STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [])
  } catch {
    return new Set()
  }
}

function writeHiddenColumns(hidden: Set<string>): void {
  try {
    localStorage.setItem(HIDDEN_COLUMNS_STORAGE_KEY, JSON.stringify([...hidden]))
  } catch {
    // storage blocked: the choice lasts for this session only
  }
}

/** What the Data view lists: one class (declared or not), or the individuals with no class. */
export type DataGroup = { kind: 'class'; iri: string } | { kind: 'noClass' }

/** How an individual is named in the table and the Detail. */
export function individualLabel(resource: Resource, prefixes: PrefixDecl[]): string {
  const label = displayLiteral(resource, RDFS_LABEL)
  if (label) return label
  return resource.term.type === 'iri'
    ? compactIri(resource.term.value, prefixes)
    : `_:${(resource.term as { value: string }).value}`
}

function termText(term: Term, prefixes: PrefixDecl[]): string {
  switch (term.type) {
    case 'iri':
      return compactIri(term.value, prefixes)
    case 'bnode':
      return `_:${term.value}`
    case 'literal':
      return term.language ? `${term.value} @${term.language}` : term.value
    case 'triple':
      return '<<( ... )>>'
  }
}

function valueInScope(value: CellValue, scope: GraphScope): boolean {
  if (scope === 'all') return true
  if (scope === 'default') return !value.g
  return graphKey(value.g) === termKey(scope)
}

// ---- Class tree --------------------------------------------------------------------

function TreeNode(props: {
  cls: string
  depth: number
  path: Set<string>
  tree: ClassTree
  index: IndividualIndex
  classLabel: (cls: string) => string
  selected: DataGroup | null
  onSelect: (group: DataGroup) => void
}) {
  const [open, setOpen] = useState(props.depth === 0)
  // Cycles are cut: a class already on this path is not expanded again (UDR-0187 D2).
  const children = (props.tree.children.get(props.cls) ?? []).filter((c) => !props.path.has(c))
  const count = props.index.members.get(props.cls)?.length ?? 0
  const active = props.selected?.kind === 'class' && props.selected.iri === props.cls
  const path = useMemo(() => new Set([...props.path, props.cls]), [props.path, props.cls])
  return (
    <div>
      <div
        className={cn('flex items-center gap-0.5 pr-1 text-[11px]', active ? 'bg-blue-50' : 'hover:bg-zinc-50')}
        style={{ paddingLeft: 4 + props.depth * 12 }}>
        {children.length > 0 ? (
          <button
            type="button"
            className="shrink-0 text-zinc-400"
            aria-label={open ? 'Collapse' : 'Expand'}
            aria-expanded={open}
            onClick={() => setOpen(!open)}>
            <ChevronRight className={cn('h-3 w-3 transition-transform', open && 'rotate-90')} />
          </button>
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <button
          type="button"
          className="min-w-0 flex-1 truncate py-1 text-left text-zinc-800"
          title={props.cls}
          onClick={() => props.onSelect({ kind: 'class', iri: props.cls })}>
          {props.classLabel(props.cls)}
        </button>
        <span className="shrink-0 text-[10px] text-zinc-500">{count}</span>
      </div>
      {open &&
        children.map((child) => <TreeNode key={child} {...props} cls={child} depth={props.depth + 1} path={path} />)}
    </div>
  )
}

function ClassTreePane(props: {
  tree: ClassTree
  index: IndividualIndex
  classLabel: (cls: string) => string
  prefixes: PrefixDecl[]
  selected: DataGroup | null
  onSelect: (group: DataGroup) => void
}) {
  const [query, setQuery] = useState('')
  const empty = useMemo(() => new Set<string>(), [])
  const needle = query.trim().toLowerCase()
  const allClasses = useMemo(() => {
    const out = new Set<string>(props.tree.roots)
    for (const [sup, subs] of props.tree.children) {
      out.add(sup)
      for (const sub of subs) out.add(sub)
    }
    return [...out]
  }, [props.tree])
  const matches = needle
    ? allClasses
        .filter((c) => props.classLabel(c).toLowerCase().includes(needle) || c.toLowerCase().includes(needle))
        .sort((a, b) => props.classLabel(a).localeCompare(props.classLabel(b)))
    : []
  const flatRow = (cls: string, label: string) => {
    const active = props.selected?.kind === 'class' && props.selected.iri === cls
    return (
      <button
        key={cls}
        type="button"
        className={cn(
          'flex w-full items-center gap-1 px-2 py-1 text-left text-[11px]',
          active ? 'bg-blue-50' : 'hover:bg-zinc-50',
        )}
        title={cls}
        onClick={() => props.onSelect({ kind: 'class', iri: cls })}>
        <span className="min-w-0 flex-1 truncate text-zinc-800">{label}</span>
        <span className="shrink-0 text-[10px] text-zinc-500">{props.index.members.get(cls)?.length ?? 0}</span>
      </button>
    )
  }
  return (
    <div className="flex min-h-0 flex-col border-r" style={{ width: 220 }}>
      <div className="shrink-0 border-b p-1.5">
        <input
          className={fieldInput}
          placeholder="Filter classes"
          aria-label="Filter classes"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto py-1">
        {needle ? (
          matches.map((cls) => flatRow(cls, props.classLabel(cls)))
        ) : (
          <>
            {props.tree.roots.map((cls) => (
              <TreeNode
                key={cls}
                cls={cls}
                depth={0}
                path={empty}
                tree={props.tree}
                index={props.index}
                classLabel={props.classLabel}
                selected={props.selected}
                onSelect={props.onSelect}
              />
            ))}
            {props.index.undeclared.length > 0 && (
              <div className="mt-1 border-t pt-1">
                <span className={cn(fieldLabel, 'px-2')} title="Types used by individuals but not declared as classes">
                  Not declared ({props.index.undeclared.length})
                </span>
                {props.index.undeclared.map((cls) => flatRow(cls, compactIri(cls, props.prefixes)))}
              </div>
            )}
            {props.index.noClass.length > 0 && (
              <div className="mt-1 border-t pt-1">
                <button
                  type="button"
                  className={cn(
                    'flex w-full items-center gap-1 px-2 py-1 text-left text-[11px]',
                    props.selected?.kind === 'noClass' ? 'bg-blue-50' : 'hover:bg-zinc-50',
                  )}
                  title="Individuals typed only owl:NamedIndividual"
                  onClick={() => props.onSelect({ kind: 'noClass' })}>
                  <span className="min-w-0 flex-1 truncate text-zinc-800">No class</span>
                  <span className="shrink-0 text-[10px] text-zinc-500">{props.index.noClass.length}</span>
                </button>
              </div>
            )}
            {props.tree.roots.length === 0 && props.index.keys.size === 0 && (
              <p className="p-3 text-xs text-zinc-400">No classes or individuals yet.</p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// ---- Value editing (one statement per value; UDR-0187 D4) --------------------------

/** Inputs for a new value, chosen by the column's range. */
function NewValue(props: {
  column: DataColumn
  candidates: string[]
  prefixes: PrefixDecl[]
  onAdd: (term: Term) => void
}) {
  const { column } = props
  const listId = useId()
  const [text, setText] = useState('')
  const [language, setLanguage] = useState('')
  const range = column.range
  const make = (): Term | null => {
    if (column.objectValued) {
      const value = expandIri(text.trim(), props.prefixes)
      return value ? iri(value) : null
    }
    if (range === XSD_BOOLEAN) return literal(text === 'true' ? 'true' : 'false', { datatype: XSD_BOOLEAN })
    if (!text) return null
    if (range === RDF_LANG_STRING || language.trim()) return literal(text, { language: language.trim() || 'en' })
    if (range?.startsWith(XSD)) return literal(text, { datatype: range })
    return literal(text)
  }
  const term = make()
  const notice = term ? lexicalNotice(term) : null
  const add = () => {
    if (!term) return
    props.onAdd(term)
    setText('')
  }
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1">
        {range === XSD_BOOLEAN ? (
          <select
            className="rounded-md border bg-transparent px-1 py-1 text-xs"
            aria-label={`New ${column.label} value`}
            value={text || 'false'}
            onChange={(e) => setText(e.target.value)}>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        ) : (
          <>
            <input
              className={fieldInput}
              aria-label={`New ${column.label} value`}
              type={range === XSD_DATE ? 'date' : 'text'}
              list={column.objectValued ? listId : undefined}
              placeholder={column.objectValued ? 'individual (prefix:name or IRI)' : 'value'}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') add()
              }}
            />
            {column.objectValued && (
              <datalist id={listId}>
                {props.candidates.map((c) => (
                  <option key={c} value={compactIri(c, props.prefixes)} />
                ))}
              </datalist>
            )}
            {!column.objectValued && (range === null || range === RDF_LANG_STRING) && (
              <input
                className={cn(fieldInput, 'w-14 flex-none')}
                aria-label="Language tag"
                placeholder="lang"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              />
            )}
          </>
        )}
        <Button variant="outline" size="sm" className="h-7 shrink-0 text-xs" disabled={!term} onClick={add}>
          <Plus className="mr-1 h-3 w-3" /> Add
        </Button>
      </div>
      {notice && <p className="text-[10px] text-amber-700">{notice}</p>}
    </div>
  )
}

/** One existing value: its lexical form (or IRI) is editable; datatype, language and graph are kept. */
function ValueRow(props: {
  value: CellValue
  column: DataColumn
  ctx: InspectorContext
  candidates: string[]
  onChange: (term: Term) => void
  onRemove: () => void
}) {
  const { value, ctx } = props
  const listId = useId()
  const initial = value.term.type === 'iri' ? compactIri(value.term.value, ctx.prefixes) : termText(value.term, [])
  const [draft, setDraft] = useState(value.term.type === 'literal' ? value.term.value : initial)
  const editable = value.term.type === 'literal' || value.term.type === 'iri'
  const next: Term | null =
    value.term.type === 'literal'
      ? relexical(value.term, draft)
      : value.term.type === 'iri'
        ? (() => {
            const expanded = expandIri(draft.trim(), ctx.prefixes)
            return expanded ? iri(expanded) : null
          })()
        : null
  const commit = () => {
    if (next && termKey(next) !== termKey(value.term)) props.onChange(next)
  }
  const notice = next ? lexicalNotice(next) : null
  return (
    <div className="space-y-0.5">
      <div className="flex items-center gap-1">
        {editable && !ctx.readOnly ? (
          value.term.type === 'literal' && value.term.datatype === XSD_BOOLEAN ? (
            <select
              className="rounded-md border bg-transparent px-1 py-1 text-xs"
              aria-label={`${props.column.label} value`}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value)
                props.onChange(relexical(value.term, e.target.value))
              }}>
              {['true', 'false', '1', '0'].includes(draft) ? null : <option value={draft}>{draft}</option>}
              <option value="true">true</option>
              <option value="false">false</option>
              <option value="1">1</option>
              <option value="0">0</option>
            </select>
          ) : (
            <>
              <input
                className={fieldInput}
                aria-label={`${props.column.label} value`}
                list={value.term.type === 'iri' ? listId : undefined}
                dir={value.term.type === 'literal' ? (value.term.direction ?? 'auto') : undefined}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commit}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commit()
                }}
              />
              {value.term.type === 'iri' && (
                <datalist id={listId}>
                  {props.candidates.map((c) => (
                    <option key={c} value={compactIri(c, ctx.prefixes)} />
                  ))}
                </datalist>
              )}
            </>
          )
        ) : (
          <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-700" title="Edit it in All statements">
            {termText(value.term, ctx.prefixes)}
          </span>
        )}
        {value.term.type === 'literal' && value.term.language && (
          <span className="shrink-0 rounded bg-sky-50 px-1 text-[10px] text-sky-700">@{value.term.language}</span>
        )}
        {value.term.type === 'literal' && !value.term.language && value.term.datatype !== `${XSD}string` && (
          <span className="shrink-0 rounded bg-zinc-100 px-1 text-[10px] text-zinc-600">
            {compactIri(value.term.datatype, ctx.prefixes)}
          </span>
        )}
        {value.g && (ctx.graphs?.length ?? 0) > 0 && (
          <span className="shrink-0 rounded bg-violet-50 px-1 text-[10px] text-violet-700" title="Named graph">
            {graphLabel(value.g, ctx.model, ctx.prefixes)}
          </span>
        )}
        {!ctx.readOnly && (
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 shrink-0"
            aria-label={`Remove this ${props.column.label} value`}
            title="Remove this value (one statement)"
            onClick={props.onRemove}>
            <X className="h-3 w-3" />
          </Button>
        )}
      </div>
      {notice && <p className="text-[10px] text-amber-700">{notice}</p>}
    </div>
  )
}

/** Every value of one property on one individual, with add / change / remove (UDR-0187 D4). */
export function ValuesEditor(props: {
  resource: Resource
  column: DataColumn
  values: CellValue[]
  ctx: InspectorContext
  candidates: string[]
  onCommit: (edits: StatementEdit[]) => void
}) {
  const { column, values, ctx } = props
  const notice = functionalNotice(column, values.length)
  // A Functional property takes one value: no second "Add" once it has one.
  const canAdd = !ctx.readOnly && !(column.functional && values.length >= 1)
  return (
    <div className="space-y-1">
      {values.map((v) => (
        <ValueRow
          key={`${v.index}|${termKey(v.term)}|${v.g ? termKey(v.g) : ''}`}
          value={v}
          column={column}
          ctx={ctx}
          candidates={props.candidates}
          onChange={(term) => props.onCommit([{ index: v.index, statement: { p: column.predicate, o: term } }])}
          onRemove={() => props.onCommit([{ index: v.index, statement: null }])}
        />
      ))}
      {notice && <p className="text-[10px] text-amber-700">{notice}</p>}
      {canAdd && (
        <NewValue
          column={column}
          candidates={props.candidates}
          prefixes={ctx.prefixes}
          onAdd={(term) => props.onCommit([{ index: null, statement: { p: column.predicate, o: term } }])}
        />
      )}
    </div>
  )
}

/** Candidate IRIs for an object-valued column: the individuals of its range class, else all. */
function candidatesFor(column: DataColumn, index: IndividualIndex, all: Resource[]): string[] {
  const pool = column.range && index.members.has(column.range) ? (index.members.get(column.range) ?? []) : all
  return pool
    .filter((r) => r.term.type === 'iri')
    .map((r) => (r.term as { value: string }).value)
    .slice(0, 500)
}

// ---- The table ------------------------------------------------------------------------

export function IndividualsView(props: {
  /** The full model (edits address its statements). */
  model: OntologyModel
  /** The individual index and class tree of the graph-scoped model. */
  index: IndividualIndex
  tree: ClassTree
  scope: GraphScope
  ctx: InspectorContext
  classLabel: (cls: string) => string
  group: DataGroup | null
  onGroupChange: (group: DataGroup) => void
  includeSubclasses: boolean
  onIncludeSubclassesChange: (on: boolean) => void
  selectedKey: string | null
  onSelectIndividual: (key: string) => void
  onCommit: (key: string, edits: StatementEdit[]) => void
  onCreate: (cls: string) => void
}) {
  const { model, index, ctx, group } = props
  const [rowQuery, setRowQuery] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [hidden, setHidden] = useState(readHiddenColumns)
  const [editing, setEditing] = useState<{ key: string; predicate: string } | null>(null)

  // Values are read from the FULL model so statement indices are the real edit addresses.
  const byKey = useMemo(() => new Map(model.resources.map((r) => [resourceKey(r), r])), [model])
  const allIndividuals = useMemo(
    () => [...index.keys].map((k) => byKey.get(k)).filter((r): r is Resource => Boolean(r)),
    [index, byKey],
  )

  const rows = useMemo(() => {
    if (!group) return []
    const listed =
      group.kind === 'noClass'
        ? index.noClass.map((resource) => ({ resource, cls: '' }))
        : classMembers(index, props.tree, group.iri, props.includeSubclasses)
    return listed
      .map((row) => ({ ...row, resource: byKey.get(resourceKey(row.resource)) ?? row.resource }))
      .map((row) => ({ ...row, label: individualLabel(row.resource, ctx.prefixes) }))
      .sort((a, b) => a.label.localeCompare(b.label))
  }, [group, index, props.tree, props.includeSubclasses, byKey, ctx.prefixes])

  const needle = rowQuery.trim().toLowerCase()
  const filtered = needle
    ? rows.filter(
        (r) => r.label.toLowerCase().includes(needle) || termKey(r.resource.term).toLowerCase().includes(needle),
      )
    : rows

  const classes = useMemo(
    () => (group?.kind === 'class' ? [...new Set([group.iri, ...rows.map((r) => r.cls)])] : []),
    [group, rows],
  )
  const columns = useMemo(
    () =>
      dataColumns(
        model,
        classes,
        rows.map((r) => r.resource),
      ),
    [model, classes, rows],
  )
  const shown = columns.filter((c) => !hidden.has(c.predicate))

  const toggleColumn = (predicate: string, visible: boolean) => {
    const next = new Set(hidden)
    if (visible) next.delete(predicate)
    else next.add(predicate)
    setHidden(next)
    writeHiddenColumns(next)
  }

  const scoped = (resource: Resource, predicate: string) =>
    cellValues(resource, predicate).filter((v) => valueInScope(v, props.scope))

  const editingResource = editing ? byKey.get(editing.key) : undefined
  const editingColumn = editing ? columns.find((c) => c.predicate === editing.predicate) : undefined

  if (!group) {
    return (
      <div className="flex min-h-0 flex-1">
        <ClassTreePane
          tree={props.tree}
          index={index}
          classLabel={props.classLabel}
          prefixes={ctx.prefixes}
          selected={group}
          onSelect={props.onGroupChange}
        />
        <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-zinc-500">
          Select a class on the left to list its individuals.
        </div>
      </div>
    )
  }

  const title = group.kind === 'noClass' ? 'No class' : props.classLabel(group.iri)
  return (
    <div className="flex min-h-0 flex-1">
      <ClassTreePane
        tree={props.tree}
        index={index}
        classLabel={props.classLabel}
        prefixes={ctx.prefixes}
        selected={group}
        onSelect={(g) => {
          setLimit(PAGE)
          props.onGroupChange(g)
        }}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-2 py-1.5 text-xs">
          <span className="font-semibold text-zinc-800">
            {title} ({rows.length})
          </span>
          {group.kind === 'class' && (
            <label
              className="flex items-center gap-1 text-zinc-600"
              title="Also list the individuals of its subclasses (asserted rdfs:subClassOf; display only, nothing is inferred or saved)">
              <input
                type="checkbox"
                checked={props.includeSubclasses}
                onChange={(e) => props.onIncludeSubclassesChange(e.target.checked)}
              />
              Include subclasses
            </label>
          )}
          <input
            className={cn(fieldInput, 'w-40 flex-none')}
            placeholder="Filter individuals"
            aria-label="Filter individuals"
            value={rowQuery}
            onChange={(e) => setRowQuery(e.target.value)}
          />
          <div className="flex-1" />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="h-7 text-xs">
                <Columns3 className="mr-1 h-3.5 w-3.5" /> Columns
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
              {columns.map((c) => (
                <DropdownMenuCheckboxItem
                  key={c.predicate}
                  checked={!hidden.has(c.predicate)}
                  onCheckedChange={(on) => toggleColumn(c.predicate, on === true)}
                  onSelect={(e) => e.preventDefault()}>
                  {c.label}
                  {!c.inSchema && <span className="ml-1 text-[10px] text-zinc-400">not in schema</span>}
                </DropdownMenuCheckboxItem>
              ))}
              {columns.length === 0 && <p className="px-2 py-1 text-xs text-zinc-400">No columns</p>}
            </DropdownMenuContent>
          </DropdownMenu>
          {group.kind === 'class' && !ctx.readOnly && (
            <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => props.onCreate(group.iri)}>
              <Plus className="mr-1 h-3.5 w-3.5" /> Individual
            </Button>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-max min-w-full border-collapse text-[11px]">
            <thead className="sticky top-0 z-[1] bg-zinc-50">
              <tr>
                <th className="border-b px-2 py-1 text-left font-medium text-zinc-600">IRI</th>
                <th className="border-b px-2 py-1 text-left font-medium text-zinc-600">label</th>
                {shown.map((c) => (
                  <th
                    key={c.predicate}
                    className="border-b px-2 py-1 text-left font-medium text-zinc-600"
                    title={`${c.predicate}${c.inSchema ? '' : ' (not in schema)'}${c.functional ? ' (Functional)' : ''}`}>
                    {c.label}
                    {!c.inSchema && <span className="ml-0.5 text-zinc-400">*</span>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, limit).map((row) => {
                const key = resourceKey(row.resource)
                const labels = scoped(row.resource, RDFS_LABEL)
                return (
                  <tr
                    key={key}
                    className={cn('align-top', props.selectedKey === key ? 'bg-blue-50' : 'hover:bg-zinc-50')}>
                    <td className="max-w-[200px] border-b px-2 py-1">
                      <button
                        type="button"
                        className="block max-w-full truncate text-left text-blue-700 hover:underline"
                        title={termKey(row.resource.term)}
                        onClick={() => props.onSelectIndividual(key)}>
                        {termText(row.resource.term, ctx.prefixes)}
                      </button>
                      {group.kind === 'class' && row.cls !== group.iri && (
                        <span className="text-[10px] text-zinc-400">{props.classLabel(row.cls)}</span>
                      )}
                    </td>
                    <Cell
                      values={labels}
                      prefixes={ctx.prefixes}
                      label="label"
                      onOpen={() => setEditing({ key, predicate: RDFS_LABEL })}
                    />
                    {shown.map((c) => (
                      <Cell
                        key={c.predicate}
                        values={scoped(row.resource, c.predicate)}
                        prefixes={ctx.prefixes}
                        label={c.label}
                        notice={functionalNotice(c, scoped(row.resource, c.predicate).length)}
                        onOpen={() => setEditing({ key, predicate: c.predicate })}
                      />
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
          {filtered.length === 0 && <p className="p-3 text-xs text-zinc-400">No individuals.</p>}
          {filtered.length > limit && (
            <Button variant="ghost" size="sm" className="h-7 w-full text-xs" onClick={() => setLimit(limit + PAGE)}>
              Show more ({filtered.length - limit} more)
            </Button>
          )}
          <p className="px-2 py-1 text-[10px] text-zinc-400">
            Showing {Math.min(limit, filtered.length)} of {filtered.length}. * = not in the schema (used by these
            individuals only).
          </p>
        </div>
      </div>
      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-sm">
              {editingResource ? individualLabel(editingResource, ctx.prefixes) : ''} -{' '}
              {editing?.predicate === RDFS_LABEL ? 'label' : (editingColumn?.label ?? '')}
            </DialogTitle>
            <DialogDescription className="text-xs">
              Each value is one statement: adding, changing or removing a value changes only that statement.
            </DialogDescription>
          </DialogHeader>
          {editing && editingResource && (
            <ValuesEditor
              resource={editingResource}
              column={
                editingColumn ?? {
                  predicate: RDFS_LABEL,
                  label: 'label',
                  inSchema: true,
                  range: null,
                  objectValued: false,
                  functional: false,
                }
              }
              values={scoped(editingResource, editing.predicate)}
              ctx={ctx}
              candidates={editingColumn?.objectValued ? candidatesFor(editingColumn, index, allIndividuals) : []}
              onCommit={(edits) => props.onCommit(editing.key, edits)}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}

function Cell(props: {
  values: CellValue[]
  prefixes: PrefixDecl[]
  label: string
  notice?: string | null
  onOpen: () => void
}) {
  const extra = props.values.length - SHOWN_VALUES
  return (
    <td className="max-w-[240px] border-b px-1 py-0.5">
      <button
        type="button"
        className={cn(
          'flex min-h-6 w-full flex-wrap items-center gap-0.5 rounded px-1 text-left hover:bg-zinc-100',
          props.notice && 'ring-1 ring-amber-400',
        )}
        aria-label={`Edit ${props.label}`}
        title={props.notice ?? undefined}
        onClick={props.onOpen}>
        {props.values.slice(0, SHOWN_VALUES).map((v) => (
          <span
            key={`${v.index}`}
            className={cn(
              'max-w-[200px] truncate rounded px-1',
              v.term.type === 'iri' ? 'bg-blue-50 text-blue-800' : 'bg-zinc-100 text-zinc-800',
            )}>
            {termText(v.term, props.prefixes)}
          </span>
        ))}
        {extra > 0 && <span className="text-[10px] text-zinc-500">+{extra}</span>}
      </button>
    </td>
  )
}

// ---- Individual Detail (UDR-0187 D5) ---------------------------------------------------

export function IndividualDetail(props: {
  resourceKey: string
  model: OntologyModel
  index: IndividualIndex
  ctx: InspectorContext
  classLabel: (cls: string) => string
  /** Declared classes offered when adding a type. */
  classOptions: string[]
  onCommit: (edits: StatementEdit[]) => void
  onEdit: (index: number, statement: StatementInput | null) => void
  onAdd: (statement: StatementInput) => void
  onOpenClass: (cls: string) => void
  onDelete: () => void
}) {
  const { ctx } = props
  const [newType, setNewType] = useState('')
  const resource = useMemo(
    () => props.model.resources.find((r) => resourceKey(r) === props.resourceKey),
    [props.model, props.resourceKey],
  )
  const classes = useMemo(() => (resource ? individualClasses(resource) : []), [resource])
  const columns = useMemo(
    () => (resource ? dataColumns(props.model, classes, [resource]) : []),
    [props.model, classes, resource],
  )
  const allIndividuals = useMemo(
    () => props.model.resources.filter((r) => props.index.keys.has(resourceKey(r))),
    [props.model, props.index],
  )
  if (!resource) {
    return <p className="p-4 text-xs text-zinc-500">This individual no longer has any statements.</p>
  }
  const typeStatements = resource.statements
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => s.p === RDF_TYPE && s.o.type === 'iri')
  const formPredicates = new Set([RDF_TYPE, RDFS_LABEL, ...columns.map((c) => c.predicate)])
  const labelColumn: DataColumn = {
    predicate: RDFS_LABEL,
    label: 'label',
    inSchema: true,
    range: null,
    objectValued: false,
    functional: false,
  }
  return (
    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
      <div>
        <span className={fieldLabel}>Individual</span>
        <div className="break-all text-[11px] text-zinc-700" title={termKey(resource.term)}>
          {termText(resource.term, ctx.prefixes)}
        </div>
      </div>
      <div>
        <span className={fieldLabel}>Types</span>
        <div className="flex flex-wrap gap-1">
          {typeStatements.map(({ s, i }) => {
            const value = (s.o as { value: string }).value
            return (
              <span key={`${i}`} className="flex items-center gap-0.5 rounded border px-1 py-0.5 text-[11px]">
                <button
                  type="button"
                  className="max-w-[160px] truncate text-blue-700 hover:underline"
                  title={value}
                  onClick={() => props.onOpenClass(value)}>
                  {props.classLabel(value)}
                </button>
                {!ctx.readOnly && (
                  <button
                    type="button"
                    className="text-zinc-400 hover:text-zinc-700"
                    aria-label={`Remove type ${props.classLabel(value)}`}
                    title="Remove this type (one rdf:type statement)"
                    onClick={() => props.onEdit(i, null)}>
                    <X className="h-3 w-3" />
                  </button>
                )}
              </span>
            )
          })}
        </div>
        {!ctx.readOnly && (
          <div className="mt-1 flex items-center gap-1">
            <select
              className="min-w-0 flex-1 rounded-md border bg-transparent px-1 py-1 text-xs"
              aria-label="Add a type"
              value={newType}
              onChange={(e) => setNewType(e.target.value)}>
              <option value="">Add a type...</option>
              {props.classOptions
                .filter((c) => !classes.includes(c))
                .map((c) => (
                  <option key={c} value={c}>
                    {props.classLabel(c)}
                  </option>
                ))}
            </select>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              disabled={!newType}
              onClick={() => {
                props.onAdd({ p: RDF_TYPE, o: iri(newType) })
                setNewType('')
              }}>
              Add
            </Button>
          </div>
        )}
      </div>
      <div>
        <span className={fieldLabel}>label</span>
        <ValuesEditor
          key={`${props.resourceKey}|label`}
          resource={resource}
          column={labelColumn}
          values={cellValues(resource, RDFS_LABEL)}
          ctx={ctx}
          candidates={[]}
          onCommit={props.onCommit}
        />
      </div>
      {columns.map((c) => (
        <div key={c.predicate}>
          <span className={fieldLabel} title={c.predicate}>
            {c.label}
            {!c.inSchema && <span className="ml-1 normal-case text-zinc-400">(not in schema)</span>}
            {c.functional && <span className="ml-1 normal-case text-zinc-400">(Functional)</span>}
          </span>
          <ValuesEditor
            key={`${props.resourceKey}|${c.predicate}`}
            resource={resource}
            column={c}
            values={cellValues(resource, c.predicate)}
            ctx={ctx}
            candidates={c.objectValued ? candidatesFor(c, props.index, allIndividuals) : []}
            onCommit={props.onCommit}
          />
        </div>
      ))}
      <AllStatements
        resourceKey={props.resourceKey}
        ctx={ctx}
        formPredicates={formPredicates}
        onEdit={props.onEdit}
        onAdd={props.onAdd}
      />
      {!ctx.readOnly && (
        <Button variant="destructive" size="sm" className="h-7 w-full text-xs" onClick={props.onDelete}>
          <Trash2 className="mr-1 h-3 w-3" /> Delete individual
        </Button>
      )}
    </div>
  )
}
