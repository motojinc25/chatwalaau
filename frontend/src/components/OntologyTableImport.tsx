/**
 * Import table into the open ontology (PRP-0217, UDR-0199, CTR-0173 v18).
 *
 * 1 File: a CSV / TSV file read by `POST /api/ontology/{id}/table/inspect` (encoding and
 *   delimiter detected, both can be chosen). 2 Mapping: one table -> one class, a subject
 *   IRI template from key columns, one rule per column (value / reference / ignore),
 *   pre-filled from the schema; saved per ontology or loaded from / downloaded as a
 *   `.json` file (CTR-0247). 3 Preview: `POST /{id}/table/preview` converts the rows and
 *   validates the stored model plus the rows with the ontology's SHACL settings; rows
 *   with Violations are excluded and listed with their reasons, and can be downloaded
 *   to be corrected. 4 Apply: the accepted rows become UNSAVED additions; Save writes
 *   them. Nothing is written before Save; the import needs an editor without unsaved
 *   edits (D1), so the preview checks exactly what will be saved.
 */

import { Download, FileUp, Loader2, Save, TableProperties } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
  compactIri,
  type GraphTerm,
  graphsOf,
  type OntologyModel,
  type PrefixDecl,
  RDF_TYPE,
  RDFS_LABEL,
} from '@/lib/ontologyModel'
import {
  type ColumnRule,
  type ExistingPolicy,
  emptyMapping,
  excludedRowsCsv,
  fitMapping,
  mappingJson,
  mappingProblems,
  parseMappingFile,
  reasonText,
  STAGE_LABELS,
  TABLE_ACCEPT,
  TABLE_DATATYPES,
  TABLE_DELIMITERS,
  TABLE_ENCODINGS,
  type TableInspection,
  type TableMapping,
  type TablePreview,
} from '@/lib/ontologyTable'

async function errorMessage(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null)
  const detail = body?.detail
  if (typeof detail === 'string') return detail
  if (typeof detail?.message === 'string') return detail.message
  return fallback
}

function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

/** `ex:Book` -> the full IRI through the model's prefixes; full IRIs stay. */
function expandName(text: string, prefixes: PrefixDecl[]): string {
  const value = text.trim()
  if (value.startsWith('<') && value.endsWith('>')) return value.slice(1, -1)
  const colon = value.indexOf(':')
  if (colon > 0 && !value.includes('//')) {
    const decl = prefixes.find((p) => p.prefix === value.slice(0, colon))
    if (decl) return decl.iri + value.slice(colon + 1)
  }
  return value
}

function labelOf(model: OntologyModel, iri: string): string {
  const resource = model.resources.find((r) => r.term.type === 'iri' && r.term.value === iri)
  const label = resource?.statements.find((s) => s.p === RDFS_LABEL && s.o.type === 'literal')
  const short = compactIri(iri, model.document.prefixes)
  return label && label.o.type === 'literal' ? `${short} (${label.o.value})` : short
}

function NameInput(props: {
  value: string
  onChange: (iri: string) => void
  listId?: string
  prefixes: PrefixDecl[]
  label: string
  className?: string
  placeholder?: string
}) {
  const [text, setText] = useState(props.value ? compactIri(props.value, props.prefixes) : '')
  useEffect(() => {
    setText(props.value ? compactIri(props.value, props.prefixes) : '')
  }, [props.value, props.prefixes])
  return (
    <input
      className={props.className ?? 'h-7 w-full rounded border px-1 text-xs'}
      list={props.listId}
      aria-label={props.label}
      placeholder={props.placeholder}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => props.onChange(expandName(text, props.prefixes))}
    />
  )
}

export function TableImportDialog(props: {
  ontologyId: string
  ontologyName: string
  model: OntologyModel
  /** The catalog's minting namespace for new terms (relative templates resolve against it). */
  baseIri: string
  revision: string | null
  /** Unsaved edits in the editor: the import waits for Save or Discard (D1). */
  dirty: boolean
  onSave: () => void
  onDiscard: () => void
  /** The Data view's selected class (the default target class). */
  defaultClass: string | null
  graphName: (g: GraphTerm) => string
  readOnly: boolean
  onClose: () => void
  onApply: (preview: TablePreview, mapping: TableMapping) => void
}) {
  const { model, ontologyId } = props
  const prefixes = model.document.prefixes
  const fileRef = useRef<HTMLInputElement>(null)
  const mappingFileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [encoding, setEncoding] = useState('auto')
  const [delimiter, setDelimiter] = useState('auto')
  const [headerRow, setHeaderRow] = useState(1)
  const [inspection, setInspection] = useState<TableInspection | null>(null)
  const [mapping, setMappingState] = useState<TableMapping | null>(null)
  const [preview, setPreview] = useState<TablePreview | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [saved, setSaved] = useState<TableMapping[]>([])
  const [saveName, setSaveName] = useState('')
  const [withoutCheck, setWithoutCheck] = useState(false)

  const setMapping = useCallback((next: TableMapping | null) => {
    setMappingState(next)
    setPreview(null)
    setWithoutCheck(false)
  }, [])

  const classes = useMemo(() => {
    const out = new Set<string>()
    for (const r of model.resources) {
      if (r.role === 'entity' && r.term.type === 'iri') out.add(r.term.value)
      for (const s of r.statements) if (s.p === RDF_TYPE && s.o.type === 'iri') out.add(s.o.value)
    }
    return [...out].sort()
  }, [model])
  const properties = useMemo(() => {
    const out = new Set<string>()
    for (const r of model.resources) {
      if ((r.role === 'object_property' || r.role === 'datatype_property') && r.term.type === 'iri')
        out.add(r.term.value)
      for (const s of r.statements) if (s.p !== RDF_TYPE) out.add(s.p)
    }
    return [...out].sort()
  }, [model])
  const graphs = graphsOf(model)
  const targetGraph = model.targetGraph
  const defaultGraph = targetGraph && targetGraph.type === 'iri' ? targetGraph.value : null

  const fetchSaved = useCallback(async () => {
    try {
      const res = await fetch(`/api/ontology/${ontologyId}/table/mappings`)
      if (res.ok) setSaved((await res.json()).mappings ?? [])
    } catch {
      // the list stays empty; saving still reports its own error
    }
  }, [ontologyId])
  useEffect(() => {
    void fetchSaved()
  }, [fetchSaved])

  const inspect = async (chosen: File, cls: string | null, keep: TableMapping | null) => {
    setBusy('Reading the file...')
    setFailed(null)
    setNotice(null)
    try {
      const form = new FormData()
      form.append('file', chosen)
      form.append('encoding', encoding)
      form.append('delimiter', delimiter)
      form.append('header_row', String(headerRow))
      if (cls) form.append('class', cls)
      const res = await fetch(`/api/ontology/${ontologyId}/table/inspect`, { method: 'POST', body: form })
      if (!res.ok) throw new Error(await errorMessage(res, 'The file could not be read'))
      const body = (await res.json()) as TableInspection
      setInspection(body)
      if (keep) {
        const fitted = fitMapping(keep, body.columns)
        setMapping(fitted.mapping)
        if (fitted.dropped.length)
          setNotice(`Rules for columns this file does not have were dropped: ${fitted.dropped.join(', ')}`)
      } else if (body.suggestion) {
        setMapping({ ...body.suggestion, graph: defaultGraph })
      } else if (cls) {
        setMapping({ ...emptyMapping(body, cls, props.baseIri), graph: defaultGraph })
      } else {
        setMapping(null)
      }
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'The file could not be read')
      setInspection(null)
    } finally {
      setBusy(null)
    }
  }

  const chooseClass = (cls: string) => {
    if (!file || !cls) return
    void inspect(file, cls, null)
  }

  const runPreview = async () => {
    if (!file || !mapping) return
    setBusy('Converting and validating (SHACL)...')
    setFailed(null)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append(
        'mapping',
        JSON.stringify({ ...mapping, source: { ...mapping.source, encoding, delimiter, header_row: headerRow } }),
      )
      if (props.revision) form.append('revision', props.revision)
      const res = await fetch(`/api/ontology/${ontologyId}/table/preview`, { method: 'POST', body: form })
      if (!res.ok) throw new Error(await errorMessage(res, 'The preview failed'))
      setPreview((await res.json()) as TablePreview)
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'The preview failed')
    } finally {
      setBusy(null)
    }
  }

  const saveMapping = async () => {
    if (!mapping || !saveName.trim()) return
    setBusy('Saving the mapping...')
    setFailed(null)
    try {
      const name = saveName.trim()
      const res = await fetch(`/api/ontology/${ontologyId}/table/mappings/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...mapping, name }),
      })
      if (!res.ok) throw new Error(await errorMessage(res, 'The mapping could not be saved'))
      setMappingState({ ...mapping, name })
      setNotice(`Mapping "${name}" saved.`)
      await fetchSaved()
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'The mapping could not be saved')
    } finally {
      setBusy(null)
    }
  }

  const deleteMapping = async (name: string) => {
    setBusy('Deleting the mapping...')
    try {
      const res = await fetch(`/api/ontology/${ontologyId}/table/mappings/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(await errorMessage(res, 'The mapping could not be deleted'))
      await fetchSaved()
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'The mapping could not be deleted')
    } finally {
      setBusy(null)
    }
  }

  const applyMapping = (chosen: TableMapping) => {
    setEncoding(chosen.source.encoding || 'auto')
    setDelimiter(chosen.source.delimiter || 'auto')
    setHeaderRow(chosen.source.header_row || 1)
    setSaveName(chosen.name)
    if (inspection) {
      const fitted = fitMapping(chosen, inspection.columns)
      setMapping(fitted.mapping)
      setNotice(
        fitted.dropped.length
          ? `Rules for columns this file does not have were dropped: ${fitted.dropped.join(', ')}`
          : `Mapping "${chosen.name || 'from file'}" applied.`,
      )
    }
  }

  const updateRule = (index: number, rule: ColumnRule) => {
    if (!mapping) return
    const columns = [...mapping.columns]
    columns[index] = rule
    setMapping({ ...mapping, columns })
  }

  const problems = mapping && inspection ? mappingProblems(mapping, inspection.columns) : []
  const shaclBlocked = preview ? !preview.shacl.checked && preview.shacl.reason !== 'not_configured' : false
  const canApply =
    !!preview && !!mapping && preview.rows.accepted > 0 && (!shaclBlocked || withoutCheck) && !props.dirty && !busy

  return (
    <Dialog open onOpenChange={(o) => !o && !busy && props.onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>Import table into {props.ontologyName}</DialogTitle>
          <DialogDescription>
            Each row of a CSV / TSV file becomes an individual of one class. The rows are checked with this ontology's
            SHACL settings before anything changes; rows with violations are left out and listed. The accepted rows are
            added as unsaved changes: check them, then Save.
          </DialogDescription>
        </DialogHeader>

        {props.dirty && (
          <div className="flex flex-wrap items-center gap-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
            The editor has unsaved changes. Save or discard them first, so the preview checks exactly what will be
            saved.
            <Button size="sm" className="h-7 text-xs" onClick={props.onSave}>
              Save changes
            </Button>
            <Button size="sm" variant="outline" className="h-7 text-xs" onClick={props.onDiscard}>
              Discard changes
            </Button>
          </div>
        )}

        <div className="space-y-4 text-xs">
          {/* 1 File */}
          <section className="space-y-2" aria-label="1 File">
            <h3 className="font-semibold text-zinc-700">1. File</h3>
            <div className="flex flex-wrap items-center gap-2">
              <input
                ref={fileRef}
                type="file"
                accept={TABLE_ACCEPT}
                className="hidden"
                aria-label="Table file"
                onChange={(e) => {
                  const chosen = e.target.files?.[0]
                  e.target.value = ''
                  if (!chosen) return
                  setFile(chosen)
                  void inspect(chosen, mapping?.class ?? props.defaultClass, mapping)
                }}
              />
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                disabled={!!busy || props.dirty}
                onClick={() => fileRef.current?.click()}>
                <FileUp className="mr-1 h-3.5 w-3.5" /> Choose a file...
              </Button>
              {file && <span className="font-medium">{file.name}</span>}
              <label className="flex items-center gap-1">
                Encoding
                <select
                  className="h-7 rounded border px-1"
                  aria-label="Encoding"
                  value={encoding}
                  onChange={(e) => setEncoding(e.target.value)}>
                  {TABLE_ENCODINGS.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1">
                Delimiter
                <select
                  className="h-7 rounded border px-1"
                  aria-label="Delimiter"
                  value={delimiter}
                  onChange={(e) => setDelimiter(e.target.value)}>
                  {TABLE_DELIMITERS.map((d) => (
                    <option key={d.label} value={d.value}>
                      {d.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1">
                Header row
                <input
                  type="number"
                  min={1}
                  className="h-7 w-14 rounded border px-1"
                  aria-label="Header row"
                  value={headerRow}
                  onChange={(e) => setHeaderRow(Math.max(1, Number(e.target.value) || 1))}
                />
              </label>
              {file && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-xs"
                  disabled={!!busy}
                  onClick={() => void inspect(file, mapping?.class ?? props.defaultClass, mapping)}>
                  Read again
                </Button>
              )}
            </div>
            {inspection && (
              <div data-testid="table-inspection" className="space-y-1">
                <p className="text-zinc-600">
                  {inspection.row_count} row(s), {inspection.columns.length} column(s), encoding {inspection.encoding},
                  delimiter {inspection.delimiter === '\t' ? 'tab' : `"${inspection.delimiter}"`}
                </p>
                <div className="max-h-32 overflow-auto rounded border">
                  <table className="w-full text-[11px]">
                    <thead className="bg-zinc-50">
                      <tr>
                        <th className="px-1 text-left text-zinc-400">line</th>
                        {inspection.columns.map((c) => (
                          <th key={c} className="px-1 text-left">
                            {c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {inspection.sample.slice(0, 5).map((row) => (
                        <tr key={row.row} className="border-t">
                          <td className="px-1 text-zinc-400">{row.line}</td>
                          {inspection.columns.map((c, i) => (
                            <td key={c} className="max-w-[160px] truncate px-1">
                              {row.cells[i] ?? ''}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </section>

          {/* 2 Mapping */}
          {inspection && (
            <section className="space-y-2" aria-label="2 Mapping">
              <h3 className="font-semibold text-zinc-700">2. Mapping</h3>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  className="h-7 rounded border px-1"
                  aria-label="Saved mapping"
                  value=""
                  onChange={(e) => {
                    const chosen = saved.find((m) => m.name === e.target.value)
                    if (chosen) applyMapping(chosen)
                  }}>
                  <option value="">Use a saved mapping...</option>
                  {saved.map((m) => (
                    <option key={m.name} value={m.name}>
                      {m.name}
                    </option>
                  ))}
                </select>
                <input
                  ref={mappingFileRef}
                  type="file"
                  accept=".json"
                  className="hidden"
                  aria-label="Mapping file"
                  onChange={async (e) => {
                    const chosen = e.target.files?.[0]
                    e.target.value = ''
                    if (!chosen) return
                    try {
                      applyMapping(parseMappingFile(await chosen.text()))
                    } catch (err) {
                      setFailed(err instanceof Error ? err.message : 'Not a mapping file')
                    }
                  }}
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => mappingFileRef.current?.click()}>
                  Load mapping file...
                </Button>
                {mapping && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() =>
                      download(`${mapping.name || 'table-mapping'}.json`, mappingJson(mapping), 'application/json')
                    }>
                    <Download className="mr-1 h-3.5 w-3.5" /> Download mapping
                  </Button>
                )}
                {mapping && !props.readOnly && (
                  <>
                    <input
                      className="h-7 w-40 rounded border px-1"
                      placeholder="Mapping name"
                      aria-label="Mapping name"
                      value={saveName}
                      onChange={(e) => setSaveName(e.target.value)}
                    />
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 text-xs"
                      disabled={!saveName.trim() || !!busy}
                      onClick={() => void saveMapping()}>
                      <Save className="mr-1 h-3.5 w-3.5" /> Save mapping
                    </Button>
                  </>
                )}
                {saved.length > 0 && !props.readOnly && (
                  <select
                    className="h-7 rounded border px-1"
                    aria-label="Delete a saved mapping"
                    value=""
                    onChange={(e) => e.target.value && void deleteMapping(e.target.value)}>
                    <option value="">Delete a saved mapping...</option>
                    {saved.map((m) => (
                      <option key={m.name} value={m.name}>
                        {m.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <datalist id="table-import-classes">
                {classes.map((c) => (
                  <option key={c} value={compactIri(c, prefixes)}>
                    {labelOf(model, c)}
                  </option>
                ))}
              </datalist>
              <datalist id="table-import-properties">
                {properties.map((p) => (
                  <option key={p} value={compactIri(p, prefixes)}>
                    {labelOf(model, p)}
                  </option>
                ))}
              </datalist>

              <div className="grid grid-cols-[140px_1fr] items-center gap-x-2 gap-y-1">
                <span>Class of the rows</span>
                <div className="flex items-center gap-2">
                  <select
                    className="h-7 w-72 rounded border px-1"
                    aria-label="Class of the rows"
                    value={mapping?.class ?? ''}
                    onChange={(e) => chooseClass(e.target.value)}>
                    <option value="">Choose a class...</option>
                    {classes.map((c) => (
                      <option key={c} value={c}>
                        {labelOf(model, c)}
                      </option>
                    ))}
                  </select>
                  <span className="text-zinc-500">
                    Choosing a class suggests the rules from its properties and shapes.
                  </span>
                </div>
                {mapping && (
                  <>
                    <span>Subject IRI</span>
                    <input
                      className="h-7 w-full rounded border px-1 font-mono"
                      aria-label="Subject template"
                      value={mapping.subject.template}
                      onChange={(e) => setMapping({ ...mapping, subject: { template: e.target.value } })}
                    />
                    <span>Graph</span>
                    <div className="flex items-center gap-2">
                      <select
                        className="h-7 rounded border px-1"
                        aria-label="Target graph"
                        value={mapping.graph ?? ''}
                        onChange={(e) => setMapping({ ...mapping, graph: e.target.value || null })}>
                        <option value="">default graph</option>
                        {graphs
                          .filter((g) => g.type === 'iri')
                          .map((g) => (
                            <option key={g.value} value={g.value}>
                              {props.graphName(g)}
                            </option>
                          ))}
                        {mapping.graph && !graphs.some((g) => g.value === mapping.graph) && (
                          <option value={mapping.graph}>{mapping.graph} (new)</option>
                        )}
                      </select>
                      <NameInput
                        value=""
                        prefixes={prefixes}
                        label="New graph IRI"
                        placeholder="or a new graph IRI"
                        className="h-7 w-64 rounded border px-1"
                        onChange={(iri) => iri && setMapping({ ...mapping, graph: iri })}
                      />
                    </div>
                    <span>Existing subjects</span>
                    <select
                      className="h-7 w-72 rounded border px-1"
                      aria-label="Existing subjects"
                      value={mapping.existing}
                      onChange={(e) => setMapping({ ...mapping, existing: e.target.value as ExistingPolicy })}>
                      <option value="add">add the values (default)</option>
                      <option value="replace">replace the values of the mapped properties</option>
                      <option value="skip">skip the row</option>
                    </select>
                    <span>Empty values</span>
                    <input
                      className="h-7 w-72 rounded border px-1"
                      aria-label="Empty values"
                      placeholder="e.g. -, N/A"
                      title="Cells with one of these texts produce no statement (comma-separated; an empty cell always counts)"
                      value={mapping.source.null_values.filter((v) => v !== '').join(', ')}
                      onChange={(e) =>
                        setMapping({
                          ...mapping,
                          source: {
                            ...mapping.source,
                            null_values: [
                              '',
                              ...e.target.value
                                .split(',')
                                .map((v) => v.trim())
                                .filter(Boolean),
                            ],
                          },
                        })
                      }
                    />
                  </>
                )}
              </div>

              {mapping && (
                <table className="w-full border text-[11px]" aria-label="Column rules">
                  <thead className="bg-zinc-50 text-left">
                    <tr>
                      <th className="px-1">Column</th>
                      <th className="px-1">Use as</th>
                      <th className="px-1">Property</th>
                      <th className="px-1">Datatype / language / target</th>
                      <th className="px-1">Several values split by</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mapping.columns.map((rule, index) => (
                      <tr key={rule.column} className="border-t align-top">
                        <td className="px-1 py-0.5 font-medium">{rule.column}</td>
                        <td className="px-1 py-0.5">
                          <select
                            className="h-6 rounded border px-1"
                            aria-label={`Use of ${rule.column}`}
                            value={rule.kind}
                            onChange={(e) => {
                              const kind = e.target.value
                              const property = rule.kind === 'ignore' ? '' : rule.property
                              if (kind === 'ignore') updateRule(index, { column: rule.column, kind: 'ignore' })
                              else if (kind === 'value')
                                updateRule(index, {
                                  column: rule.column,
                                  kind: 'value',
                                  property,
                                  datatype: TABLE_DATATYPES[0],
                                })
                              else
                                updateRule(index, {
                                  column: rule.column,
                                  kind: 'reference',
                                  property,
                                  target: { template: `${props.baseIri}{${rule.column}}` },
                                })
                            }}>
                            <option value="ignore">ignore</option>
                            <option value="value">value</option>
                            <option value="reference">reference</option>
                          </select>
                        </td>
                        <td className="px-1 py-0.5">
                          {rule.kind !== 'ignore' && (
                            <NameInput
                              value={rule.property}
                              listId="table-import-properties"
                              prefixes={prefixes}
                              label={`Property of ${rule.column}`}
                              className="h-6 w-full rounded border px-1"
                              onChange={(iri) => updateRule(index, { ...rule, property: iri })}
                            />
                          )}
                        </td>
                        <td className="px-1 py-0.5">
                          {rule.kind === 'value' && (
                            <div className="flex items-center gap-1">
                              <select
                                className="h-6 rounded border px-1"
                                aria-label={`Datatype of ${rule.column}`}
                                value={rule.lang !== undefined ? 'lang' : (rule.datatype ?? TABLE_DATATYPES[0])}
                                onChange={(e) => {
                                  const { datatype: _d, lang: _l, ...rest } = rule
                                  if (e.target.value === 'lang') updateRule(index, { ...rest, lang: 'ja' })
                                  else updateRule(index, { ...rest, datatype: e.target.value })
                                }}>
                                {[
                                  ...TABLE_DATATYPES,
                                  ...(rule.datatype && !TABLE_DATATYPES.includes(rule.datatype) ? [rule.datatype] : []),
                                ].map((dt) => (
                                  <option key={dt} value={dt}>
                                    {compactIri(dt, prefixes)}
                                  </option>
                                ))}
                                <option value="lang">text with a language</option>
                              </select>
                              {rule.lang !== undefined && (
                                <input
                                  className="h-6 w-14 rounded border px-1"
                                  aria-label={`Language of ${rule.column}`}
                                  value={rule.lang}
                                  onChange={(e) => updateRule(index, { ...rule, lang: e.target.value.trim() })}
                                />
                              )}
                            </div>
                          )}
                          {rule.kind === 'reference' && (
                            <div className="space-y-1">
                              <select
                                className="h-6 rounded border px-1"
                                aria-label={`Target of ${rule.column}`}
                                value={'template' in rule.target ? 'template' : 'lookup'}
                                onChange={(e) =>
                                  updateRule(index, {
                                    ...rule,
                                    target:
                                      e.target.value === 'template'
                                        ? { template: `${props.baseIri}{${rule.column}}` }
                                        : { lookup: { class: '', property: RDFS_LABEL } },
                                  })
                                }>
                                <option value="template">IRI from a template</option>
                                <option value="lookup">an existing individual by a value</option>
                              </select>
                              {'template' in rule.target ? (
                                <input
                                  className="h-6 w-full rounded border px-1 font-mono"
                                  aria-label={`Target template of ${rule.column}`}
                                  value={rule.target.template}
                                  onChange={(e) => updateRule(index, { ...rule, target: { template: e.target.value } })}
                                />
                              ) : (
                                <div className="flex gap-1">
                                  <NameInput
                                    value={rule.target.lookup.class}
                                    listId="table-import-classes"
                                    prefixes={prefixes}
                                    label={`Lookup class of ${rule.column}`}
                                    className="h-6 w-1/2 rounded border px-1"
                                    onChange={(iri) =>
                                      'lookup' in rule.target &&
                                      updateRule(index, {
                                        ...rule,
                                        target: { lookup: { ...rule.target.lookup, class: iri } },
                                      })
                                    }
                                  />
                                  <NameInput
                                    value={rule.target.lookup.property}
                                    listId="table-import-properties"
                                    prefixes={prefixes}
                                    label={`Lookup property of ${rule.column}`}
                                    className="h-6 w-1/2 rounded border px-1"
                                    onChange={(iri) =>
                                      'lookup' in rule.target &&
                                      updateRule(index, {
                                        ...rule,
                                        target: { lookup: { ...rule.target.lookup, property: iri } },
                                      })
                                    }
                                  />
                                </div>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-1 py-0.5">
                          {rule.kind !== 'ignore' && (
                            <input
                              className="h-6 w-12 rounded border px-1"
                              aria-label={`Separator of ${rule.column}`}
                              placeholder="-"
                              value={rule.separator ?? ''}
                              onChange={(e) => {
                                const { separator: _s, ...rest } = rule
                                updateRule(index, e.target.value ? { ...rest, separator: e.target.value } : rest)
                              }}
                            />
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {problems.length > 0 && (
                <ul className="list-disc pl-4 text-amber-700">
                  {problems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              )}
            </section>
          )}

          {/* 3 Preview */}
          {mapping && inspection && (
            <section className="space-y-2" aria-label="3 Preview">
              <div className="flex items-center gap-2">
                <h3 className="font-semibold text-zinc-700">3. Preview</h3>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs"
                  disabled={!!busy || problems.length > 0 || props.dirty}
                  onClick={() => void runPreview()}>
                  Preview
                </Button>
              </div>
              {preview && (
                <div className="space-y-2 rounded border p-2" data-testid="table-preview">
                  <ul className="list-disc space-y-0.5 pl-4 text-zinc-700">
                    <li data-testid="table-accepted">
                      <b>{preview.rows.accepted}</b> of {preview.rows.total} row(s) accepted ({preview.subjects.new}{' '}
                      new, {preview.subjects.existing} existing individual(s)); <b>{preview.add.length}</b> statement(s)
                      to add
                      {preview.present > 0 && `, ${preview.present} already present`}
                    </li>
                    {preview.remove.length > 0 && (
                      <li className="text-amber-700">{preview.remove.length} existing value(s) replaced (removed)</li>
                    )}
                    <li data-testid="table-excluded" className={preview.rows.excluded ? 'text-red-700' : ''}>
                      <b>{preview.rows.excluded}</b> row(s) excluded
                    </li>
                    {preview.rows.with_warnings > 0 && (
                      <li className="text-amber-700">{preview.rows.with_warnings} accepted row(s) with warnings</li>
                    )}
                    <li data-testid="table-shacl">
                      {preview.shacl.checked
                        ? `SHACL checked${preview.shacl.preexisting ? ` (${preview.shacl.preexisting} violation(s) already in the ontology, not caused by the table)` : ''}`
                        : (preview.shacl.message ?? `SHACL not checked (${preview.shacl.reason})`)}
                    </li>
                  </ul>
                  {shaclBlocked && (
                    <label className="flex items-center gap-2 text-amber-800">
                      <input
                        type="checkbox"
                        checked={withoutCheck}
                        onChange={(e) => setWithoutCheck(e.target.checked)}
                      />
                      Import without SHACL check
                    </label>
                  )}
                  {preview.excluded.length > 0 && (
                    <>
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-zinc-700">Excluded rows</span>
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-6 text-[11px]"
                          onClick={() =>
                            download(
                              `${(file?.name ?? 'table').replace(/\.[^.]+$/, '')}-excluded.csv`,
                              excludedRowsCsv(preview),
                              'text/csv;charset=utf-8',
                            )
                          }>
                          <Download className="mr-1 h-3 w-3" /> Download excluded rows
                        </Button>
                      </div>
                      <div className="max-h-56 overflow-auto rounded border">
                        <table className="w-full text-[11px]" aria-label="Excluded rows">
                          <thead className="sticky top-0 bg-zinc-50 text-left">
                            <tr>
                              <th className="px-1">Line</th>
                              <th className="px-1">Key</th>
                              <th className="px-1">Stage</th>
                              <th className="px-1">Reason</th>
                            </tr>
                          </thead>
                          <tbody>
                            {preview.excluded.map((row) => (
                              <tr key={row.row} className="border-t align-top">
                                <td className="px-1">{row.line}</td>
                                <td className="px-1">{row.key}</td>
                                <td className="px-1">{STAGE_LABELS[row.stage]}</td>
                                <td className="px-1">
                                  {reasonText(row.reasons)}
                                  {row.depends_on.length > 0 && (
                                    <span className="text-zinc-500">
                                      {' '}
                                      (depends on row{row.depends_on.length > 1 ? 's' : ''} {row.depends_on.join(', ')})
                                    </span>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  )}
                  {preview.warnings.length > 0 && (
                    <div className="max-h-32 overflow-auto rounded border">
                      <table className="w-full text-[11px]" aria-label="Warnings">
                        <thead className="sticky top-0 bg-zinc-50 text-left">
                          <tr>
                            <th className="px-1">Line</th>
                            <th className="px-1">Key</th>
                            <th className="px-1">Warning (imported)</th>
                          </tr>
                        </thead>
                        <tbody>
                          {preview.warnings.map((row) => (
                            <tr key={row.row} className="border-t align-top">
                              <td className="px-1">{row.line}</td>
                              <td className="px-1">{row.key}</td>
                              <td className="px-1">{reasonText(row.reasons)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </section>
          )}

          {busy && (
            <p className="flex items-center text-zinc-500">
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> {busy}
            </p>
          )}
          {notice && <p className="text-zinc-600">{notice}</p>}
          {failed && <p className="text-red-600">{failed}</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={props.onClose} disabled={!!busy}>
            Cancel
          </Button>
          <Button onClick={() => preview && mapping && props.onApply(preview, mapping)} disabled={!canApply}>
            <TableProperties className="mr-1 h-3.5 w-3.5" /> Add {preview?.rows.accepted ?? 0} row(s)
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
