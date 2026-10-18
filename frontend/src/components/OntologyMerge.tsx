/**
 * Merge into the open ontology (PRP-0213, UDR-0195 D1-D6, CTR-0173 v17).
 *
 * Two sources: a file (parsed by `POST /api/ontology/{id}/parse`, which writes nothing)
 * or another catalog ontology (`GET /api/ontology/{other}`; its content is COPIED,
 * unlike owl:imports, which only references it). The dialog previews the merge -- new
 * and already-present statements, the graphs taken (one checkbox per graph of a
 * dataset: the partial merge), prefix conflicts, a VERSION change, the source's
 * ontology header (left out by default) -- and hands the plan to the editor, which
 * adds the statements as UNSAVED edits. Nothing is written until Save.
 */

import { FilePlus2, Loader2 } from 'lucide-react'
import { useMemo, useRef, useState } from 'react'
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
  defaultMergeOptions,
  type MergeOptions,
  type MergePlan,
  type MergeSource,
  planMerge,
  sourceGraphs,
} from '@/lib/ontologyMerge'
import { fromProjection, type GraphTerm, graphKey, graphsOf, type OntologyModel } from '@/lib/ontologyModel'

/** File types the upload (and so the merge) accepts (UDR-0182 D6). */
const MERGE_ACCEPT = '.ttl,.turtle,.trig,.nt,.nq,.rdf,.owl,.xml,.jsonld,.json'

export interface MergeCatalogEntry {
  id: string
  name: string
}

async function errorMessage(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null)
  const detail = body?.detail
  if (typeof detail === 'string') return detail
  if (typeof detail?.message === 'string') return detail.message
  return fallback
}

export function MergeDialog(props: {
  ontologyId: string
  ontologyName: string
  model: OntologyModel
  catalog: MergeCatalogEntry[]
  /** The target's declared SHACL shapes graphs (PRP-0212). */
  shapesGraphs: string[]
  graphName: (g: GraphTerm) => string
  onClose: () => void
  onMerge: (plan: MergePlan, source: MergeSource) => void
}) {
  const [mode, setMode] = useState<'file' | 'ontology'>('file')
  const [otherId, setOtherId] = useState('')
  const [source, setSource] = useState<MergeSource | null>(null)
  const [options, setOptions] = useState<MergeOptions | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const others = props.catalog.filter((entry) => entry.id !== props.ontologyId)

  const accept = (loaded: MergeSource) => {
    setSource(loaded)
    setOptions(defaultMergeOptions(props.model, loaded))
  }

  const readFile = async (file: File) => {
    setLoading(true)
    setFailed(null)
    setSource(null)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`/api/ontology/${props.ontologyId}/parse`, { method: 'POST', body: form })
      if (!res.ok) throw new Error(await errorMessage(res, 'The file could not be read'))
      const body = await res.json()
      accept({ model: fromProjection(body), dataset: body.dataset === true, label: file.name })
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'The file could not be read')
    } finally {
      setLoading(false)
    }
  }

  const readOntology = async (id: string) => {
    setOtherId(id)
    if (!id) return
    setLoading(true)
    setFailed(null)
    setSource(null)
    try {
      const res = await fetch(`/api/ontology/${id}`)
      if (!res.ok) throw new Error(await errorMessage(res, 'The ontology could not be read'))
      const body = await res.json()
      const model = fromProjection(body)
      const dataset = graphsOf(model).length > 0
      accept({ model, dataset, label: others.find((entry) => entry.id === id)?.name ?? id })
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'The ontology could not be read')
    } finally {
      setLoading(false)
    }
  }

  const graphs = useMemo(() => (source ? sourceGraphs(source) : []), [source])
  const plan = useMemo(
    () => (source && options ? planMerge(props.model, source, options, props.shapesGraphs) : null),
    [source, options, props.model, props.shapesGraphs],
  )
  const targetGraphs = graphsOf(props.model)
  const sourceGraphName = (g: GraphTerm | null) => (g ? props.graphName(g) : 'default graph')

  return (
    <Dialog open onOpenChange={(o) => !o && !loading && props.onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Merge into {props.ontologyName}</DialogTitle>
          <DialogDescription>
            Copy the statements of a file or of another ontology into this one. They are added as unsaved changes: check
            them, then Save. (owl:imports, under Imports, only references another ontology.)
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-xs">
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="merge-source"
                checked={mode === 'file'}
                onChange={() => {
                  setMode('file')
                  setSource(null)
                }}
              />
              File
            </label>
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="merge-source"
                checked={mode === 'ontology'}
                disabled={others.length === 0}
                onChange={() => {
                  setMode('ontology')
                  setSource(null)
                }}
              />
              Another ontology
            </label>
          </div>
          {mode === 'file' ? (
            <div className="flex items-center gap-2">
              <input
                ref={fileRef}
                type="file"
                accept={MERGE_ACCEPT}
                className="hidden"
                aria-label="File to merge"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = ''
                  if (file) void readFile(file)
                }}
              />
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                disabled={loading}
                onClick={() => fileRef.current?.click()}>
                Choose a file...
              </Button>
              <span className="text-zinc-500">Turtle, TriG, N-Triples, N-Quads, RDF/XML, JSON-LD</span>
            </div>
          ) : (
            <select
              className="h-7 w-full rounded border px-1 text-xs"
              aria-label="Ontology to merge"
              value={otherId}
              disabled={loading}
              onChange={(e) => void readOntology(e.target.value)}>
              <option value="">Choose an ontology...</option>
              {others.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.name}
                </option>
              ))}
            </select>
          )}
          {loading && (
            <p className="flex items-center text-zinc-500">
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> Reading...
            </p>
          )}
          {failed && <p className="text-red-600">{failed}</p>}
          {source && options && plan && (
            <div className="space-y-2 rounded border p-2" data-testid="merge-preview">
              <div className="font-medium text-zinc-700">{source.label}</div>
              {source.dataset ? (
                <fieldset className="space-y-1">
                  <legend className="mb-1 text-zinc-600">Graphs to merge</legend>
                  {graphs.map((row) => (
                    <label key={row.key} className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={options.graphs.has(row.key)}
                        onChange={(e) => {
                          const next = new Set(options.graphs)
                          if (e.target.checked) next.add(row.key)
                          else next.delete(row.key)
                          setOptions({ ...options, graphs: next })
                        }}
                      />
                      <span className="truncate">{sourceGraphName(row.graph)}</span>
                      <span className="text-zinc-400">({row.count})</span>
                    </label>
                  ))}
                </fieldset>
              ) : (
                <label className="flex items-center gap-2">
                  <span className="text-zinc-600">Into graph</span>
                  <select
                    className="h-7 rounded border px-1 text-xs"
                    aria-label="Graph to merge into"
                    value={graphKey(options.target)}
                    onChange={(e) =>
                      setOptions({
                        ...options,
                        target: targetGraphs.find((g) => graphKey(g) === e.target.value) ?? null,
                      })
                    }>
                    <option value="">default graph</option>
                    {targetGraphs.map((g) => (
                      <option key={graphKey(g)} value={graphKey(g)}>
                        {props.graphName(g)}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={options.includeHeader}
                  onChange={(e) => setOptions({ ...options, includeHeader: e.target.checked })}
                />
                Include the source's ontology header (owl:Ontology, its owl:imports and annotations)
              </label>
              <ul className="list-disc space-y-0.5 pl-4 text-zinc-700">
                <li data-testid="merge-new">
                  <b>{plan.statements.length}</b> new statement{plan.statements.length === 1 ? '' : 's'}
                </li>
                <li>{plan.present} already in this ontology (unchanged)</li>
                {plan.graphSkipped > 0 && <li>{plan.graphSkipped} in graphs left out</li>}
                {plan.headerSkipped > 0 && <li>{plan.headerSkipped} ontology header statements left out</li>}
                {plan.newGraphs.length > 0 && (
                  <li>New graphs: {plan.newGraphs.map((g) => props.graphName(g)).join(', ')}</li>
                )}
                {plan.prefixesAdded.length > 0 && (
                  <li>Prefixes added: {plan.prefixesAdded.map((p) => `${p.prefix}:`).join(' ')}</li>
                )}
                {plan.prefixesSkipped.map((p) => (
                  <li key={p.prefix} className="text-amber-700">
                    Prefix {p.prefix}: kept as &lt;{p.existing}&gt;; the source's &lt;{p.iri}&gt; is shown in full
                  </li>
                ))}
                {plan.version && <li className="text-amber-700">The ontology becomes VERSION {plan.version}</li>}
                {plan.shapesGraphs.length > 0 && (
                  <li className="text-amber-700">
                    Merged into SHACL shapes graphs: {plan.shapesGraphs.map((g) => props.graphName(g)).join(', ')}
                  </li>
                )}
              </ul>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={props.onClose} disabled={loading}>
            Cancel
          </Button>
          <Button
            onClick={() => source && plan && props.onMerge(plan, source)}
            disabled={loading || !plan || plan.statements.length === 0}>
            <FilePlus2 className="mr-1 h-3.5 w-3.5" /> Merge
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
