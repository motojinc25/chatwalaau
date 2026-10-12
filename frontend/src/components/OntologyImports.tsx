/**
 * owl:imports in the Ontology editor (CTR-0173 v12, PRP-0207 / UDR-0189).
 *
 * The Imports dialog lists the imports of the open ontology, how each resolved in the
 * catalog (never fetched from the network, D1 / D2) and what they bring in; it adds and
 * removes `owl:imports` statements (ordinary statement edits, saved with Save, D9) and
 * edits the catalog-wide mapping table (`PUT /api/ontology/import-map`, D3). The
 * imported vocabulary is read-only: `ImportedDetail` shows an imported term without
 * any way to change it (D6 / D8).
 */

import { ExternalLink, Loader2, Plus, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { fieldInput, fieldLabel } from '@/components/OntologyInspector'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  compactIri,
  displayLiteral,
  expandIri,
  type GraphTerm,
  graphKey,
  type ImportedIndex,
  type ImportItem,
  type ImportsInfo,
  localName,
  type PrefixDecl,
  RDFS_COMMENT,
  RDFS_LABEL,
  type Term,
  termKey,
} from '@/lib/ontologyModel'
import { cn } from '@/lib/utils'

/** One `owl:imports` statement of the open (possibly unsaved) model. */
export interface ModelImport {
  iri: string
  subject: Term
  graph: GraphTerm | null
}

export type ImportsTab = 'imports' | 'mapping'

const STATUS_TEXT: Record<string, string> = {
  resolved: 'resolved',
  builtin: 'built-in',
  missing: 'not in the catalog',
  ambiguous: 'ambiguous',
  cycle: 'cycle (cut)',
  self: 'this ontology (ignored)',
  over_limit: 'over the limit',
}

const MATCH_TEXT: Record<string, string> = {
  map: 'mapping table',
  builtin: 'W3C vocabulary',
  catalog: 'catalog IRI',
  ontology_iri: 'ontology IRI',
  version_iri: 'version IRI',
}

/** The warnings the Imports button counts: unresolved, ambiguous, over the limit. */
export function importWarningCount(info: ImportsInfo | null): number {
  if (!info) return 0
  return new Set(
    info.imports
      .filter((i) => i.status === 'missing' || i.status === 'ambiguous' || i.status === 'over_limit')
      .map((i) => i.iri),
  ).size
}

function StatusBadge(props: { status: string }) {
  const warn = props.status === 'missing' || props.status === 'ambiguous' || props.status === 'over_limit'
  const quiet = props.status === 'cycle' || props.status === 'self' || props.status === 'unsaved'
  return (
    <span
      className={cn(
        'shrink-0 rounded px-1 text-[10px]',
        warn ? 'bg-amber-100 text-amber-800' : quiet ? 'bg-zinc-100 text-zinc-500' : 'bg-emerald-50 text-emerald-700',
      )}>
      {props.status === 'unsaved' ? 'save to resolve' : (STATUS_TEXT[props.status] ?? props.status)}
    </span>
  )
}

function ImportRow(props: {
  item: ImportItem | null
  iri: string
  prefixes: PrefixDecl[]
  depth: number
  readOnly: boolean
  onRemove?: () => void
  onMap: (iri: string) => void
  onOpenOntology: (id: string) => void
}) {
  const { item } = props
  const status = item?.status ?? 'unsaved'
  return (
    <div
      className="flex items-center gap-1.5 rounded border px-2 py-1 text-[11px]"
      style={{ marginLeft: props.depth * 16 }}
      data-import-status={status}>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          {item?.ontology ? (
            <button
              type="button"
              className="truncate font-medium text-blue-700 hover:underline"
              title="Open this ontology"
              onClick={() => item.ontology && props.onOpenOntology(item.ontology.id)}>
              {item.ontology.name}
            </button>
          ) : (
            <span className="truncate font-medium text-zinc-700">{compactIri(props.iri, props.prefixes)}</span>
          )}
          <StatusBadge status={status} />
          {item?.match && <span className="shrink-0 text-[10px] text-zinc-400">by {MATCH_TEXT[item.match]}</span>}
          {item?.statements != null && (
            <span className="shrink-0 text-[10px] text-zinc-400">{item.statements} statements</span>
          )}
        </div>
        <div className="truncate text-[10px] text-zinc-400" title={props.iri}>
          &lt;{props.iri}&gt;
          {item?.status === 'ambiguous' && ` -- matches ${item.candidates.map((c) => c.name).join(', ')}`}
        </div>
      </div>
      {!props.readOnly && (status === 'missing' || status === 'ambiguous') && (
        <Button variant="outline" size="sm" className="h-6 px-1.5 text-[10px]" onClick={() => props.onMap(props.iri)}>
          Map...
        </Button>
      )}
      {!props.readOnly && props.onRemove && (
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-zinc-500 hover:text-red-600"
          aria-label={`Remove the import of ${props.iri}`}
          title="Remove this owl:imports statement"
          onClick={props.onRemove}>
          <Trash2 className="h-3 w-3" />
        </Button>
      )}
    </div>
  )
}

interface MapRow {
  iri: string
  ontology_id: string
}

/** The Imports dialog (D9): imports of the open ontology and the catalog-wide mapping table (D3). */
export function ImportsDialog(props: {
  open: boolean
  onOpenChange: (open: boolean) => void
  ontologyId: string | null
  info: ImportsInfo | null
  loading: boolean
  /** The `owl:imports` statements of the open model (saved or not). */
  modelImports: ModelImport[]
  /** The IRI an added import is declared on: the ontology IRI, else the catalog IRI. */
  selfIri: string
  catalog: { id: string; name: string }[]
  readOnly: boolean
  prefixes: PrefixDecl[]
  tab: ImportsTab
  onTabChange: (tab: ImportsTab) => void
  /** An import IRI to put into a new mapping row (Map... / a Checks finding). */
  mapIri: string | null
  onAdd: (target: string) => void
  onRemove: (item: ModelImport) => void
  onMapSaved: () => void
  /** Open the Mapping tab with a new row for `iri`. */
  onMap: (iri: string) => void
  onOpenOntology: (id: string) => void
}) {
  const { info } = props
  const [addId, setAddId] = useState('')
  const [addIri, setAddIri] = useState('')
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [rows, setRows] = useState<MapRow[] | null>(null)
  const [mapError, setMapError] = useState<string | null>(null)
  const [mapSaving, setMapSaving] = useState(false)

  const directIds = useMemo(
    () =>
      new Set(
        (info?.imports ?? [])
          .filter((i) => i.direct)
          .map((i) => i.ontology?.id)
          .filter(Boolean),
      ),
    [info],
  )
  const candidates = props.catalog.filter((c) => c.id !== props.ontologyId && !directIds.has(c.id))

  const loadMap = useCallback(async () => {
    setMapError(null)
    try {
      const res = await fetch('/api/ontology/import-map')
      if (!res.ok) throw new Error('Could not load the mapping table')
      const body = await res.json()
      const loaded = ((body.entries ?? []) as MapRow[]).map((e) => ({ iri: e.iri, ontology_id: e.ontology_id }))
      if (props.mapIri && !loaded.some((r) => r.iri === props.mapIri)) {
        loaded.push({ iri: props.mapIri, ontology_id: '' })
      }
      setRows(loaded)
    } catch (err) {
      setMapError(err instanceof Error ? err.message : 'Could not load the mapping table')
    }
  }, [props.mapIri])

  useEffect(() => {
    if (props.open && props.tab === 'mapping') void loadMap()
    if (!props.open) {
      setRows(null)
      setAddError(null)
    }
  }, [props.open, props.tab, loadMap])

  const saveMap = useCallback(async () => {
    if (!rows) return
    setMapSaving(true)
    setMapError(null)
    try {
      const entries = rows
        .map((r) => ({ iri: r.iri.trim(), ontology_id: r.ontology_id }))
        .filter((r) => r.iri && r.ontology_id)
      const res = await fetch('/api/ontology/import-map', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entries }),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) {
        const detail = body?.detail
        throw new Error(typeof detail?.message === 'string' ? detail.message : 'Could not save the mapping table')
      }
      setRows(((body?.entries ?? []) as MapRow[]).map((e) => ({ iri: e.iri, ontology_id: e.ontology_id })))
      props.onMapSaved()
    } catch (err) {
      setMapError(err instanceof Error ? err.message : 'Could not save the mapping table')
    } finally {
      setMapSaving(false)
    }
  }, [rows, props.onMapSaved])

  /** Add from the catalog: its single ontology IRI, else its catalog IRI (D9). */
  const addFromCatalog = useCallback(async () => {
    if (!addId) return
    setAdding(true)
    setAddError(null)
    try {
      const res = await fetch(`/api/ontology/${addId}/imports`)
      if (!res.ok) throw new Error('Could not read that ontology')
      const body = (await res.json()) as ImportsInfo
      const target = body.ontology_iris.length === 1 ? body.ontology_iris[0] : body.catalog_iri
      props.onAdd(target)
      setAddId('')
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Could not add the import')
    } finally {
      setAdding(false)
    }
  }, [addId, props.onAdd])

  const typed = addIri.trim() ? expandIri(addIri.trim(), props.prefixes) : null

  const itemFor = (m: ModelImport) =>
    (info?.imports ?? []).find(
      (i) =>
        i.direct &&
        i.iri === m.iri &&
        termKey(i.subject) === termKey(m.subject) &&
        graphKey(i.graph) === graphKey(m.graph),
    ) ?? null

  const renderChildren = (parentIri: string, depth: number, seen: Set<string>): React.ReactNode =>
    depth > 6
      ? null
      : (info?.imports ?? [])
          .filter((i) => !i.direct && i.parent === parentIri)
          .map((child) => {
            const id = `${parentIri} ${child.iri}`
            if (seen.has(id)) return null
            const next = new Set(seen).add(id)
            return (
              <div key={id} className="space-y-1">
                <ImportRow
                  item={child}
                  iri={child.iri}
                  prefixes={props.prefixes}
                  depth={depth}
                  readOnly={props.readOnly}
                  onMap={(iri) => {
                    setRows(null)
                    props.onMap(iri)
                  }}
                  onOpenOntology={props.onOpenOntology}
                />
                {child.status === 'resolved' && renderChildren(child.iri, depth + 1, next)}
              </div>
            )
          })

  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Imports</DialogTitle>
          <DialogDescription>
            Imports are resolved from the ontologies in this catalog only; nothing is downloaded. Imported terms are
            read-only and are never written into this ontology.
          </DialogDescription>
        </DialogHeader>
        <div className="flex gap-1 border-b text-xs">
          {(['imports', 'mapping'] as const).map((tab) => (
            <button
              key={tab}
              type="button"
              className={cn(
                'px-3 py-1.5 font-medium capitalize',
                props.tab === tab ? 'border-b-2 border-blue-500 text-blue-600' : 'text-zinc-500',
              )}
              onClick={() => props.onTabChange(tab)}>
              {tab === 'imports' ? 'Imports' : 'Mapping'}
            </button>
          ))}
        </div>
        {props.tab === 'imports' ? (
          <div className="space-y-3 text-xs">
            <p className="break-all text-[11px] text-zinc-500">
              This ontology: <span className="font-mono">{props.selfIri}</span>
            </p>
            {props.loading && (
              <p className="flex items-center gap-1 text-zinc-500">
                <Loader2 className="h-3 w-3 animate-spin" /> Resolving...
              </p>
            )}
            <div className="space-y-1" data-imports-list>
              {props.modelImports.length === 0 && <p className="text-zinc-500">This ontology imports nothing.</p>}
              {props.modelImports.map((m) => {
                const item = itemFor(m)
                return (
                  <div key={`${termKey(m.subject)} ${m.iri} ${graphKey(m.graph)}`} className="space-y-1">
                    <ImportRow
                      item={item}
                      iri={m.iri}
                      prefixes={props.prefixes}
                      depth={0}
                      readOnly={props.readOnly}
                      onRemove={() => props.onRemove(m)}
                      onMap={(iri) => {
                        setRows(null)
                        props.onMap(iri)
                      }}
                      onOpenOntology={props.onOpenOntology}
                    />
                    {item?.status === 'resolved' && renderChildren(m.iri, 1, new Set())}
                  </div>
                )
              })}
            </div>
            {(info?.notices ?? []).map((n) => (
              <p key={n} className="text-[11px] text-amber-700">
                {n}
              </p>
            ))}
            {!props.readOnly && (
              <div className="space-y-2 rounded border bg-zinc-50 p-2">
                <div>
                  <span className={fieldLabel}>Add from the catalog</span>
                  <div className="flex items-center gap-1">
                    <select
                      className="h-7 flex-1 rounded-md border bg-white px-1 text-xs"
                      aria-label="Ontology to import"
                      value={addId}
                      onChange={(e) => setAddId(e.target.value)}>
                      <option value="">Choose an ontology...</option>
                      {candidates.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                    </select>
                    <Button
                      size="sm"
                      className="h-7 text-xs"
                      disabled={!addId || adding}
                      onClick={() => void addFromCatalog()}>
                      {adding ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Plus className="mr-1 h-3 w-3" />}
                      Add
                    </Button>
                  </div>
                </div>
                <div>
                  <span className={fieldLabel}>Add by IRI</span>
                  <div className="flex items-center gap-1">
                    <input
                      className={cn(fieldInput, 'flex-1')}
                      aria-label="Import IRI"
                      placeholder="https://example.org/ontology or prefix:name"
                      value={addIri}
                      onChange={(e) => setAddIri(e.target.value)}
                    />
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs"
                      disabled={!typed}
                      onClick={() => {
                        if (!typed) return
                        props.onAdd(typed)
                        setAddIri('')
                      }}>
                      Add
                    </Button>
                  </div>
                </div>
                {addError && <p className="text-[11px] text-red-600">{addError}</p>}
                <p className="text-[10px] text-zinc-500">
                  Adding or removing an import changes this ontology (one owl:imports statement, plus the ontology
                  header when there is none). Save to apply it; History can undo it.
                </p>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2 text-xs" data-import-map>
            <p className="text-[11px] text-zinc-500">
              One table for the whole catalog: an import IRI listed here always means the chosen ontology, whatever IRI
              that ontology declares. Nothing is written into the ontology files.
            </p>
            {rows === null && !mapError && (
              <p className="flex items-center gap-1 text-zinc-500">
                <Loader2 className="h-3 w-3 animate-spin" /> Loading...
              </p>
            )}
            {rows?.map((row, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: rows are edited in place by position
              <div key={i} className="flex items-center gap-1">
                <input
                  className={cn(fieldInput, 'flex-1 font-mono')}
                  aria-label="Import IRI"
                  placeholder="https://example.org/ontology"
                  value={row.iri}
                  readOnly={props.readOnly}
                  onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, iri: e.target.value } : r)))}
                />
                <span className="text-zinc-400">means</span>
                <select
                  className="h-7 w-48 rounded-md border bg-white px-1 text-xs"
                  aria-label="Catalog ontology"
                  value={row.ontology_id}
                  disabled={props.readOnly}
                  onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, ontology_id: e.target.value } : r)))}>
                  <option value="">Choose...</option>
                  {props.catalog.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
                {!props.readOnly && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 text-zinc-500 hover:text-red-600"
                    aria-label="Remove this mapping"
                    onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                    <Trash2 className="h-3 w-3" />
                  </Button>
                )}
              </div>
            ))}
            {rows && rows.length === 0 && <p className="text-zinc-500">No mappings.</p>}
            {mapError && <p className="text-[11px] text-red-600">{mapError}</p>}
            {!props.readOnly && rows && (
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => setRows([...rows, { iri: '', ontology_id: '' }])}>
                  <Plus className="mr-1 h-3 w-3" /> Add mapping
                </Button>
                <Button size="sm" className="h-7 text-xs" disabled={mapSaving} onClick={() => void saveMap()}>
                  {mapSaving ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                  Save mappings
                </Button>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** A read-only Detail for an imported term (D6 / D8): its statements and where it comes from. */
export function ImportedDetail(props: {
  iri: string
  imported: ImportedIndex
  prefixes: PrefixDecl[]
  formatTerm: (term: Term) => string
  onOpenSource: (id: string) => void
}) {
  const resource = props.imported.byIri.get(props.iri)
  if (!resource) {
    return <p className="p-3 text-xs text-zinc-500">{compactIri(props.iri, props.prefixes)} is no longer imported.</p>
  }
  const source = props.imported.sources.get(resource.source) ?? resource.source
  const label = displayLiteral(resource, RDFS_LABEL) || localName(props.iri)
  const comment = displayLiteral(resource, RDFS_COMMENT)
  return (
    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3 text-xs" data-imported-detail>
      <div>
        <span className={fieldLabel}>Imported {resource.role === 'entity' ? 'class' : 'term'} (read-only)</span>
        <div className="text-sm font-medium text-zinc-800">{label}</div>
        <div className="break-all text-[10px] text-zinc-400">{props.iri}</div>
      </div>
      {comment && <p className="text-zinc-600">{comment}</p>}
      <div className="flex items-center gap-1.5">
        <span className="text-zinc-500">From</span>
        <span className="font-medium">{source}</span>
        <Button
          variant="outline"
          size="sm"
          className="ml-auto h-6 px-2 text-[10px]"
          onClick={() => props.onOpenSource(resource.source)}>
          <ExternalLink className="mr-1 h-3 w-3" /> Open in its ontology
        </Button>
      </div>
      <div>
        <span className={fieldLabel}>Statements</span>
        <ul className="space-y-0.5 font-mono text-[11px] text-zinc-600">
          {resource.statements.map((s) => (
            <li key={`${s.p} ${termKey(s.o)} ${graphKey(s.g)}`} className="break-all">
              {compactIri(s.p, props.prefixes)} {props.formatTerm(s.o)}
            </li>
          ))}
        </ul>
      </div>
      <p className="text-[10px] text-zinc-400">
        Imported terms are changed in their own ontology. Statements of this ontology that use it (a superclass, a
        domain or range, an individual's type) are edited where they are.
      </p>
    </div>
  )
}
