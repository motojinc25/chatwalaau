/**
 * Ontology Manager (CTR-0173, PRP-0105, FEAT-0058 / UDR-0084).
 *
 * Full-screen portal on the File Explorer recipe (CTR-0137): three freely
 * resizable panes -- LEFT = the ontology catalog (add / import / export /
 * delete-with-confirmation), CENTER = the React Flow design canvas (Entity
 * nodes with emoji, directional cardinality edges, pan / zoom / fit / reset
 * layout via elkjs / PNG download), RIGHT = a tabbed Detail / Search pane
 * (monaco SPARQL editor + natural-language search via /nl-query; SELECT
 * results render as a table and drive the strong/dim canvas highlight).
 *
 * The frontend never parses RDF (UDR-0084 D6): it edits the CTR-0169 v3
 * statement-complete projection served by CTR-0171 -- every quad is a typed
 * statement of its subject, carrying its named graph as `g` (PRP-0198 / PRP-0200,
 * UDR-0180 / UDR-0182). A graph selector scopes the canvas and the search and is
 * the target graph of new statements; exports offer six formats and show why a
 * format cannot carry the ontology; any statement can be annotated (a reifier
 * with an IRI). `lib/ontologyModel.ts` maps
 * statements to the canvas and canvas edits to statement edits; the inspector
 * (`OntologyInspector.tsx`) shows and edits every statement, the resources the
 * canvas does not draw (Resources tab) and the prefixes / base / VERSION
 * (Document). Saving sends the full state with the loaded revision (409 when it
 * went stale) and is backup-then-atomic server-side; closing (or switching
 * ontologies) with unsaved changes asks for confirmation first.
 */

import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import Editor from '@monaco-editor/react'
import type { Connection, Edge, EdgeProps, Node, NodeChange, NodeProps } from '@xyflow/react'
import {
  applyNodeChanges,
  Background,
  BaseEdge,
  ConnectionMode,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useInternalNode,
  useNodesInitialized,
  useReactFlow,
} from '@xyflow/react'
import {
  ArchiveRestore,
  Braces,
  Download,
  FilePlus2,
  GripVertical,
  History,
  ImageDown,
  Import,
  Info,
  KeyRound,
  Layers,
  LayoutGrid,
  Lightbulb,
  ListChecks,
  Loader2,
  Maximize,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  SendHorizontal,
  ShieldCheck,
  Square,
  Trash2,
  TriangleAlert,
  Upload,
  Users,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { findingId, OntologyChecksPane, useIgnoredChecks } from '@/components/OntologyChecks'
import { type DataGroup, IndividualDetail, IndividualsView, individualLabel } from '@/components/OntologyDataView'
import { ExplainPane, type ExplainRequest, type ExplainResult } from '@/components/OntologyExplain'
import { DeletedOntologiesDialog, HistoryPanel } from '@/components/OntologyHistory'
import {
  ImportedDetail,
  ImportsDialog,
  type ImportsTab,
  importWarningCount,
  type ModelImport,
} from '@/components/OntologyImports'
import {
  AllStatements,
  AnnotateDialog,
  DocumentDialog,
  fieldInput,
  fieldLabel,
  graphLabel,
  type InspectorContext,
  NewResourceDialog,
  ResourceDetail,
  ResourcesPane,
  TermView,
} from '@/components/OntologyInspector'
import { NewOntologyIriFields, newOntologyIriReady, OntologyIrisBlock } from '@/components/OntologyIri'
import { MergeDialog } from '@/components/OntologyMerge'
import { ShapesView } from '@/components/OntologyShapes'
import { TableImportDialog } from '@/components/OntologyTableImport'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { BusyOverlay } from '@/components/ui/busy-overlay'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useEntryVisible } from '@/lib/narrowSurface'
import {
  type InferredMode,
  type InferredStatement,
  type InferredStatements,
  inferredCanvas,
  inferredData,
  readInferredMode,
  writeInferredMode,
} from '@/lib/ontologyInferred'
import { type CatalogNamespace, DEFAULT_IRI_BASE, irisOf, namespaceRefusal, type OntologyIris } from '@/lib/ontologyIri'
import { applyMerge, type MergePlan } from '@/lib/ontologyMerge'
import {
  addImport,
  addSubclassOf,
  annotateStatement,
  applyStatementEdit,
  assertedElsewhere,
  CARDINALITIES,
  CHARACTERISTICS,
  type Characteristic,
  type CheckEdge,
  CW_CARDINALITY,
  CW_COLOR,
  CW_EMOJI,
  characteristicGraphs,
  characteristicNotices,
  classHierarchy,
  classTree,
  compactIri,
  createDatatypeProperty,
  createEntity,
  createIndividual,
  createRelationship,
  type Diagnostic,
  declareClass,
  deleteEntity as deleteEntityFromModel,
  deleteIndividual,
  diffStatements,
  displayedLiteralEdit,
  displayLiteral,
  documentKey,
  EMPTY_MODEL,
  type EntityView,
  entityViews,
  externalReferenceCount,
  type Finding,
  findResource,
  firstObjectEdit,
  fromProjection,
  type GraphScope,
  type GraphTerm,
  graphsOf,
  type ImportedClassView,
  type ImportsInfo,
  type IndividualReferences,
  importedClassViews,
  importedIndex,
  importedOptions,
  individualIndex,
  individualReferences,
  iriObjects,
  iri as iriTerm,
  localName,
  mintReifierIri,
  type OntologyDocument,
  type OntologyModel,
  OWL_IMPORTS,
  OWL_ONTOLOGY,
  otherLiterals,
  propertyCharacteristics,
  type QuadStatement,
  RDF_TYPE,
  RDFS_COMMENT,
  RDFS_DOMAIN,
  RDFS_LABEL,
  RDFS_RANGE,
  RDFS_SUB_PROPERTY_OF,
  type RelationshipEdgeView,
  type Resource,
  referencedImportedClasses,
  reifiersOf,
  relationshipEdges,
  removeCheckEdge,
  removeImport,
  removePropertyFromEntity,
  removeResource,
  removeStatement,
  removeSubclassOf,
  renameBlankNodes,
  runChecks,
  type SaveBase,
  type StatementEdit,
  type StatementInput,
  saveBase,
  scopeModel,
  setCharacteristic,
  setIsKey,
  setPosition,
  subclassEdges,
  type Term,
  termKey,
  toPayload,
  undeclaredClasses,
  type VocabularyStyle,
  vocabularyStyle,
  vocabularyStyleReason,
  withoutGraphs,
  XSD,
} from '@/lib/ontologyModel'
import { hasShapes, type ShapesGraph, targetedClasses } from '@/lib/ontologyShapes'
import { applyTableImport, type TableMapping, type TablePreview } from '@/lib/ontologyTable'
import { chooseTabLevel, showsLabel, type TabLevel } from '@/lib/panelTabs'
import { cn } from '@/lib/utils'
import '@/lib/monaco-setup'
import '@xyflow/react/dist/style.css'

/** A statement a rebased save could not apply (409 stale_conflict; UDR-0183 D8). */
interface SaveConflict {
  op: string
  statement: QuadStatement
}

interface CatalogEntry {
  id: string
  name: string
  description: string
  updated_at?: string
  /** PRP-0212 (UDR-0194 D1): the SHACL validation settings. */
  shacl?: ShaclSettings
  /** PRP-0218 (UDR-0200 D2): the namespace stored when the ontology was created. */
  namespace?: CatalogNamespace
}

/** The catalog's SHACL block (CTR-0171 v12). */
interface ShaclSettings {
  shapes_graphs: string[]
  shapes_ontologies: string[]
  inference: 'none' | 'rdfs' | 'owl-rl'
  strict: boolean
}

type ShaclSeverity = 'violation' | 'warning' | 'info'

/** One (shape, path, constraint) group of results (GET /{id}/shacl, UDR-0194 D9). */
interface ShaclGroup {
  key: string
  severity: ShaclSeverity
  shape: { nt: string; shown: string } | null
  path: string | null
  component: string
  constraint: string | null
  message: string
  count: number
  focus: { nt: string; shown: string; term: Term; imported: boolean }[]
  focus_count: number
}

/** One stored result (GET /{id}/shacl/results; also the strict-mode refusal). */
interface ShaclResultItem {
  key: string
  severity: ShaclSeverity
  focus: { nt: string; shown: string; term: Term }
  imported?: boolean
  path: string | null
  value: { nt: string; shown: string } | null
  shape: { nt: string; shown: string } | null
  component: string
  constraint: string | null
  message: string
}

interface ShaclStatus {
  configured: boolean
  settings: ShaclSettings | null
  state: string
  revision: string | null
  inference: string
  strict: boolean
  shapes: { kind: 'graph' | 'ontology'; ref: string; revision: string | null; missing: boolean; name?: string | null }[]
  conforms: boolean | null
  counts: Record<ShaclSeverity, number> | null
  unsupported: { construct: string; shape: string; count: number }[]
  groups: ShaclGroup[]
  focus_counts: Record<string, Record<ShaclSeverity, number>>
  results_count: number
  truncated: boolean
  elapsed_ms: number | null
  message: string | null
  detail: { errors?: string[] } | null
  precompute: boolean
}

/** A query notice: what the query engine could not give back as written (UDR-0181 D3). */
interface QueryNotice {
  code: string
  message: string
  /** `entailment_inconsistent` (CTR-0171 v10, UDR-0192 D5): the total and the first 20 findings. */
  count?: number
  truncated?: boolean
  items?: InconsistencyItem[]
}

/** One inconsistency OWL 2 RL found (graph null = the merge of the ontology's graphs). */
interface InconsistencyItem {
  kind: string
  graph: string | null
  message: string
  terms: string[]
}

/** A typed SELECT cell; `lexical_forms` lists the file's spellings when it is ambiguous. */
type QueryCell = (Term & { lexical_forms?: string[] }) | null

/**
 * The search entailment modes (PRP-0209 / UDR-0191 D1, PRP-0210 / UDR-0192 D1). `simple`
 * answers from the stated statements only; `owl-rl` includes everything `rdfs` gives.
 */
type Entailment = 'simple' | 'rdfs' | 'owl-rl'
const ENTAILMENT_OPTIONS: { value: Entailment; label: string; title: string }[] = [
  { value: 'simple', label: 'None', title: 'Only the statements written in the ontology files.' },
  {
    value: 'rdfs',
    label: 'RDFS',
    title:
      'Also what RDFS entails: subclass and subproperty hierarchies, domains and ranges. Inferred results are marked and never written to the file.',
  },
  {
    value: 'owl-rl',
    label: 'OWL 2 RL',
    title:
      'Also what OWL 2 RL entails: transitive, symmetric and inverse properties, equivalences, owl:sameAs, restrictions and keys, on top of RDFS. Slower; limited to smaller ontologies. Inconsistencies are reported.',
  },
]

/** How an answer names the entailment that produced its inferred part. */
const ENTAILMENT_LABELS: Record<Entailment, string> = {
  simple: 'No entailment',
  rdfs: 'RDFS entailment',
  'owl-rl': 'OWL 2 RL entailment',
}

/** The state of the stored entailment of one mode (PRP-0211, CTR-0171 v11, UDR-0193 D12). */
interface EntailmentModeStatus {
  state: 'missing' | 'queued' | 'running' | 'ready' | 'too_large' | 'failed' | 'timeout'
  label: string
  stated?: number | null
  inferred?: number | null
  elapsed_ms?: number | null
  running_seconds?: number
  findings_count?: number
  message?: string | null
}

interface EntailmentStatus {
  revision: string
  precompute: boolean
  modes: Partial<Record<'rdfs' | 'owl-rl', EntailmentModeStatus>>
}

/** A short text for an entailment state (the Search pane and the Inferred switch). */
function entailmentStateText(status: EntailmentModeStatus | undefined): string | null {
  if (!status) return null
  switch (status.state) {
    case 'ready':
      return 'Ready'
    case 'queued':
      return 'Computing (queued)...'
    case 'running':
      return `Computing... ${Math.round(status.running_seconds ?? 0)} s`
    case 'too_large':
      return status.message ?? 'Too large'
    case 'timeout':
      return status.message ?? 'Did not finish in time'
    case 'failed':
      return status.message ?? 'Could not be computed'
    default:
      return 'Computed at the next search'
  }
}

/** Watches React Flow's node measuring (inside the provider): true once every node is measured. */
function NodesReady({ onChange }: { onChange: (ready: boolean) => void }) {
  const initialized = useNodesInitialized()
  useEffect(() => onChange(initialized), [initialized, onChange])
  return null
}

/** The "Inferred: Off | RDFS | OWL 2 RL" switch of the canvas and the Data view (UDR-0193 D17). */
function InferredSwitch(props: {
  mode: InferredMode
  onChange: (mode: InferredMode) => void
  disabled: boolean
  note: string | null
}) {
  return (
    <label
      className="flex items-center gap-1 text-[11px] text-zinc-600"
      title="Show what RDFS / OWL 2 RL infer from the saved ontology, faintly. Inferred items are never edited or saved; click one to see why. Remembered in this browser.">
      Inferred
      <select
        aria-label="Inferred"
        className="h-6 rounded border bg-white px-1 text-[11px]"
        value={props.mode}
        disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.value as InferredMode)}>
        <option value="off">Off</option>
        <option value="rdfs">RDFS</option>
        <option value="owl-rl">OWL 2 RL</option>
      </select>
      {props.note && (
        <span className="max-w-[220px] truncate text-[10px] text-zinc-500" data-inferred-note title={props.note}>
          {props.note}
        </span>
      )}
    </label>
  )
}

/** The canvas legend shown with inferred edges (UDR-0193 D18). */
function InferredLegend() {
  const row = (dash: string | undefined, opacity: number, text: string, open = false) => (
    <div className="flex items-center gap-1.5">
      <svg width="28" height="8" aria-hidden="true">
        <line
          x1="0"
          y1="4"
          x2="28"
          y2="4"
          stroke="#64748b"
          strokeWidth="1.5"
          strokeDasharray={dash}
          opacity={opacity}
        />
        {!open && <polygon points="22,1 28,4 22,7" fill="#64748b" opacity={opacity} />}
      </svg>
      <span>{text}</span>
    </div>
  )
  return (
    <section
      aria-label="Legend"
      className="pointer-events-none absolute bottom-2 left-2 z-[5] space-y-0.5 rounded border bg-white/90 p-1.5 text-[10px] text-zinc-600 shadow-sm">
      {row(undefined, 1, 'relationship')}
      {row('6 4', 1, 'is a', true)}
      {row('2 4', 0.5, 'inferred (click: why)')}
      {row('2 4', 0.5, 'derived from owl:inverseOf')}
    </section>
  )
}

type QueryResult =
  | {
      kind: 'select'
      columns: string[]
      rows: string[][]
      /** CTR-0171 v4: typed values parallel to `rows` (absent from older servers). */
      cells?: QueryCell[][]
      row_count: number
      truncated: boolean
      entity_iris: string[]
      notices?: QueryNotice[]
      entailment?: Entailment
      /** CTR-0171 v9: one flag per row, true when the row exists only through inference. */
      row_inferred?: boolean[]
      inferred_count?: number
    }
  | {
      kind: 'construct'
      /** CTR-0171 v5: "trig" for an ontology with named graphs (each triple under its graphs). */
      format?: 'turtle' | 'trig'
      /** With an entailment mode: the stated part only (CTR-0171 v9). */
      turtle: string
      triple_count: number
      truncated: boolean
      notices?: QueryNotice[]
      entailment?: Entailment
      /** CTR-0171 v9: the inferred triples (Turtle, or TriG for a dataset). */
      inferred_turtle?: string
      inferred_count?: number
      /** CTR-0171 v9: the same triples as table rows (graph null = the default graph). */
      triples?: { s: Term; p: Term; o: Term; graph: Term | null; inferred: boolean }[]
      triples_truncated?: boolean
    }
  | { kind: 'ask'; value: boolean; notices?: QueryNotice[]; entailment?: Entailment; inferred?: boolean }
  | { kind: 'error'; error: string }

/** The message of an error response: a string `detail`, or `detail.message` (CTR-0171 v9 422). */
function errorDetail(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && typeof (detail as { message?: unknown }).message === 'string') {
    return (detail as { message: string }).message
  }
  return fallback
}

/**
 * What the Detail pane shows: an entity or a relationship drawn on the canvas
 * (by IRI), or any resource by its term key (inspector, UDR-0180 D9).
 */
type Selection =
  | { kind: 'entity' | 'relationship'; iri: string }
  | { kind: 'resource'; key: string }
  | { kind: 'isa'; source: string; target: string }
  | { kind: 'individual'; key: string }
  | { kind: 'imported'; iri: string }

type LinkMode = 'relationship' | 'isa'

// Per-browser view preference, never saved in the ontology (UDR-0185 D3).
const SHOW_ISA_STORAGE_KEY = 'chatwalaau.ontology.showIsa'

function readShowIsa(): boolean {
  try {
    return localStorage.getItem(SHOW_ISA_STORAGE_KEY) !== 'false'
  } catch {
    return true
  }
}

function writeShowIsa(show: boolean): void {
  try {
    localStorage.setItem(SHOW_ISA_STORAGE_KEY, String(show))
  } catch {
    // storage blocked: the choice lasts for this session only
  }
}

// Per-browser: draw the imported classes the ontology refers to (UDR-0189 D8).
const SHOW_IMPORTED_STORAGE_KEY = 'chatwalaau.ontology.showImported'

function readShowImported(): boolean {
  try {
    return localStorage.getItem(SHOW_IMPORTED_STORAGE_KEY) !== 'false'
  } catch {
    return true
  }
}

function writeShowImported(show: boolean): void {
  try {
    localStorage.setItem(SHOW_IMPORTED_STORAGE_KEY, String(show))
  } catch {
    // storage blocked: the choice lasts for this session only
  }
}

const NO_IRIS: Set<string> = new Set()

/** Schema = the canvas (TBox); Data = individuals per class (ABox, UDR-0187 D2);
 * Shapes = SHACL node shapes as tables (PRP-0212, UDR-0194 D10). */
type ManagerView = 'schema' | 'data' | 'shapes'

// Per-browser view preference, never saved in the ontology (UDR-0187 D2).
const VIEW_STORAGE_KEY = 'chatwalaau.ontology.view'

function readView(): ManagerView {
  try {
    const stored = localStorage.getItem(VIEW_STORAGE_KEY)
    return stored === 'data' || stored === 'shapes' ? stored : 'schema'
  } catch {
    return 'schema'
  }
}

function writeView(view: ManagerView): void {
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, view)
  } catch {
    // storage blocked: the choice lasts for this session only
  }
}

/** The Schema | Data (| Shapes) switch, shown in every view's toolbar. */
function ViewSwitch(props: {
  view: ManagerView
  onChange: (view: ManagerView) => void
  disabled: boolean
  /** The ontology has shapes or declares shapes sources (PRP-0212, UDR-0194 D10). */
  shapes?: boolean
}) {
  const values: ManagerView[] = props.shapes ? ['schema', 'data', 'shapes'] : ['schema', 'data']
  return (
    <fieldset className="m-0 flex shrink-0 overflow-hidden rounded-md border p-0 text-xs">
      <legend className="sr-only">View</legend>
      {values.map((value) => (
        <button
          key={value}
          type="button"
          aria-pressed={props.view === value}
          disabled={props.disabled}
          title={
            value === 'schema'
              ? 'Schema: classes, properties and "is a" on the canvas'
              : value === 'data'
                ? 'Data: the individuals of each class, as a table'
                : 'Shapes: the SHACL shapes the data is validated against, as tables'
          }
          className={cn(
            'h-7 px-2 font-medium',
            props.view === value ? 'bg-zinc-800 text-white' : 'bg-white text-zinc-600 hover:bg-zinc-100',
          )}
          onClick={() => props.onChange(value)}>
          {value === 'schema' ? 'Schema' : value === 'data' ? 'Data' : 'Shapes'}
        </button>
      ))}
    </fieldset>
  )
}

// ---- SHACL validation dialogs (PRP-0212, UDR-0194) --------------------------------------

function ShaclResultsTable(props: { items: ShaclResultItem[]; onSelect: (term: Term) => void }) {
  if (props.items.length === 0) return <p className="text-xs text-zinc-500">No results.</p>
  return (
    <div className="max-h-[50vh] overflow-y-auto rounded border">
      <table className="w-full table-fixed border-collapse text-[11px]">
        <thead>
          <tr className="bg-zinc-50 text-left text-[10px] uppercase tracking-wide text-zinc-500">
            <th className="w-[18%] px-2 py-1 font-medium">node</th>
            <th className="w-[16%] px-2 py-1 font-medium">path</th>
            <th className="w-[16%] px-2 py-1 font-medium">value</th>
            <th className="w-[40%] px-2 py-1 font-medium">message</th>
            <th className="w-[10%] px-2 py-1 font-medium">severity</th>
          </tr>
        </thead>
        <tbody>
          {props.items.map((item) => (
            <tr key={item.key} className="border-t align-top">
              <td className="px-2 py-1">
                <button
                  type="button"
                  className="max-w-full truncate text-left text-blue-700 hover:underline"
                  title={item.focus.nt}
                  onClick={() => props.onSelect(item.focus.term)}>
                  {item.focus.shown}
                </button>
              </td>
              <td className="truncate px-2 py-1 font-mono text-[10px]" title={item.path ?? ''}>
                {item.path ?? '(node)'}
              </td>
              <td className="truncate px-2 py-1 font-mono text-[10px]" title={item.value?.shown ?? ''}>
                {item.value?.shown ?? ''}
              </td>
              <td className="px-2 py-1">
                {item.message}
                {item.constraint && <span className="ml-1 text-zinc-400">({item.constraint})</span>}
              </td>
              <td className="px-2 py-1">{item.severity}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ShaclResultsDialog(props: {
  ontologyId: string
  title: string
  query: Record<string, string>
  stale: boolean
  onClose: () => void
  onSelect: (term: Term) => void
}) {
  const [items, setItems] = useState<ShaclResultItem[] | null>(null)
  const [total, setTotal] = useState(0)
  const [failed, setFailed] = useState<string | null>(null)
  const queryKey = JSON.stringify(props.query)
  useEffect(() => {
    let cancelled = false
    const params = new URLSearchParams({ ...(JSON.parse(queryKey) as Record<string, string>), limit: '500' })
    void (async () => {
      try {
        const res = await fetch(`/api/ontology/${props.ontologyId}/shacl/results?${params}`)
        if (!res.ok) throw new Error('Could not load the results')
        const body = await res.json()
        if (cancelled) return
        setItems((body.items ?? []) as ShaclResultItem[])
        setTotal(Number(body.total ?? 0))
      } catch (err) {
        if (!cancelled) setFailed(err instanceof Error ? err.message : 'Could not load the results')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [props.ontologyId, queryKey])
  return (
    <Dialog open onOpenChange={(o) => !o && props.onClose()}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{props.title}</DialogTitle>
          <DialogDescription>
            SHACL results of the last saved version{props.stale ? ' (you have unsaved changes)' : ''}.
          </DialogDescription>
        </DialogHeader>
        {failed && <p className="text-xs text-red-600">{failed}</p>}
        {items === null && !failed ? (
          <p className="flex items-center text-xs text-zinc-500">
            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> Loading...
          </p>
        ) : (
          items && <ShaclResultsTable items={items} onSelect={props.onSelect} />
        )}
        {items && total > items.length && (
          <p className="text-xs text-zinc-500">
            Showing {items.length} of {total} results.
          </p>
        )}
      </DialogContent>
    </Dialog>
  )
}

function ShaclSettingsDialog(props: {
  ontologyId: string
  settings: ShaclSettings | null
  catalog: CatalogEntry[]
  graphs: GraphTerm[]
  graphName: (g: GraphTerm) => string
  status: string
  readOnly: boolean
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const [graphs, setGraphs] = useState<string[]>(props.settings?.shapes_graphs ?? [])
  const [ontologies, setOntologies] = useState<string[]>(props.settings?.shapes_ontologies ?? [])
  const [inference, setInference] = useState<ShaclSettings['inference']>(props.settings?.inference ?? 'none')
  const [strict, setStrict] = useState(props.settings?.strict ?? false)
  const [newGraph, setNewGraph] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const graphOptions = [
    ...new Set([
      ...props.graphs.filter((g) => g.type === 'iri').map((g) => g.value),
      ...(props.settings?.shapes_graphs ?? []),
      ...graphs,
    ]),
  ]
  const toggle = (list: string[], value: string, on: boolean) =>
    on ? [...list.filter((v) => v !== value), value] : list.filter((v) => v !== value)
  const send = async (shacl: ShaclSettings | null) => {
    setBusy(true)
    setFailed(null)
    try {
      const res = await fetch(`/api/ontology/catalog/${props.ontologyId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ shacl }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail?.message ?? 'Could not save the validation settings')
      }
      await props.onSaved()
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'Could not save the validation settings')
    } finally {
      setBusy(false)
    }
  }
  const addGraph = () => {
    const value = newGraph.trim().replace(/^<|>$/g, '')
    if (!/^[A-Za-z][A-Za-z0-9+.-]*:\S+$/.test(value)) {
      setFailed('A shapes graph is named by a full IRI, e.g. https://example.org/g/shapes')
      return
    }
    setGraphs((list) => toggle(list, value, true))
    setNewGraph('')
    setFailed(null)
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !busy && props.onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Validation (SHACL)</DialogTitle>
          <DialogDescription>
            Validate this ontology's data against SHACL shapes after every save. Shapes are kept in named graphs of this
            ontology or in other ontologies; they are never part of the data. {props.status}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-xs">
          <fieldset className="space-y-1">
            <legend className="mb-1 font-medium text-zinc-700">Shapes graphs of this ontology</legend>
            {graphOptions.length === 0 && <p className="text-zinc-500">No named graph yet: add one below.</p>}
            {graphOptions.map((value) => (
              <label key={value} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={graphs.includes(value)}
                  disabled={props.readOnly}
                  onChange={(e) => setGraphs((list) => toggle(list, value, e.target.checked))}
                />
                <span className="truncate" title={value}>
                  {props.graphName({ type: 'iri', value })}
                </span>
              </label>
            ))}
            <div className="flex items-center gap-1">
              <Input
                className="h-7 text-xs"
                placeholder="New shapes graph IRI, e.g. https://example.org/g/shapes"
                aria-label="New shapes graph IRI"
                value={newGraph}
                disabled={props.readOnly}
                onChange={(e) => setNewGraph(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && addGraph()}
              />
              <Button variant="outline" size="sm" className="h-7 text-xs" onClick={addGraph} disabled={props.readOnly}>
                Add
              </Button>
            </div>
          </fieldset>
          <fieldset className="space-y-1">
            <legend className="mb-1 font-medium text-zinc-700">Shapes ontologies</legend>
            <div className="max-h-32 space-y-1 overflow-y-auto">
              {props.catalog
                .filter((entry) => entry.id !== props.ontologyId)
                .map((entry) => (
                  <label key={entry.id} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={ontologies.includes(entry.id)}
                      disabled={props.readOnly || (!ontologies.includes(entry.id) && ontologies.length >= 10)}
                      onChange={(e) => setOntologies((list) => toggle(list, entry.id, e.target.checked))}
                    />
                    <span className="truncate">{entry.name}</span>
                  </label>
                ))}
            </div>
          </fieldset>
          <label className="flex items-center gap-2">
            <span className="font-medium text-zinc-700">Inference before validation</span>
            <select
              className="h-7 rounded border px-1 text-xs"
              value={inference}
              disabled={props.readOnly}
              onChange={(e) => setInference(e.target.value as ShaclSettings['inference'])}>
              <option value="none">None (stated statements)</option>
              <option value="rdfs">RDFS</option>
              <option value="owl-rl">OWL 2 RL</option>
            </select>
          </label>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={strict}
              disabled={props.readOnly}
              onChange={(e) => setStrict(e.target.checked)}
            />
            <span>
              <span className="font-medium text-zinc-700">Strict mode</span> -- a save with SHACL violations is refused
              (you can still save anyway). File imports and restores are never refused.
            </span>
          </label>
          {failed && <p className="text-red-600">{failed}</p>}
        </div>
        <DialogFooter className="gap-2 sm:justify-between">
          <Button
            variant="ghost"
            onClick={() => void send(null)}
            disabled={busy || props.readOnly || !props.settings}
            title="Remove the validation settings of this ontology">
            Turn off
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" onClick={props.onClose} disabled={busy}>
              Cancel
            </Button>
            <Button
              onClick={() => void send({ shapes_graphs: graphs, shapes_ontologies: ontologies, inference, strict })}
              disabled={busy || props.readOnly}>
              {busy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
              Save settings
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** An edit waiting for the "follow the reifiers?" answer (PRP-0198 Q4). */
interface PendingReifiedEdit {
  key: string
  edits: StatementEdit[]
  reifierCount: number
}

/** A statement whose deletion waits for "delete its annotations too?" (PRP-0200 Q3). */
interface PendingAnnotatedDelete {
  key: string
  index: number
  reifierCount: number
}

/** The export formats the server offers (CTR-0171 v5, UDR-0182 D7). */
const EXPORT_FORMATS = [
  { name: 'turtle', label: 'Turtle', extension: '.ttl', graphs: false },
  { name: 'trig', label: 'TriG', extension: '.trig', graphs: true },
  { name: 'rdfxml', label: 'RDF/XML', extension: '.rdf', graphs: false },
  { name: 'jsonld', label: 'JSON-LD', extension: '.jsonld', graphs: true },
  { name: 'ntriples', label: 'N-Triples', extension: '.nt', graphs: false },
  { name: 'nquads', label: 'N-Quads', extension: '.nq', graphs: true },
] as const

/** Why a format cannot carry the ontology (422 export_unsupported_content). */
interface ExportReason {
  code: string
  count: number
  examples: { s: Term; p: string; o: Term; g?: GraphTerm }[]
}

const EXPORT_REASON_TEXT: Record<string, string> = {
  named_graphs: 'statements in named graphs (this format holds one graph)',
  triple_terms: 'triple terms, such as annotations (this format cannot write them)',
  rdfxml_name: 'predicates or classes RDF/XML cannot write as an XML element name (for example one ending in a digit)',
  xml_characters: 'values with control characters XML cannot hold',
}

/** The file extensions the import accepts (UDR-0182 D6). */
const IMPORT_ACCEPT = '.ttl,.turtle,.trig,.nt,.nq,.rdf,.owl,.xml,.jsonld,.json'

const CARDINALITY_SYMBOL: Record<string, string> = {
  'one-to-one': '1:1',
  'one-to-many': '1:N',
  'many-to-one': 'N:1',
  'many-to-many': 'N:M',
}

/** owl:inverseOf: the derived inverse edges of the Inferred switch (PRP-0211, UDR-0193 D18). */
const OWL_INVERSE_OF = 'http://www.w3.org/2002/07/owl#inverseOf'

const COLOR_PRESETS = ['', '#3b82f6', '#22c55e', '#eab308', '#f97316', '#ef4444', '#a855f7', '#14b8a6']

const XSD_RANGES = ['string', 'integer', 'decimal', 'boolean', 'date', 'dateTime'] as const

const DEFAULT_SPARQL = [
  'PREFIX owl: <http://www.w3.org/2002/07/owl#>',
  'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>',
  'SELECT ?entity ?label WHERE {',
  '  ?entity a owl:Class ; rdfs:label ?label .',
  '}',
].join('\n')

function slugify(text: string): string {
  const slug = text
    .trim()
    .replace(/[^\p{L}\p{N}]+/gu, '_')
    .replace(/^_+|_+$/g, '')
  return slug || 'term'
}

function mintIri(baseIri: string, label: string, taken: Set<string>): string {
  const base = `${baseIri}${slugify(label)}`
  if (!taken.has(base)) return base
  let n = 2
  while (taken.has(`${base}_${n}`)) n += 1
  return `${base}_${n}`
}

/** Every IRI used as a subject (new terms must not collide with any of them). */
function takenIris(model: OntologyModel): Set<string> {
  return new Set(model.resources.filter((r) => r.term.type === 'iri').map((r) => (r.term as { value: string }).value))
}

/** True when the model uses RDF 1.2 features (triple terms, directional strings). */
function usesRdf12(model: OntologyModel): boolean {
  const visit = (term: Term): boolean => term.type === 'triple' || (term.type === 'literal' && Boolean(term.direction))
  return model.resources.some((r) => r.statements.some((s) => visit(s.o)))
}

/** A short text form of a term for the export refusal list (prefixed names where possible). */
function compactTerm(term: Term, model: OntologyModel): string {
  switch (term.type) {
    case 'iri':
      return compactIri(term.value, model.document.prefixes)
    case 'bnode':
      return `_:${term.value}`
    case 'literal':
      return JSON.stringify(term.value)
    case 'triple':
      return `<<( ${compactTerm(term.s, model)} ${compactIri(term.p, model.document.prefixes)} ${compactTerm(term.o, model)} )>>`
  }
}

/** Grid placement for entities without a stored or computed position. */
function gridPosition(index: number): { x: number; y: number } {
  return { x: 40 + (index % 6) * 150, y: 40 + Math.floor(index / 6) * 140 }
}

// ---- Custom Entity node (circle, 360-degree connectable) ----------------------

interface EntityNodeData extends Record<string, unknown> {
  label: string
  emoji: string
  color: string
  propertyCount: number
  keyCount: number
  isSelected: boolean
  related: boolean
  dimmed: boolean
  matched: boolean
  /** Direct individuals of this class (UDR-0187 D2); the badge opens the Data view. */
  individualCount: number
  onOpenData: (cls: string) => void
  /** SHACL node shapes targeting this class (PRP-0212, UDR-0194 D12); the badge opens Shapes. */
  shapeCount?: number
  onOpenShapes?: (cls: string) => void
  iri: string
  /** A faded node: an imported class this ontology refers to (read-only, UDR-0189 D8). */
  imported?: boolean
  sourceName?: string
}

type EntityFlowNode = Node<EntityNodeData, 'entity'>

const NODE_SIZE = 72
// Selector for the inner draggable disc: the node's `dragHandle` restricts drag
// to the inner circle so the outer ring stays a pure connection zone (UDR-0098 D1).
const ENTITY_DRAG_CLASS = 'entity-drag'
// The full-node connection handle CSS: fills the node, invisible, no transform,
// so a drag can start / end anywhere on the perimeter (360 degrees).
const RING_HANDLE_CLASS =
  '!absolute !inset-0 !m-0 !h-full !w-full !min-h-0 !min-w-0 !transform-none !rounded-full !border-0 !bg-transparent'

const EntityNode = memo(function EntityNode({ data }: NodeProps<EntityFlowNode>) {
  return (
    <div className="relative transition-opacity" style={{ height: NODE_SIZE, width: NODE_SIZE }}>
      {/* Outer ring = ONE 360-degree connection zone (UDR-0098 D1). Two full-node
          handles (target + source) sit under everything; ConnectionMode.Loose lets
          a drag start or end anywhere on the ring. */}
      <Handle id="ring-target" type="target" position={Position.Top} className={RING_HANDLE_CLASS} />
      <Handle id="ring-source" type="source" position={Position.Top} className={RING_HANDLE_CLASS} />
      {/* Outer ring visual -- non-interactive so the handle beneath receives the drag. */}
      <div
        className={cn(
          'pointer-events-none absolute inset-0 rounded-full border-2 bg-white shadow-sm',
          data.isSelected && 'ring-2 ring-blue-500 ring-offset-2',
          !data.isSelected && data.related && 'ring-2 ring-blue-300 ring-offset-2',
          data.matched && 'ring-2 ring-amber-500 ring-offset-2',
        )}
        style={{
          borderColor: data.color || '#d4d4d8',
          borderStyle: data.imported ? 'dashed' : 'solid',
          opacity: data.dimmed ? 0.25 : data.imported ? 0.55 : 1,
        }}
      />
      {/* Inner disc = the move/drag surface (dragHandle '.entity-drag'), on top. */}
      <div
        className={cn(
          ENTITY_DRAG_CLASS,
          'absolute inset-[7px] flex items-center justify-center rounded-full border border-zinc-200 bg-white text-zinc-900',
          data.imported ? 'cursor-pointer border-dashed text-zinc-500' : 'cursor-move',
        )}
        style={{ opacity: data.dimmed ? 0.25 : data.imported ? 0.6 : 1 }}
        title={
          data.imported
            ? `Imported from ${data.sourceName} (read-only; placed automatically)`
            : 'Drag to move; drag from the outer ring to connect'
        }>
        <span className="text-2xl leading-none">{data.emoji || data.label.charAt(0).toUpperCase()}</span>
      </div>
      {data.propertyCount > 0 && (
        <span className="pointer-events-none absolute -right-1.5 -top-1.5 flex h-5 min-w-5 items-center justify-center rounded-full border bg-zinc-100 px-1 text-[10px] font-medium text-zinc-600">
          {data.propertyCount}
        </span>
      )}
      {data.keyCount > 0 && (
        <span
          className="pointer-events-none absolute -bottom-1.5 -right-1.5 flex h-5 w-5 items-center justify-center rounded-full border bg-amber-100 text-amber-700"
          title={`${data.keyCount} key propert${data.keyCount === 1 ? 'y' : 'ies'}`}>
          <KeyRound className="h-3 w-3" />
        </span>
      )}
      {data.individualCount > 0 && (
        <button
          type="button"
          className="nodrag nopan absolute -bottom-1.5 -left-2 flex h-5 min-w-5 items-center justify-center rounded-full border border-emerald-300 bg-emerald-50 px-1 text-[10px] font-medium text-emerald-700 hover:bg-emerald-100"
          title={`${data.individualCount} individual${data.individualCount === 1 ? '' : 's'}: open in the Data view`}
          aria-label={`Open the ${data.individualCount} individuals of ${data.label} in the Data view`}
          onClick={(e) => {
            e.stopPropagation()
            data.onOpenData(data.iri)
          }}>
          x{data.individualCount}
        </button>
      )}
      {(data.shapeCount ?? 0) > 0 && (
        <button
          type="button"
          className="nodrag nopan absolute -left-2 -top-1.5 flex h-5 min-w-5 items-center justify-center rounded-full border border-indigo-300 bg-indigo-50 px-0.5 text-indigo-700 hover:bg-indigo-100"
          title={`${data.shapeCount} SHACL shape${data.shapeCount === 1 ? '' : 's'} target this class: open in the Shapes view`}
          aria-label={`Open the shapes of ${data.label}`}
          onClick={(e) => {
            e.stopPropagation()
            data.onOpenShapes?.(data.iri)
          }}>
          <ShieldCheck className="h-3 w-3" />
        </button>
      )}
      {/* Label below the circle (kept outside the shape per the design spec). */}
      <span className="pointer-events-none absolute left-1/2 top-full mt-1.5 max-w-[130px] -translate-x-1/2 truncate text-center text-xs font-medium text-zinc-700">
        {data.label}
      </span>
      {data.imported && (
        <span className="pointer-events-none absolute left-1/2 top-full mt-5 max-w-[130px] -translate-x-1/2 truncate text-center text-[10px] text-zinc-400">
          {data.sourceName}
        </span>
      )}
    </div>
  )
})

const nodeTypes = { entity: EntityNode }

// ---- Custom floating relationship edge (UDR-0098 D1/D2) ----------------------
// Attaches at the nearest circle perimeter (recomputed live via useInternalNode)
// and fans parallel edges out by per-edge curvature so multiple relationships
// between the same node pair are individually selectable.

interface RelEdgeData extends Record<string, unknown> {
  label: string
  stroke: string
  strokeWidth: number
  opacity: number
  labelColor: string
  parallelIndex: number
  parallelCount: number
  /** The object property this edge draws (one property may draw several edges, UDR-0180 D5). */
  relationshipIri: string
  /** An "is a" edge (rdfs:subClassOf, UDR-0185 D3): dashed, open arrow to the superclass. */
  isa?: boolean
  /** PRP-0211 (UDR-0193 D18): an inferred / derived edge -- dotted, faint, never edited. */
  inferred?: 'inferred' | 'derived'
  /** The statement Explain is asked about (inferred edges). */
  explain?: { s: Term; p: Term; o: Term; graph: Term | null }
  /** A derived inverse edge: the property it is the inverse of. */
  inverseOf?: string
}

const PARALLEL_SPREAD = 26 // px of perpendicular offset per fan-out step

function nodeCenter(node: ReturnType<typeof useInternalNode>): { x: number; y: number; r: number } {
  const w = node?.measured?.width ?? NODE_SIZE
  const h = node?.measured?.height ?? NODE_SIZE
  const px = node?.internals.positionAbsolute.x ?? 0
  const py = node?.internals.positionAbsolute.y ?? 0
  return { x: px + w / 2, y: py + h / 2, r: Math.min(w, h) / 2 }
}

const FloatingRelationshipEdge = memo(function FloatingRelationshipEdge({
  id,
  source,
  target,
  markerEnd,
  data,
}: EdgeProps) {
  const sourceNode = useInternalNode(source)
  const targetNode = useInternalNode(target)
  const d = (data ?? {}) as unknown as RelEdgeData
  if (!sourceNode || !targetNode) return null

  const a = nodeCenter(sourceNode)
  const b = nodeCenter(targetNode)
  const idx = d.parallelIndex ?? 0
  const count = d.parallelCount ?? 1
  const factor = idx - (count - 1) / 2
  const baseStyle = {
    stroke: d.stroke,
    strokeWidth: d.strokeWidth,
    opacity: d.opacity,
    strokeDasharray: d.inferred ? '2 4' : d.isa ? '6 4' : undefined,
  }

  let path: string
  let lx: number
  let ly: number

  if (source === target) {
    // Self-relationship -> a loop above the node; multiple loops stack outward.
    const h = 34 + Math.abs(factor) * 26 + (count > 1 ? 8 : 0)
    const startX = a.x - a.r * 0.5
    const startY = a.y - a.r * 0.87
    const endX = a.x + a.r * 0.5
    const endY = a.y - a.r * 0.87
    path = `M ${startX} ${startY} C ${a.x - a.r} ${a.y - a.r - h} ${a.x + a.r} ${a.y - a.r - h} ${endX} ${endY}`
    lx = a.x
    ly = a.y - a.r - h * 0.72
  } else {
    // Canonical (id-sorted) perpendicular so opposite-direction siblings fan to
    // opposite sides instead of overlapping.
    const [c1, c2] = source < target ? [a, b] : [b, a]
    const dx = c2.x - c1.x
    const dy = c2.y - c1.y
    const len = Math.hypot(dx, dy) || 1
    const px = -dy / len
    const py = dx / len
    const off = factor * PARALLEL_SPREAD
    const mx = (a.x + b.x) / 2 + px * off
    const my = (a.y + b.y) / 2 + py * off
    // Perimeter attach points aimed at the control point (tangent-ish entry).
    const sAng = Math.atan2(my - a.y, mx - a.x)
    const tAng = Math.atan2(my - b.y, mx - b.x)
    const sx = a.x + Math.cos(sAng) * a.r
    const sy = a.y + Math.sin(sAng) * a.r
    const tx = b.x + Math.cos(tAng) * b.r
    const ty = b.y + Math.sin(tAng) * b.r
    path = `M ${sx} ${sy} Q ${mx} ${my} ${tx} ${ty}`
    lx = 0.25 * sx + 0.5 * mx + 0.25 * tx
    ly = 0.25 * sy + 0.5 * my + 0.25 * ty
  }

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={baseStyle} />
      {d.label ? (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan pointer-events-none absolute rounded bg-white/85 px-1 text-[10px] font-medium"
            style={{
              transform: `translate(-50%, -50%) translate(${lx}px, ${ly}px)`,
              color: d.labelColor,
              opacity: d.opacity,
            }}>
            {d.label}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  )
})

const edgeTypes = { floating: FloatingRelationshipEdge }

// ---- Canvas toolbar (needs the ReactFlowProvider context) --------------------

/** The graph selector's value for a scope ('*' all, '' default, else a term key). */
function scopeKey(scope: GraphScope): string {
  return scope === 'all' ? '*' : scope === 'default' ? '' : termKey(scope)
}

/** The graph selector (or the "add a named graph" button) shared by both views' toolbars. */
function GraphScopeControl(props: {
  graphs: GraphTerm[]
  graphName: (g: GraphTerm) => string
  scope: GraphScope
  onScopeChange: (scope: GraphScope) => void
  onNewGraph: () => void
  readOnly: boolean
  disabled: boolean
}) {
  const iconButton = 'h-7 w-7 text-zinc-600'
  return props.graphs.length > 0 ? (
    <select
      className="h-7 max-w-[200px] rounded-md border bg-white px-1 text-xs"
      aria-label="Graph"
      title="Which graph the canvas and the search show; new statements go into the selected graph"
      value={scopeKey(props.scope)}
      disabled={props.disabled}
      onChange={(e) => {
        const value = e.target.value
        if (value === '+') {
          props.onNewGraph()
          return
        }
        if (value === '*') props.onScopeChange('all')
        else if (value === '') props.onScopeChange('default')
        else {
          const g = props.graphs.find((item) => termKey(item) === value)
          if (g) props.onScopeChange(g)
        }
      }}>
      <option value="*">All graphs</option>
      <option value="">Default graph</option>
      {props.graphs.map((g) => (
        <option key={termKey(g)} value={termKey(g)}>
          {props.graphName(g)}
        </option>
      ))}
      {!props.readOnly && <option value="+">New graph...</option>}
    </select>
  ) : (
    <Button
      variant="ghost"
      size="icon"
      className={iconButton}
      onClick={props.onNewGraph}
      disabled={props.disabled || props.readOnly}
      aria-label="Add a named graph"
      title="Add a named graph (the ontology becomes a dataset, saved as TriG)">
      <Layers className="h-4 w-4" />
    </Button>
  )
}

function CanvasToolbar(props: {
  onAddEntity: () => void
  onResetLayout: () => void
  onDownload: () => void
  layouting: boolean
  disabled: boolean
  /** Named graphs; the selector shows only when there is one (or the user adds one). */
  graphs: GraphTerm[]
  graphName: (g: GraphTerm) => string
  scope: GraphScope
  onScopeChange: (scope: GraphScope) => void
  onNewGraph: () => void
  readOnly: boolean
  /** What a drag between two entities creates (UDR-0185 D3). */
  linkMode: LinkMode
  onLinkModeChange: (mode: LinkMode) => void
  showIsa: boolean
  onShowIsaChange: (show: boolean) => void
  /** Draw the imported classes this ontology refers to (UDR-0189 D8); null = nothing imported. */
  showImported: boolean | null
  onShowImportedChange: (show: boolean) => void
  /** The detected vocabulary style for new terms and why (UDR-0185 D4). */
  style: VocabularyStyle
  styleReason: string
  view: ManagerView
  onViewChange: (view: ManagerView) => void
  shapesAvailable: boolean
  /** The Inferred switch (PRP-0211, UDR-0193 D17). */
  inferredMode: InferredMode
  onInferredModeChange: (mode: InferredMode) => void
  inferredNote: string | null
}) {
  const { zoomIn, zoomOut, fitView } = useReactFlow()
  const iconButton = 'h-7 w-7 text-zinc-600'
  return (
    <div className="ontology-toolbar flex shrink-0 items-center gap-1 border-b bg-zinc-50 px-2 py-1">
      <ViewSwitch
        view={props.view}
        onChange={props.onViewChange}
        disabled={props.disabled}
        shapes={props.shapesAvailable}
      />
      <div className="mx-1 h-4 w-px bg-zinc-200" />
      <Button
        variant="ghost"
        size="sm"
        className="h-7 px-2 text-xs"
        onClick={props.onAddEntity}
        disabled={props.disabled}>
        <Plus className="mr-1 h-3.5 w-3.5" /> Entity
      </Button>
      <select
        className="h-7 rounded-md border bg-white px-1 text-xs"
        aria-label="Link"
        title="What dragging from one entity to another creates: a relationship, or an 'is a' (rdfs:subClassOf) link from the subclass to the superclass"
        value={props.linkMode}
        disabled={props.disabled || props.readOnly}
        onChange={(e) => props.onLinkModeChange(e.target.value as LinkMode)}>
        <option value="relationship">Link: Relationship</option>
        <option value="isa">Link: Is a</option>
      </select>
      <label
        className="flex h-7 items-center gap-1 px-1 text-xs text-zinc-600"
        title="Show the 'is a' (rdfs:subClassOf) edges; remembered in this browser">
        <input
          type="checkbox"
          checked={props.showIsa}
          disabled={props.disabled}
          onChange={(e) => props.onShowIsaChange(e.target.checked)}
        />
        Show &quot;is a&quot;
      </label>
      {props.showImported !== null && (
        <label
          className="flex h-7 items-center gap-1 px-1 text-xs text-zinc-600"
          title="Show the imported classes this ontology refers to, faded (read-only); remembered in this browser">
          <input
            type="checkbox"
            checked={props.showImported}
            disabled={props.disabled}
            onChange={(e) => props.onShowImportedChange(e.target.checked)}
          />
          Imported
        </label>
      )}
      <InferredSwitch
        mode={props.inferredMode}
        onChange={props.onInferredModeChange}
        disabled={props.disabled}
        note={props.inferredNote}
      />
      <span
        className="rounded bg-zinc-200 px-1.5 py-0.5 text-[10px] font-semibold text-zinc-600"
        title={props.styleReason}>
        <span className="sr-only">Vocabulary style: </span>
        {props.style.toUpperCase()}
      </span>
      <div className="mx-1 h-4 w-px bg-zinc-200" />
      <GraphScopeControl {...props} />
      <div className="mx-1 h-4 w-px bg-zinc-200" />
      <Button
        variant="ghost"
        size="icon"
        className={iconButton}
        onClick={() => zoomIn()}
        aria-label="Zoom in"
        title="Zoom in">
        <ZoomIn className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className={iconButton}
        onClick={() => zoomOut()}
        aria-label="Zoom out"
        title="Zoom out">
        <ZoomOut className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className={iconButton}
        onClick={() => fitView({ padding: 0.2 })}
        aria-label="Fit to view"
        title="Fit to view">
        <Maximize className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className={iconButton}
        onClick={props.onResetLayout}
        disabled={props.disabled || props.layouting}
        aria-label="Reset layout"
        title="Reset layout (auto-arrange)">
        {props.layouting ? <Loader2 className="h-4 w-4 animate-spin" /> : <LayoutGrid className="h-4 w-4" />}
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className={iconButton}
        onClick={props.onDownload}
        disabled={props.disabled}
        aria-label="Download graph"
        title="Download graph (PNG)">
        <ImageDown className="h-4 w-4" />
      </Button>
    </div>
  )
}

// ---- The portal ---------------------------------------------------------------

export function OntologyManager({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  // Catalog
  const [catalog, setCatalog] = useState<CatalogEntry[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // The loaded statement-complete projection (the editing SSOT of this modal).
  // `baseIri` is the catalog's minting namespace for NEW terms, not document.base.
  const [baseIri, setBaseIri] = useState('')
  // PRP-0218 (UDR-0200 D3 / D8): new individuals are minted in their own namespace
  // (`{ontology}/id/`); for the original form it equals `baseIri`.
  const [individualsIri, setIndividualsIri] = useState('')
  const [model, setModel] = useState<OntologyModel>(EMPTY_MODEL)
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([])
  const [revision, setRevision] = useState<string | null>(null)
  const [staleRevision, setStaleRevision] = useState(false)
  // The statements and document the save diff is taken against (UDR-0183 D7), and the
  // statements a rebased save could not apply (D8).
  const saveBaseRef = useRef<SaveBase | null>(null)
  const [saveConflicts, setSaveConflicts] = useState<{ count: number; items: SaveConflict[] } | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  // Canvas interaction
  const [selection, setSelection] = useState<Selection | null>(null)
  const [linkMode, setLinkMode] = useState<LinkMode>('relationship')
  const [showIsa, setShowIsa] = useState(readShowIsa)
  // Schema | Data view and the Data view's selection (PRP-0205 / UDR-0187 D2).
  const [view, setView] = useState<ManagerView>(readView)
  const [dataGroup, setDataGroup] = useState<DataGroup | null>(null)
  const [includeSubclasses, setIncludeSubclasses] = useState(false)
  const [newIndividualClass, setNewIndividualClass] = useState<string | null>(null)
  const [newIndividualName, setNewIndividualName] = useState('')
  const [newIndividualLabel, setNewIndividualLabel] = useState('')
  const [individualDeleteTarget, setIndividualDeleteTarget] = useState<{
    key: string
    label: string
    refs: IndividualReferences
  } | null>(null)
  const [highlight, setHighlight] = useState<Set<string> | null>(null)
  const [layouting, setLayouting] = useState(false)
  const canvasRef = useRef<HTMLDivElement | null>(null)
  // Positions for entities WITHOUT cw:x / cw:y: a transient layout, never saved on
  // its own (UDR-0180 D7). The first user layout action of a session writes the
  // positions of every entity shown; later drags write only the moved ones.
  const [autoPositions, setAutoPositions] = useState<Map<string, { x: number; y: number }>>(new Map())
  const layoutCommittedRef = useRef(false)

  // Inspector dialogs
  const [documentOpen, setDocumentOpen] = useState(false)
  const [newResourceOpen, setNewResourceOpen] = useState(false)
  const [entityDeleteTarget, setEntityDeleteTarget] = useState<{ iri: string; label: string; refs: number } | null>(
    null,
  )
  const [pendingReified, setPendingReified] = useState<PendingReifiedEdit | null>(null)
  // The answer per edited statement (key|index), so typing does not re-ask per keystroke.
  const reifierChoiceRef = useRef<Map<string, boolean>>(new Map())
  const [pendingDelete, setPendingDelete] = useState<PendingAnnotatedDelete | null>(null)
  const [annotateTarget, setAnnotateTarget] = useState<{ key: string; index: number; iri: string } | null>(null)

  // Graphs (UDR-0182): the selector scopes the canvas and the search and is the
  // target graph of new statements.
  const [graphScope, setGraphScope] = useState<GraphScope>('all')
  const [newGraphOpen, setNewGraphOpen] = useState(false)
  const [newGraphIri, setNewGraphIri] = useState('')

  // Export (UDR-0182 D7): the format menu and the reasons a format was refused.
  const [exportTarget, setExportTarget] = useState<CatalogEntry | null>(null)
  const [exporting, setExporting] = useState<string | null>(null)
  const [exportRefusal, setExportRefusal] = useState<{
    format: string
    message: string
    reasons: ExportReason[]
  } | null>(null)
  const [exportNotes, setExportNotes] = useState<string | null>(null)

  // owl:imports (PRP-0207 / UDR-0189): resolution and the read-only imported vocabulary.
  const [importsInfo, setImportsInfo] = useState<ImportsInfo | null>(null)
  const [importsLoading, setImportsLoading] = useState(false)
  const [importsOpen, setImportsOpen] = useState(false)
  const [importsTab, setImportsTab] = useState<ImportsTab>('imports')
  const [mapIri, setMapIri] = useState<string | null>(null)
  const [showImported, setShowImported] = useState(readShowImported)
  // The Search pane's "Include imports"; null = the App Settings default.
  const [includeImports, setIncludeImports] = useState<boolean | null>(null)
  // The Search pane's entailment mode (UDR-0191 D10): None every time the dialog opens.
  const [entailment, setEntailment] = useState<Entailment>('simple')
  useEffect(() => {
    if (open) setEntailment('simple')
  }, [open])
  const [deleteImporters, setDeleteImporters] = useState<{ id: string; name: string }[] | null>(null)

  // Right pane
  const [rightTab, setRightTab] = useState<RightTab>('detail')
  // PRP-0211 (UDR-0193): the stored entailment's state, Explain, and the Inferred switch.
  const [entailmentStatus, setEntailmentStatus] = useState<EntailmentStatus | null>(null)
  // PRP-0212 (UDR-0194): SHACL validation of the saved version, its settings and views.
  const [shaclStatus, setShaclStatus] = useState<ShaclStatus | null>(null)
  const [shaclDialogOpen, setShaclDialogOpen] = useState(false)
  const [strictRefusal, setStrictRefusal] = useState<{
    count: number
    results: ShaclResultItem[]
    message: string
  } | null>(null)
  const [shaclNotice, setShaclNotice] = useState<string | null>(null)
  // PRP-0213 (UDR-0195 D1-D6): Merge into the open ontology; one step back until saved.
  const [mergeOpen, setMergeOpen] = useState(false)
  const [mergeUndo, setMergeUndo] = useState<{
    model: OntologyModel
    dirty: boolean
    count: number
    kind: 'merge' | 'import'
  } | null>(null)
  // PRP-0217 (UDR-0199 D1 / D2): Import table from the Data view; the same one step back.
  const [tableOpen, setTableOpen] = useState(false)
  const [shaclList, setShaclList] = useState<{ title: string; query: Record<string, string> } | null>(null)
  const [shapesGraphKey, setShapesGraphKey] = useState<string | null>(null)
  const [shapeHighlight, setShapeHighlight] = useState<{ shape: string; path: string | null; nonce: number } | null>(
    null,
  )
  const [consistency, setConsistency] = useState<{
    revision: string
    state: string
    message: string | null
    items: {
      key: string
      kind: string
      graph: string | null
      message: string
      terms: string[]
      subjects: Term[]
    }[]
  } | null>(null)
  const [explainRequest, setExplainRequest] = useState<ExplainRequest | null>(null)
  const [explainResult, setExplainResult] = useState<ExplainResult | null>(null)
  const [explainLoading, setExplainLoading] = useState(false)
  const [explainError, setExplainError] = useState<string | null>(null)
  // A derived inverse edge explains itself (no rule of the reasoner gives it, UDR-0193 D20).
  const [explainNote, setExplainNote] = useState<string | null>(null)
  const [inferredMode, setInferredMode] = useState<InferredMode>(readInferredMode)
  const [inferredStatements, setInferredStatements] = useState<InferredStatements | null>(null)
  const searchAbortRef = useRef<AbortController | null>(null)
  // Deleted ontologies (PRP-0202 / UDR-0184 D6).
  const [trashOpen, setTrashOpen] = useState(false)
  // A characteristic turned on in an RDFS-only ontology waits for confirmation (PRP-0204 Q1).
  const [owlConfirm, setOwlConfirm] = useState<{ iri: string; characteristic: Characteristic } | null>(null)
  const [sparql, setSparql] = useState(DEFAULT_SPARQL)
  const [nlQuestion, setNlQuestion] = useState('')
  const [queryResult, setQueryResult] = useState<QueryResult | null>(null)
  const [queryRunning, setQueryRunning] = useState(false)
  const [nlRunning, setNlRunning] = useState(false)

  // Catalog actions
  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createDescription, setCreateDescription] = useState('')
  // PRP-0218 (UDR-0200 D4): the IRI of the new ontology, decided once.
  const [createShortName, setCreateShortName] = useState('')
  const [createCustomIri, setCreateCustomIri] = useState('')
  const [createUseCustom, setCreateUseCustom] = useState(false)
  const [creating, setCreating] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<CatalogEntry | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [renameTarget, setRenameTarget] = useState<CatalogEntry | null>(null)
  const [renameName, setRenameName] = useState('')
  const [renameDescription, setRenameDescription] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [importing, setImporting] = useState(false)
  // PRP-0211 (UDR-0193 D2): the file being imported, and "loaded, waiting until drawn".
  const [importLabel, setImportLabel] = useState<string | null>(null)
  const [importSettling, setImportSettling] = useState(false)
  const [nodesReady, setNodesReady] = useState(false)
  const [layoutFailed, setLayoutFailed] = useState(false)
  const importInputRef = useRef<HTMLInputElement | null>(null)

  // Unsaved-changes confirmation: the action to run once the user discards.
  const [pendingDiscard, setPendingDiscard] = useState<(() => void) | null>(null)
  // Advertised by GET /api/ontology/catalog (PRP-0139 / UDR-0122 D4). Derived from
  // the server payload, never inferred client-side, so the notice cannot claim a
  // restriction the backend is not enforcing.
  const [demoBlocked, setDemoBlocked] = useState(false)
  // PRP-0218 (UDR-0200 D10): the IRI base new ontologies are named under (App Settings).
  const [iriBase, setIriBase] = useState(DEFAULT_IRI_BASE)
  const [iriBaseIsDefault, setIriBaseIsDefault] = useState(true)

  const fetchCatalog = useCallback(async () => {
    setError(null)
    try {
      const res = await fetch('/api/ontology/catalog')
      if (!res.ok) throw new Error('Failed to load the ontology catalog')
      const data = await res.json()
      setCatalog((data.ontologies ?? []) as CatalogEntry[])
      setDemoBlocked(data.demo_mode === true)
      setIriBase(typeof data.iri_base === 'string' ? data.iri_base : DEFAULT_IRI_BASE)
      setIriBaseIsDefault(data.iri_base_is_default !== false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load the ontology catalog')
    }
  }, [])

  /** Reorder the catalog: optimistic, rolled back on failure (PRP-0213, UDR-0195 D8 / D9). */
  const reorderCatalog = useCallback(
    async (ids: string[]) => {
      const previous = catalog
      const byId = new Map(catalog.map((entry) => [entry.id, entry]))
      setCatalog(ids.map((id) => byId.get(id)).filter((entry): entry is CatalogEntry => entry !== undefined))
      try {
        const res = await fetch('/api/ontology/catalog/order', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids }),
        })
        if (!res.ok) throw new Error('Could not save the order of the ontologies')
        const body = await res.json()
        setCatalog((body.ontologies ?? []) as CatalogEntry[])
      } catch (err) {
        setCatalog(previous)
        setError(err instanceof Error ? err.message : 'Could not save the order of the ontologies')
      }
    },
    [catalog],
  )

  /** Resolve the imports of an ontology and read the imported vocabulary (UDR-0189 D6). */
  const fetchImports = useCallback(async (id: string) => {
    setImportsLoading(true)
    try {
      const res = await fetch(`/api/ontology/${id}/imports`)
      if (!res.ok) throw new Error('imports')
      setImportsInfo((await res.json()) as ImportsInfo)
    } catch {
      setImportsInfo(null)
    } finally {
      setImportsLoading(false)
    }
  }, [])

  /**
   * Load an ontology. `keepView` (after a rebased save, UDR-0183 D8) keeps the graph
   * selector, the selection, the search result and the transient layout, so the
   * canvas only gains what another tab saved.
   */
  const loadOntology = useCallback(
    async (id: string, options: { keepView?: boolean } = {}) => {
      setLoading(true)
      setError(null)
      try {
        const res = await fetch(`/api/ontology/${id}`)
        if (!res.ok) throw new Error('Failed to load the ontology')
        const data = await res.json()
        const loaded = fromProjection(data)
        setSelectedId(id)
        setBaseIri((data.base_iri as string) ?? '')
        setIndividualsIri(((data.iri as OntologyIris | undefined)?.individuals ?? data.base_iri ?? '') as string)
        setModel(loaded)
        saveBaseRef.current = saveBase(loaded)
        setDiagnostics((data.diagnostics ?? []) as Diagnostic[])
        setRevision((data.revision as string) ?? null)
        setStaleRevision(false)
        setSaveConflicts(null)
        setDirty(false)
        void fetchImports(id)
        if (options.keepView) return
        setIncludeImports(null)
        setAutoPositions(new Map())
        setLayoutFailed(false)
        setExplainRequest(null)
        setExplainResult(null)
        layoutCommittedRef.current = false
        reifierChoiceRef.current = new Map()
        setGraphScope('all')
        setSelection(null)
        setDataGroup(null)
        setHighlight(null)
        setQueryResult(null)
        setRightTab('detail')
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load the ontology')
      } finally {
        setLoading(false)
      }
    },
    [fetchImports],
  )

  useEffect(() => {
    if (open) void fetchCatalog()
  }, [open, fetchCatalog])

  /** Run `action` immediately, or after an unsaved-changes confirmation. */
  const guardDirty = useCallback(
    (action: () => void) => {
      if (dirty) setPendingDiscard(() => action)
      else action()
    },
    [dirty],
  )

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) {
        guardDirty(() => onOpenChange(false))
        return
      }
      onOpenChange(next)
    },
    [guardDirty, onOpenChange],
  )

  /**
   * Save statement changes as operations against the loaded revision, rebased onto
   * a newer file when they still fit (UDR-0183 D5 / D7). Returns false when the
   * document changed, which only the full PUT carries (PRP-0201 C3).
   */
  const saveStatements = useCallback(
    async (override = false): Promise<boolean> => {
      const base = saveBaseRef.current
      if (!selectedId || !base || documentKey(model.document) !== base.document) return false
      const { operations, freshBlankNodes } = diffStatements(base.statements, model)
      if (operations.length === 0) {
        setDirty(false)
        return true
      }
      const res = await fetch(`/api/ontology/${selectedId}/statements`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          revision,
          operations,
          fresh_blank_nodes: freshBlankNodes,
          on_stale: 'rebase',
          ...(override ? { shacl_override: true } : {}),
        }),
      })
      if (!res.ok) {
        const refused = await demoRefusal(res)
        if (refused) {
          setDemoBlocked(true)
          throw new Error(refused)
        }
        const body = await res.json().catch(() => null)
        const detail = body?.detail
        if (res.status === 409 && detail?.error === 'stale_conflict') {
          // Nothing was written; the user decides by reloading (no overwrite, D8).
          setSaveConflicts({ count: detail.count ?? 0, items: (detail.conflicts ?? []) as SaveConflict[] })
          setStaleRevision(true)
          throw new Error(detail.message ?? 'Some changes conflict with a newer version.')
        }
        if (res.status === 422 && detail?.error === 'shacl_violations') {
          setStrictRefusal({ count: detail.count ?? 0, results: detail.results ?? [], message: detail.message ?? '' })
          throw new Error(detail.message ?? 'Strict mode: SHACL violations. Nothing was saved.')
        }
        if (detail?.pointer && typeof detail?.message === 'string') {
          throw new Error(`Cannot save: ${detail.pointer} -- ${detail.message}`)
        }
        if (typeof detail?.message === 'string') throw new Error(detail.message)
        throw new Error(typeof detail === 'string' ? detail : 'Failed to save the ontology')
      }
      const body = await res.json().catch(() => null)
      setShaclNotice(body?.shacl?.checked === false ? (body.shacl.message as string) : null)
      if (body?.rebased) {
        // Another tab saved first and these changes were applied on top: show both.
        await loadOntology(selectedId, { keepView: true })
        return true
      }
      const renamed = (body?.blank_nodes ?? {}) as Record<string, string>
      const saved = renameBlankNodes(model, renamed)
      saveBaseRef.current = saveBase(saved)
      if (Object.keys(renamed).length > 0) setModel((current) => renameBlankNodes(current, renamed))
      setRevision((body?.revision as string) ?? null)
      setDirty(false)
      return true
    },
    [selectedId, model, revision, loadOntology],
  )

  const save = useCallback(
    async (override = false) => {
      if (!selectedId) return
      setSaving(true)
      setError(null)
      setSaveConflicts(null)
      setStrictRefusal(null)
      try {
        if (await saveStatements(override)) {
          await fetchCatalog()
          void fetchImports(selectedId)
          return
        }
        const res = await fetch(`/api/ontology/${selectedId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...toPayload(model), revision, ...(override ? { shacl_override: true } : {}) }),
        })
        if (!res.ok) {
          const refused = await demoRefusal(res)
          if (refused) {
            setDemoBlocked(true)
            throw new Error(refused)
          }
          const body = await res.json().catch(() => null)
          const detail = body?.detail
          if (res.status === 409 && detail?.error === 'stale_revision') {
            // Someone saved this ontology after it was opened here (UDR-0180 D8).
            setStaleRevision(true)
            throw new Error(detail.message ?? 'The ontology was changed elsewhere.')
          }
          if (res.status === 422 && detail?.error === 'shacl_violations') {
            setStrictRefusal({ count: detail.count ?? 0, results: detail.results ?? [], message: detail.message ?? '' })
            throw new Error(detail.message ?? 'Strict mode: SHACL violations. Nothing was saved.')
          }
          if (detail?.error === 'invalid_projection') {
            throw new Error(`Cannot save: ${detail.pointer} -- ${detail.message}`)
          }
          if (typeof detail?.message === 'string') throw new Error(detail.message)
          throw new Error(typeof detail === 'string' ? detail : 'Failed to save the ontology')
        }
        const body = await res.json().catch(() => null)
        setShaclNotice(body?.shacl?.checked === false ? (body.shacl.message as string) : null)
        setRevision((body?.revision as string) ?? null)
        saveBaseRef.current = saveBase(model)
        setDirty(false)
        await fetchCatalog()
        void fetchImports(selectedId)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to save the ontology')
      } finally {
        setSaving(false)
      }
    },
    [selectedId, model, revision, fetchCatalog, saveStatements, fetchImports],
  )

  // ---- Catalog actions ----

  const createOntology = useCallback(async () => {
    setCreating(true)
    setError(null)
    try {
      const res = await fetch('/api/ontology/catalog', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: createName,
          description: createDescription,
          ...(createUseCustom
            ? { ontology_iri: createCustomIri.trim() }
            : createShortName.trim()
              ? { short_name: createShortName.trim() }
              : {}),
        }),
      })
      if (!res.ok) {
        const refused = await demoRefusal(res)
        if (refused) setDemoBlocked(true)
        throw new Error(refused ?? (await namespaceRefusal(res, 'Failed to create the ontology')))
      }
      const entry = (await res.json()) as CatalogEntry
      setCreateOpen(false)
      setCreateName('')
      setCreateDescription('')
      setCreateShortName('')
      setCreateCustomIri('')
      setCreateUseCustom(false)
      await fetchCatalog()
      await loadOntology(entry.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create the ontology')
    } finally {
      setCreating(false)
    }
  }, [createName, createDescription, createUseCustom, createCustomIri, createShortName, fetchCatalog, loadOntology])

  /** Set the IRI of an ontology without one, once (PRP-0218, UDR-0200 D6); the error or null. */
  const setOntologyIri = useCallback(
    async (id: string, payload: { short_name?: string; ontology_iri?: string }): Promise<string | null> => {
      const res = await fetch(`/api/ontology/catalog/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ namespace: payload }),
      })
      if (!res.ok) return (await demoRefusal(res)) ?? (await namespaceRefusal(res, 'Failed to set the IRI'))
      const entry = (await res.json()) as CatalogEntry & { iri?: OntologyIris; base_iri?: string }
      setRenameTarget((current) =>
        current && current.id === id ? { ...current, namespace: entry.namespace } : current,
      )
      if (id === selectedId && entry.iri) {
        setBaseIri(entry.iri.vocabulary)
        setIndividualsIri(entry.iri.individuals)
      }
      await fetchCatalog()
      return null
    },
    [selectedId, fetchCatalog],
  )

  const renameOntology = useCallback(async () => {
    if (!renameTarget) return
    setRenaming(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/catalog/${renameTarget.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: renameName, description: renameDescription }),
      })
      if (!res.ok) {
        const refused = await demoRefusal(res)
        if (refused) setDemoBlocked(true)
        throw new Error(refused ?? 'Failed to rename the ontology')
      }
      setRenameTarget(null)
      await fetchCatalog()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to rename the ontology')
    } finally {
      setRenaming(false)
    }
  }, [renameTarget, renameName, renameDescription, fetchCatalog])

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return
    setDeleting(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/catalog/${deleteTarget.id}`, { method: 'DELETE' })
      if (!res.ok) {
        const refused = await demoRefusal(res)
        if (refused) setDemoBlocked(true)
        throw new Error(refused ?? 'Failed to delete the ontology')
      }
      if (selectedId === deleteTarget.id) {
        setSelectedId(null)
        setModel(EMPTY_MODEL)
        setDiagnostics([])
        setRevision(null)
        setDirty(false)
        setSelection(null)
        setHighlight(null)
        setImportsInfo(null)
      }
      await fetchCatalog()
      setDeleteTarget(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete the ontology')
    } finally {
      setDeleting(false)
    }
  }, [deleteTarget, selectedId, fetchCatalog])

  const importOntology = useCallback(
    async (file: File) => {
      setImporting(true)
      setImportLabel(file.name)
      setError(null)
      let settled = false
      try {
        const form = new FormData()
        form.append('file', file)
        const res = await fetch('/api/ontology/import', { method: 'POST', body: form })
        if (!res.ok) {
          const refused = await demoRefusal(res)
          if (refused) {
            setDemoBlocked(true)
            throw new Error(refused)
          }
          const body = await res.json().catch(() => null)
          const detail = body?.detail
          // A remote JSON-LD @context is refused, never fetched (UDR-0182 D6).
          throw new Error(
            typeof detail === 'string'
              ? detail
              : typeof detail?.message === 'string'
                ? detail.message
                : 'Failed to import the file',
          )
        }
        const entry = (await res.json()) as CatalogEntry
        await fetchCatalog()
        await loadOntology(entry.id)
        // Loaded: the overlay stays until the new canvas is laid out and drawn (UDR-0193 D2).
        settled = true
        setImportSettling(true)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to import the file')
      } finally {
        if (!settled) setImporting(false)
      }
    },
    [fetchCatalog, loadOntology],
  )

  /**
   * Download in one format (UDR-0182 D7). A format that cannot carry the ontology is
   * refused by the server with the reasons and example statements; they are shown in
   * the export dialog instead of a lossy file.
   */
  const exportOntology = useCallback(async (entry: CatalogEntry, format: (typeof EXPORT_FORMATS)[number]) => {
    setExporting(format.name)
    setExportRefusal(null)
    setExportNotes(null)
    try {
      const res = await fetch(`/api/ontology/${entry.id}/export?format=${format.name}`)
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        const detail = body?.detail
        setExportRefusal({
          format: format.label,
          message: typeof detail === 'string' ? detail : (detail?.message ?? `The ${format.label} export failed.`),
          reasons: (detail?.reasons ?? []) as ExportReason[],
        })
        return
      }
      const notes = res.headers.get('X-Ontology-Export-Notes')
      const blob = await res.blob()
      const objectUrl = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = objectUrl
      a.download = `${entry.name || entry.id}${format.extension}`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(objectUrl)
      if (notes) setExportNotes(notes)
      else setExportTarget(null)
    } catch (err) {
      setExportRefusal({
        format: format.label,
        message: err instanceof Error ? err.message : `The ${format.label} export failed.`,
        reasons: [],
      })
    } finally {
      setExporting(null)
    }
  }, [])

  // ---- Editing the model (every edit is a statement edit; UDR-0180 D1 / D4) ----

  const markDirty = useCallback(() => setDirty(true), [])
  // A save, a load or a discard (dirty -> false) ends the one-step "Undo merge" (UDR-0195 D5).
  useEffect(() => {
    if (!dirty) setMergeUndo(null)
  }, [dirty])

  // The canvas draws the selected graph(s) only; layout stays in the default graph
  // and is always shown (UDR-0182 D3 / D4). Edits still go to the full model.
  const graphs = useMemo(() => graphsOf(model), [model])
  // PRP-0212 (UDR-0194 D1, D12): the declared shapes graphs are not data -- the canvas and
  // the Data view leave them out unless the graph selector picks one of them.
  const selectedEntry = useMemo(() => catalog.find((entry) => entry.id === selectedId) ?? null, [catalog, selectedId])
  const shaclSettings = selectedEntry?.shacl ?? null
  const shapesGraphsKey = (shaclSettings?.shapes_graphs ?? []).join('\n')
  const declaredShapesGraphs = useMemo<GraphTerm[]>(
    () => (shapesGraphsKey ? shapesGraphsKey.split('\n').map((value) => ({ type: 'iri' as const, value })) : []),
    [shapesGraphsKey],
  )
  const hiddenGraphs = useMemo(() => new Set(declaredShapesGraphs.map(termKey)), [declaredShapesGraphs])
  const usedAsShapes = useMemo(
    () => Boolean(selectedId && catalog.some((entry) => entry.shacl?.shapes_ontologies?.includes(selectedId))),
    [catalog, selectedId],
  )
  const editableShapesGraphs = useMemo<ShapesGraph[]>(
    () =>
      declaredShapesGraphs.length > 0 ? declaredShapesGraphs : usedAsShapes || hasShapes(model, null) ? [null] : [],
    [declaredShapesGraphs, usedAsShapes, model],
  )
  const shapesAvailable = editableShapesGraphs.length > 0 || (shaclSettings?.shapes_ontologies.length ?? 0) > 0
  const currentShapesGraph =
    editableShapesGraphs.find((g) => (g ? termKey(g) : '') === shapesGraphKey) ?? editableShapesGraphs[0] ?? null
  const viewModel = useMemo(
    () => (graphScope === 'all' ? withoutGraphs(model, hiddenGraphs) : scopeModel(model, graphScope)),
    [model, graphScope, hiddenGraphs],
  )
  // The imported vocabulary, kept beside the model and never written (UDR-0189 D6).
  const imported = useMemo(() => importedIndex(importsInfo), [importsInfo])
  // Faded nodes: the imported classes this ontology refers to (D8).
  const ghostRefs = useMemo(
    () => (showImported ? referencedImportedClasses(viewModel, imported) : NO_IRIS),
    [showImported, viewModel, imported],
  )
  const ghostViews = useMemo<ImportedClassView[]>(
    () => (ghostRefs.size > 0 ? importedClassViews(viewModel, imported, ghostRefs) : []),
    [viewModel, imported, ghostRefs],
  )
  const views = useMemo(() => entityViews(viewModel), [viewModel])
  const relEdges = useMemo(() => relationshipEdges(viewModel, ghostRefs), [viewModel, ghostRefs])
  const isaEdges = useMemo(() => subclassEdges(viewModel, ghostRefs), [viewModel, ghostRefs])
  const vocabStyle = useMemo(() => vocabularyStyle(model), [model])
  const undeclared = useMemo(() => undeclaredClasses(model, imported.declared), [model, imported])
  // Individuals of the graph-scoped model, computed once per change (UDR-0187 D9).
  const dataIndex = useMemo(() => individualIndex(viewModel, imported), [viewModel, imported])
  const dataTree = useMemo(() => classTree(viewModel, imported), [viewModel, imported])
  const changeView = useCallback((next: ManagerView) => {
    setView(next)
    writeView(next)
  }, [])
  const effectiveView: ManagerView = view === 'shapes' && !shapesAvailable ? 'schema' : view
  // The classes shapes target (the canvas badge, UDR-0194 D12).
  const shapeTargets = useMemo(() => targetedClasses(model, editableShapesGraphs), [model, editableShapesGraphs])
  /** Open the Shapes view on one node shape (Show shape, the canvas badge). */
  const openShape = useCallback(
    (shapeKey: string, path: string | null) => {
      const holder = editableShapesGraphs.find((g) =>
        model.resources.some(
          (r) =>
            termKey(r.term) === shapeKey &&
            r.statements.some((st) => (st.g ? termKey(st.g) : '') === (g ? termKey(g) : '')),
        ),
      )
      if (holder !== undefined) setShapesGraphKey(holder ? termKey(holder) : '')
      setShapeHighlight({ shape: shapeKey, path, nonce: Date.now() })
      changeView('shapes')
    },
    [editableShapesGraphs, model, changeView],
  )
  const openShapesFor = useCallback(
    (cls: string) => {
      const first = shapeTargets.get(cls)?.[0]
      if (first) openShape(termKey(first), null)
    },
    [shapeTargets, openShape],
  )
  /** Open the Data view on one class (the canvas badge, a type chip). */
  const openData = useCallback(
    (cls: string) => {
      setDataGroup({ kind: 'class', iri: cls })
      changeView('data')
    },
    [changeView],
  )
  const changeShowIsa = useCallback((show: boolean) => {
    setShowIsa(show)
    writeShowIsa(show)
  }, [])
  const graphName = useCallback((g: GraphTerm) => graphLabel(g, model, model.document.prefixes), [model])

  /** Select what the canvas and the search show; a named graph is also where new statements go. */
  const changeScope = useCallback((scope: GraphScope) => {
    setGraphScope(scope)
    setModel((prev) => ({ ...prev, targetGraph: typeof scope === 'object' ? scope : null }))
    setHighlight(null)
    setQueryResult(null)
  }, [])

  /** Add a named graph to the selector and select it (saved once it holds a statement). */
  const addGraph = useCallback(() => {
    const value = newGraphIri.trim()
    if (!value) return
    const g: GraphTerm = { type: 'iri', value }
    setModel((prev) =>
      graphsOf(prev).some((item) => termKey(item) === termKey(g))
        ? prev
        : { ...prev, graphs: [...(prev.graphs ?? []), g] },
    )
    setNewGraphOpen(false)
    setNewGraphIri('')
    changeScope(g)
  }, [newGraphIri, changeScope])

  /**
   * Apply statement edits to one resource. When an edited statement is reified
   * (a reifier's rdf:reifies points to it), ask once whether the reifiers follow
   * the edit (PRP-0198 Q4; default: follow) and remember the answer for that
   * statement so typing does not ask again.
   */
  const commitEdits = useCallback(
    (key: string, edits: (StatementEdit | null)[]) => {
      const real = edits.filter((e): e is StatementEdit => e !== null)
      if (real.length === 0) return
      // Deleting an annotated statement asks whether its annotations go too (PRP-0200 Q3).
      const removal = real.length === 1 && real[0].index !== null && real[0].statement === null ? real[0] : null
      if (removal?.index != null && !assertedElsewhere(model, key, removal.index)) {
        const count = reifiersOf(model, key, removal.index).length
        if (count > 0) {
          setPendingDelete({ key, index: removal.index, reifierCount: count })
          return
        }
      }
      let reifierCount = 0
      let undecided = false
      for (const edit of real) {
        if (edit.index === null || edit.statement === null) continue
        // The old triple is still asserted in another graph: its reifiers stay (UDR-0182 D9).
        if (assertedElsewhere(model, key, edit.index)) continue
        const count = reifiersOf(model, key, edit.index).length
        if (count === 0) continue
        reifierCount += count
        if (!reifierChoiceRef.current.has(`${key}|${edit.index}`)) undecided = true
      }
      if (undecided) {
        setPendingReified({ key, edits: real, reifierCount })
        return
      }
      setModel((prev) => {
        let next = prev
        for (const edit of real) {
          const follow = edit.index === null ? true : (reifierChoiceRef.current.get(`${key}|${edit.index}`) ?? true)
          next = applyStatementEdit(next, key, edit, follow)
        }
        return next
      })
      markDirty()
    },
    [model, markDirty],
  )

  const resolvePendingReified = useCallback(
    (follow: boolean) => {
      const pending = pendingReified
      setPendingReified(null)
      if (!pending) return
      for (const edit of pending.edits) {
        if (edit.index !== null) reifierChoiceRef.current.set(`${pending.key}|${edit.index}`, follow)
      }
      setModel((prev) => {
        let next = prev
        for (const edit of pending.edits) next = applyStatementEdit(next, pending.key, edit, follow)
        return next
      })
      markDirty()
    },
    [pendingReified, markDirty],
  )

  const updateModel = useCallback(
    (update: (prev: OntologyModel) => OntologyModel) => {
      setModel(update)
      markDirty()
    },
    [markDirty],
  )

  /** Apply a merge plan as unsaved additions, keeping one step back (PRP-0213, UDR-0195 D5 / D6). */
  const applyMergePlan = useCallback(
    (plan: MergePlan) => {
      setMergeUndo({ model, dirty, count: plan.statements.length, kind: 'merge' })
      updateModel((prev) => applyMerge(prev, plan))
      setMergeOpen(false)
    },
    [model, dirty, updateModel],
  )

  /** Apply an accepted table preview as unsaved edits (PRP-0217, UDR-0199 D2); one step back. */
  const applyTablePreview = useCallback(
    (preview: TablePreview, mapping: TableMapping) => {
      setMergeUndo({ model, dirty, count: preview.add.length + preview.remove.length, kind: 'import' })
      updateModel((prev) => applyTableImport(prev, preview))
      setTableOpen(false)
      setDataGroup({ kind: 'class', iri: mapping.class })
    },
    [model, dirty, updateModel],
  )

  /** Return to the model before the last merge (also drops edits made after it). */
  const undoMerge = useCallback(() => {
    if (!mergeUndo) return
    setModel(mergeUndo.model)
    setDirty(mergeUndo.dirty)
    setMergeUndo(null)
  }, [mergeUndo])

  /** Answer "delete its annotations too?" for a pending statement deletion (default: keep). */
  const resolvePendingDelete = useCallback(
    (deleteAnnotations: boolean) => {
      const pending = pendingDelete
      setPendingDelete(null)
      if (!pending) return
      updateModel((prev) => removeStatement(prev, pending.key, pending.index, deleteAnnotations))
    },
    [pendingDelete, updateModel],
  )

  const requestAnnotate = useCallback(
    (key: string, index: number) => {
      setAnnotateTarget({ key, index, iri: mintReifierIri(model, baseIri || 'https://chatwalaau.com/ontology/local#') })
    },
    [model, baseIri],
  )

  const keyOf = useCallback((value: string) => termKey(iriTerm(value)), [])

  /** Set the displayed literal of `predicate` on the resource `value` (keeps language / datatype). */
  const setDisplayed = useCallback(
    (value: string, predicate: string, text: string) => {
      const key = keyOf(value)
      commitEdits(key, [displayedLiteralEdit(findResource(model, key), predicate, text)])
    },
    [model, keyOf, commitEdits],
  )

  /** Set the FIRST object of `predicate` (emoji, color, cardinality, a single range). */
  const setFirst = useCallback(
    (value: string, predicate: string, term: Term | null) => {
      const key = keyOf(value)
      commitEdits(key, [firstObjectEdit(findResource(model, key), predicate, term)])
    },
    [model, keyOf, commitEdits],
  )

  const addEntity = useCallback(() => {
    const value = mintIri(baseIri || 'https://chatwalaau.com/ontology/local#', 'Entity', takenIris(model))
    const count = views.length
    updateModel((prev) =>
      createEntity(prev, value, 'New Entity', 40 + (count % 5) * 60, 40 + (count % 7) * 40, vocabStyle),
    )
    setSelection({ kind: 'entity', iri: value })
    setRightTab('detail')
  }, [model, views.length, baseIri, updateModel, vocabStyle])

  const requestDeleteEntity = useCallback(
    (value: string) => {
      const view = views.find((v) => v.iri === value)
      setEntityDeleteTarget({
        iri: value,
        label: view?.label ?? localName(value),
        refs: externalReferenceCount(model, value),
      })
    },
    [model, views],
  )

  const confirmDeleteEntity = useCallback(() => {
    const target = entityDeleteTarget
    setEntityDeleteTarget(null)
    if (!target) return
    updateModel((prev) => deleteEntityFromModel(prev, target.iri))
    setSelection(null)
  }, [entityDeleteTarget, updateModel])

  const deleteRelationship = useCallback(
    (value: string) => {
      updateModel((prev) => removeResource(prev, keyOf(value)))
      setSelection(null)
    },
    [updateModel, keyOf],
  )

  /** "+ Individual": ask for a name and an optional label (UDR-0187 D6). */
  const requestCreateIndividual = useCallback((cls: string) => {
    setNewIndividualName('')
    setNewIndividualLabel('')
    setNewIndividualClass(cls)
  }, [])

  const confirmCreateIndividual = useCallback(() => {
    const cls = newIndividualClass
    if (!cls) return
    const name = newIndividualName.trim() || newIndividualLabel.trim() || 'individual'
    const value = mintIri(individualsIri || baseIri || 'https://chatwalaau.com/ontology/local#', name, takenIris(model))
    setNewIndividualClass(null)
    updateModel((prev) => createIndividual(prev, value, cls, newIndividualLabel))
    setSelection({ kind: 'individual', key: termKey(iriTerm(value)) })
    setRightTab('detail')
  }, [newIndividualClass, newIndividualName, newIndividualLabel, individualsIri, baseIri, model, updateModel])

  const requestDeleteIndividual = useCallback(
    (key: string) => {
      const resource = findResource(model, key)
      if (!resource) return
      setIndividualDeleteTarget({
        key,
        label: individualLabel(resource, model.document.prefixes),
        refs: individualReferences(model, key),
      })
    },
    [model],
  )

  /** Delete an individual and the statements referring to it, after confirmation (UDR-0187 D7). */
  const confirmDeleteIndividual = useCallback(() => {
    const target = individualDeleteTarget
    setIndividualDeleteTarget(null)
    if (!target) return
    updateModel((prev) => deleteIndividual(prev, target.key))
    setSelection(null)
  }, [individualDeleteTarget, updateModel])

  const entityOptions = useMemo(
    () => [...views.map((v) => v.iri), ...importedOptions(model, imported, 'class').map((o) => o.iri)],
    [views, model, imported],
  )
  // ---- Schema checks (PRP-0206 / UDR-0188) ----

  // Every finding over the whole model (all graphs), once per change (D1 / D6).
  const findings = useMemo(
    () =>
      // The declared shapes graphs are not data (PRP-0212, UDR-0194 D12).
      runChecks(withoutGraphs(model, hiddenGraphs), {
        bases: baseIri ? [baseIri] : [],
        diagnostics,
        imported,
        imports: importsInfo?.imports ?? [],
      }),
    [model, hiddenGraphs, baseIri, diagnostics, imported, importsInfo],
  )
  // PRP-0211 (UDR-0193 D13): the OWL 2 RL consistency of the SAVED version, from the backend.
  const consistencyFindings = useMemo<Finding[]>(() => {
    const owl = entailmentStatus?.modes['owl-rl']
    if (!owl) return []
    if (owl.state === 'queued' || owl.state === 'running') {
      return [
        {
          code: 'owl_consistency_unchecked',
          severity: 'info',
          key: 'computing',
          subjects: [],
          message: 'Checking consistency (OWL 2 RL)...',
        },
      ]
    }
    if (owl.state === 'too_large' || owl.state === 'timeout' || owl.state === 'failed') {
      return [
        {
          code: 'owl_consistency_unchecked',
          severity: 'info',
          key: owl.state,
          subjects: [],
          message: `Consistency was not checked: ${owl.message ?? owl.state}`,
        },
      ]
    }
    if (owl.state !== 'ready' || !consistency) return []
    return consistency.items.map(
      (item): Finding => ({
        code: 'owl_inconsistency',
        severity: 'warning',
        key: item.key,
        subjects: item.subjects,
        message: item.message,
        explainKey: item.key,
        graphLabel: item.graph,
        stale: dirty,
      }),
    )
  }, [entailmentStatus, consistency, dirty])
  // PRP-0212 (UDR-0194 D9): the SHACL results of the SAVED version, from the backend.
  const shaclFindings = useMemo<Finding[]>(() => {
    const status = shaclStatus
    if (!status?.configured) return []
    const out: Finding[] = []
    const unchecked = (key: string, message: string): Finding => ({
      code: 'shacl_unchecked',
      severity: 'info',
      key,
      subjects: [],
      message,
    })
    for (const source of status.shapes) {
      if (source.missing) {
        out.push(
          unchecked(
            `missing:${source.ref}`,
            source.kind === 'graph'
              ? `The shapes graph <${source.ref}> is not in this ontology yet.`
              : `The shapes ontology ${source.ref} is not in the catalog any more.`,
          ),
        )
      }
    }
    if (status.state === 'queued' || status.state === 'running' || status.state === 'missing') {
      out.push(unchecked('computing', 'Validating with SHACL...'))
      return out
    }
    if (status.state === 'shapes_error') {
      const errors = status.detail?.errors?.length
        ? status.detail.errors
        : [status.message ?? 'The shapes are not valid SHACL.']
      for (const [i, text] of errors.slice(0, 20).entries()) {
        out.push({
          code: 'shacl_shapes_error',
          severity: 'warning',
          key: `shape-error-${i}`,
          subjects: [],
          message: text,
          stale: dirty,
        })
      }
      return out
    }
    if (status.state !== 'ready') {
      out.push(unchecked(status.state, `The data was not validated: ${status.message ?? status.state}`))
      return out
    }
    for (const group of status.groups) {
      const imported = group.focus.filter((f) => f.imported).length
      out.push({
        code:
          group.severity === 'violation'
            ? 'shacl_violation'
            : group.severity === 'warning'
              ? 'shacl_warning'
              : 'shacl_info',
        severity: group.severity === 'violation' ? 'warning' : 'info',
        key: group.key,
        subjects: group.focus.filter((f) => !f.imported).map((f) => f.term),
        message: group.message,
        stale: dirty,
        shacl: {
          severity: group.severity,
          shape: group.shape,
          path: group.path,
          component: group.component,
          constraint: group.constraint,
          count: group.count,
          more: Math.max(0, group.focus_count - group.focus.length),
          imported,
        },
      })
    }
    for (const item of status.unsupported) {
      out.push({
        code: 'shacl_unsupported',
        severity: 'info',
        key: `${item.construct}|${item.shape}`,
        subjects: [],
        message: `${item.construct} on ${item.shape} is not SHACL Core and was ignored${item.count > 1 ? ` (${item.count}x)` : ''}.`,
      })
    }
    return out
  }, [shaclStatus, dirty])
  const allFindings = useMemo(
    () => [...consistencyFindings, ...shaclFindings, ...findings],
    [consistencyFindings, shaclFindings, findings],
  )
  const ignoredChecks = useIgnoredChecks(selectedId)
  const warningCount = useMemo(
    () => allFindings.filter((f) => f.severity === 'warning' && !ignoredChecks.ignored.has(findingId(f))).length,
    [allFindings, ignoredChecks.ignored],
  )
  const [externalDeclare, setExternalDeclare] = useState<Finding | null>(null)
  const [declareAll, setDeclareAll] = useState<{ namespace: string; classes: string[] } | null>(null)

  /** Declare a class: own namespace at once, another vocabulary after a confirmation (D3). */
  const requestDeclare = useCallback(
    (finding: Finding) => {
      if (finding.own) updateModel((prev) => declareClass(prev, finding.key, vocabStyle))
      else setExternalDeclare(finding)
    },
    [updateModel, vocabStyle],
  )

  const confirmExternalDeclare = useCallback(() => {
    const target = externalDeclare
    setExternalDeclare(null)
    if (target) updateModel((prev) => declareClass(prev, target.key, vocabStyle))
  }, [externalDeclare, updateModel, vocabStyle])

  const confirmDeclareAll = useCallback(() => {
    const target = declareAll
    setDeclareAll(null)
    if (!target) return
    updateModel((prev) => target.classes.reduce((next, cls) => declareClass(next, cls, vocabStyle), prev))
  }, [declareAll, updateModel, vocabStyle])

  const classLabel = useCallback(
    (value: string) => {
      const own = views.find((e) => e.iri === value)?.label
      if (own) return own
      const external = imported.byIri.get(value)
      return external
        ? displayLiteral(external, RDFS_LABEL) || localName(value)
        : compactIri(value, model.document.prefixes)
    },
    [views, imported, model.document.prefixes],
  )

  /**
   * Toggle a property characteristic (UDR-0186 D3). Turning one on in an RDFS-only
   * ontology adds OWL vocabulary, which makes later terms OWL (UDR-0185 D4): ask once
   * first (PRP-0204 Q1). Once added, the ontology is OWL-styled and no longer asks.
   */
  const toggleCharacteristic = useCallback(
    (value: string, characteristic: Characteristic, on: boolean) => {
      if (on && vocabStyle === 'rdfs') {
        setOwlConfirm({ iri: value, characteristic })
        return
      }
      updateModel((prev) => setCharacteristic(prev, value, characteristic, on))
    },
    [updateModel, vocabStyle],
  )

  const confirmOwlCharacteristic = useCallback(() => {
    const target = owlConfirm
    setOwlConfirm(null)
    if (target) updateModel((prev) => setCharacteristic(prev, target.iri, target.characteristic, true))
  }, [owlConfirm, updateModel])

  /** Remove the one rdfs:subClassOf statement an "is a" edge draws (UDR-0185 D3). */
  const removeIsa = useCallback(
    (source: string, target: string) => {
      updateModel((prev) => removeSubclassOf(prev, source, target, graphScope))
      setSelection(null)
    },
    [updateModel, graphScope],
  )

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return
      const { source, target } = connection
      if (linkMode === 'isa') {
        if (source === target) return // a class is trivially its own subclass: nothing to add
        updateModel((prev) => addSubclassOf(prev, source, target))
        setSelection({ kind: 'isa', source, target })
        setRightTab('detail')
        return
      }
      const value = mintIri(baseIri || 'https://chatwalaau.com/ontology/local#', 'relatesTo', takenIris(model))
      updateModel((prev) => createRelationship(prev, value, source, target, vocabStyle))
      setSelection({ kind: 'relationship', iri: value })
      setRightTab('detail')
    },
    [model, baseIri, updateModel, linkMode, vocabStyle],
  )

  const selectResource = useCallback(
    (key: string) => {
      const resource = findResource(model, key)
      const value = resource?.term.type === 'iri' ? resource.term.value : null
      const external = !resource && key.startsWith('<') ? key.slice(1, -1) : null
      if (external && imported.byIri.has(external)) setSelection({ kind: 'imported', iri: external })
      else if (value && resource?.role === 'entity') setSelection({ kind: 'entity', iri: value })
      else if (value && relEdges.some((e) => e.iri === value)) setSelection({ kind: 'relationship', iri: value })
      else if (dataIndex.keys.has(key)) setSelection({ kind: 'individual', key })
      else setSelection({ kind: 'resource', key })
      setRightTab('detail')
    },
    [model, relEdges, dataIndex, imported],
  )

  /** Show a finding's subject where it lives: the canvas for schema terms, else its Detail. */
  const selectFindingSubject = useCallback(
    (term: Term) => {
      const key = termKey(term)
      const resource = findResource(model, key)
      if (!resource) return
      if (resource.role === 'entity' || relEdges.some((e) => termKey(iriTerm(e.iri)) === key)) changeView('schema')
      else if (effectiveView === 'shapes') changeView('data')
      selectResource(key)
    },
    [model, relEdges, changeView, selectResource, effectiveView],
  )

  // ---- The Inferred switch (PRP-0211, UDR-0193 D17-D20) ----

  const changeInferredMode = useCallback((mode: InferredMode) => {
    setInferredMode(mode)
    writeInferredMode(mode)
  }, [])
  const inferredState = inferredMode === 'off' ? undefined : entailmentStatus?.modes[inferredMode]
  // biome-ignore lint/correctness/useExhaustiveDependencies: refetched when the saved version changes
  useEffect(() => {
    if (!open || !selectedId || inferredMode === 'off' || inferredState?.state !== 'ready') {
      if (inferredMode === 'off') setInferredStatements(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch(`/api/ontology/${selectedId}/entailment/statements?mode=${inferredMode}`)
        if (!res.ok) return
        const body = (await res.json()) as InferredStatements
        if (!cancelled) setInferredStatements(body)
      } catch {
        // the views stay as stated
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, selectedId, inferredMode, inferredState?.state, entailmentStatus?.revision])

  // The inferred statements of the graph the canvas shows (UDR-0193 D17 / D20): with unsaved
  // edits only those about things that still exist are kept.
  const shownInferred = useMemo<InferredStatement[]>(() => {
    if (inferredMode === 'off' || !inferredStatements || inferredStatements.mode !== inferredMode) return []
    const inScope = inferredStatements.statements.filter((statement) =>
      graphScope === 'all'
        ? true
        : graphScope === 'default'
          ? statement.graph === null
          : statement.graph !== null && termKey(statement.graph) === termKey(graphScope),
    )
    if (!dirty) return inScope
    const known = new Set(model.resources.map((r) => termKey(r.term)))
    return inScope.filter((statement) => known.has(termKey(statement.s)))
  }, [inferredMode, inferredStatements, graphScope, dirty, model])
  const inferredNote =
    inferredMode === 'off'
      ? null
      : inferredState?.state !== 'ready'
        ? entailmentStateText(inferredState)
        : dirty
          ? 'from the last saved version'
          : inferredStatements?.truncated
            ? 'only the first 20,000 inferred statements'
            : null

  // The Data view's inferred memberships and values (UDR-0193 D19).
  const dataInferred = useMemo(
    () => (shownInferred.length > 0 ? inferredData(shownInferred, (term) => termKey(term)) : null),
    [shownInferred],
  )

  // ---- React Flow derivation ----

  // Neighborhood of the current selection: clicking an ENTITY activates its
  // In/Out edges plus the connected nodes; clicking a RELATIONSHIP activates its
  // endpoint nodes plus the edges flowing in/out of those endpoints. Cleared by
  // clicking another area (the pane).
  const related = useMemo(() => {
    const nodes = new Set<string>()
    const edges = new Set<string>()
    if (
      !selection ||
      selection.kind === 'resource' ||
      selection.kind === 'individual' ||
      selection.kind === 'imported'
    ) {
      return { nodes, edges }
    }
    if (selection.kind === 'isa') {
      nodes.add(selection.source)
      nodes.add(selection.target)
      return { nodes, edges }
    }
    if (selection.kind === 'entity') {
      for (const rel of relEdges) {
        if (rel.source === selection.iri || rel.target === selection.iri) {
          edges.add(rel.id)
          nodes.add(rel.source)
          nodes.add(rel.target)
        }
      }
      nodes.delete(selection.iri) // the primary node has its own stronger style
    } else {
      const own = relEdges.filter((r) => r.iri === selection.iri)
      for (const rel of own) {
        nodes.add(rel.source)
        nodes.add(rel.target)
      }
      for (const other of relEdges) {
        if (other.iri === selection.iri) continue
        if (nodes.has(other.source) || nodes.has(other.target)) edges.add(other.id)
      }
    }
    return { nodes, edges }
  }, [selection, relEdges])

  const positionOf = useCallback(
    (view: EntityView, index: number) => {
      if (view.x !== null && view.y !== null) return { x: view.x, y: view.y }
      return autoPositions.get(view.iri) ?? gridPosition(index)
    },
    [autoPositions],
  )

  const buildNodes = useCallback(
    (): EntityFlowNode[] =>
      views
        .map(
          (entity, index): EntityFlowNode => ({
            id: entity.iri,
            type: 'entity' as const,
            position: positionOf(entity, index),
            // Drag only from the inner disc; the outer ring is the connection zone (UDR-0098 D1).
            dragHandle: `.${ENTITY_DRAG_CLASS}`,
            data: {
              label: entity.label,
              emoji: entity.emoji,
              color: entity.color,
              propertyCount: entity.properties.length,
              keyCount: entity.properties.filter((p) => p.isKey).length,
              isSelected: selection?.kind === 'entity' && selection.iri === entity.iri,
              related: related.nodes.has(entity.iri),
              dimmed: highlight !== null && !highlight.has(entity.iri),
              matched: Boolean(highlight?.has(entity.iri)),
              individualCount: dataIndex.members.get(entity.iri)?.length ?? 0,
              onOpenData: openData,
              shapeCount: shapeTargets.get(entity.iri)?.length ?? 0,
              onOpenShapes: openShapesFor,
              iri: entity.iri,
            },
          }),
        )
        .concat(
          // Faded imported classes (UDR-0189 D8): placed automatically, never dragged,
          // nothing is written for them.
          ghostViews.map(
            (ghost, index): EntityFlowNode => ({
              id: ghost.iri,
              type: 'entity' as const,
              position: positionOf(ghost, views.length + index),
              draggable: false,
              data: {
                label: ghost.label,
                emoji: '',
                color: '',
                propertyCount: ghost.properties.length,
                keyCount: ghost.properties.filter((p) => p.isKey).length,
                isSelected: selection?.kind === 'imported' && selection.iri === ghost.iri,
                related: related.nodes.has(ghost.iri),
                dimmed: highlight !== null && !highlight.has(ghost.iri),
                matched: Boolean(highlight?.has(ghost.iri)),
                individualCount: dataIndex.members.get(ghost.iri)?.length ?? 0,
                onOpenData: openData,
                iri: ghost.iri,
                imported: true,
                sourceName: ghost.sourceName,
              },
            }),
          ),
        ),
    [views, ghostViews, positionOf, selection, related, highlight, dataIndex, openData, shapeTargets, openShapesFor],
  )

  // FLICKER FIX: node positions live in local React Flow state during a drag
  // (applyNodeChanges clones only the dragged node, so the memoized siblings do
  // not re-render per pointer move) and are written back into the model on
  // drag stop. The list is rebuilt synchronously (derived-state-during-render)
  // whenever the underlying data / selection / highlight actually changes, so
  // an ontology switch never shows a stale frame. A rebuild keeps each node's
  // `measured` size: React Flow hides a node without one until it is measured
  // again, and a rebuild that lands between two measurements left the whole
  // canvas hidden (found by the browser suite, PRP-0208).
  const [nodes, setNodes] = useState<EntityFlowNode[]>([])
  const nodesDepsRef = useRef<readonly unknown[] | null>(null)
  const nodesDeps = [views, ghostViews, autoPositions, selection, related, highlight, dataIndex] as const
  if (nodesDepsRef.current === null || nodesDeps.some((dep, index) => dep !== nodesDepsRef.current?.[index])) {
    nodesDepsRef.current = nodesDeps
    const built = buildNodes()
    setNodes((current) => {
      const measured = new Map(current.map((node) => [node.id, node.measured]))
      return built.map((node) => {
        const size = measured.get(node.id)
        return size ? { ...node, measured: size } : node
      })
    })
  }

  const onNodesChange = useCallback((changes: NodeChange<EntityFlowNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current))
  }, [])

  /** Write positions for a user layout action (UDR-0180 D7). */
  const commitPositions = useCallback(
    (positions: Map<string, { x: number; y: number }>) => {
      if (positions.size === 0) return
      updateModel((prev) => {
        let next = prev
        for (const [value, position] of positions) next = setPosition(next, value, position.x, position.y)
        return next
      })
      layoutCommittedRef.current = true
    },
    [updateModel],
  )

  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, _node: EntityFlowNode, draggedNodes: EntityFlowNode[]) => {
      const moved = new Map(draggedNodes.filter((n) => !n.data.imported).map((n) => [n.id, n.position]))
      if (moved.size === 0) return
      // The FIRST layout action of the session saves the picture the user sees:
      // every entity's current position, not only the dragged one. Faded imported
      // classes are never written (UDR-0189 D8).
      const positions = layoutCommittedRef.current
        ? moved
        : new Map(nodes.filter((n) => !n.data.imported).map((n) => [n.id, moved.get(n.id) ?? n.position]))
      commitPositions(positions)
    },
    [nodes, commitPositions],
  )

  // PRP-0211 (UDR-0193 D18): the inferred / derived edges worth drawing, reduced and capped.
  const inferredEdges = useMemo(() => {
    if (shownInferred.length === 0 && inferredMode === 'off') return null
    const entities = new Set([...views.map((v) => v.iri), ...ghostViews.map((g) => g.iri)])
    const properties = viewModel.resources
      .filter((r) => r.role === 'object_property' && r.term.type === 'iri')
      .map((r) => ({
        iri: (r.term as { value: string }).value,
        domains: r.statements
          .filter((s) => s.p === RDFS_DOMAIN && s.o.type === 'iri')
          .map((s) => (s.o as { value: string }).value),
        ranges: r.statements
          .filter((s) => s.p === RDFS_RANGE && s.o.type === 'iri')
          .map((s) => (s.o as { value: string }).value),
        inverses: r.statements
          .filter((s) => s.p === OWL_INVERSE_OF && s.o.type === 'iri')
          .map((s) => (s.o as { value: string }).value),
      }))
    const focus = selection?.kind === 'entity' ? selection.iri : null
    return inferredCanvas(shownInferred, entities, isaEdges, properties, focus)
  }, [shownInferred, inferredMode, views, ghostViews, viewModel, isaEdges, selection])

  const edges = useMemo<Edge[]>(() => {
    // Fan-out bookkeeping: how many relationships share each unordered node pair,
    // and this edge's index within that group (UDR-0098 D2).
    const pairKey = (s: string, t: string) => (s < t ? `${s}|${t}` : `${t}|${s}`)
    const shownIsa = showIsa ? isaEdges : []
    const extra = inferredEdges
      ? [...(showIsa ? inferredEdges.isa : []), ...inferredEdges.same, ...inferredEdges.relationships]
      : []
    const pairTotal = new Map<string, number>()
    for (const rel of [...relEdges, ...shownIsa, ...extra]) {
      const key = pairKey(rel.source, rel.target)
      pairTotal.set(key, (pairTotal.get(key) ?? 0) + 1)
    }
    const pairSeen = new Map<string, number>()
    // "is a" edges (UDR-0185 D3): dashed, open arrow to the superclass, sharing the fan-out.
    const isa = shownIsa.map((edge): Edge => {
      const key = pairKey(edge.source, edge.target)
      const parallelIndex = pairSeen.get(key) ?? 0
      pairSeen.set(key, parallelIndex + 1)
      const isSelected =
        selection?.kind === 'isa' && selection.source === edge.source && selection.target === edge.target
      const isRelated =
        !isSelected && selection?.kind === 'entity' && (edge.source === selection.iri || edge.target === selection.iri)
      const matched = highlight !== null && (highlight.has(edge.source) || highlight.has(edge.target))
      const dimmed = highlight !== null && !matched
      const stroke = isSelected ? '#2563eb' : isRelated ? '#93c5fd' : '#64748b'
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'floating',
        markerEnd: { type: MarkerType.Arrow, color: stroke, width: 18, height: 18 },
        data: {
          label: 'is a',
          stroke,
          strokeWidth: isSelected || isRelated ? 2.5 : 1.5,
          opacity: dimmed ? 0.2 : 1,
          labelColor: isSelected ? '#2563eb' : '#64748b',
          parallelIndex,
          parallelCount: pairTotal.get(key) ?? 1,
          relationshipIri: '',
          isa: true,
        } satisfies RelEdgeData,
      }
    })
    const relationships = relEdges.map((rel) => {
      const key = pairKey(rel.source, rel.target)
      const parallelIndex = pairSeen.get(key) ?? 0
      pairSeen.set(key, parallelIndex + 1)
      const parallelCount = pairTotal.get(key) ?? 1
      const isSelected = selection?.kind === 'relationship' && selection.iri === rel.iri
      const isRelated = !isSelected && related.edges.has(rel.id)
      const matched = highlight !== null && (highlight.has(rel.source) || highlight.has(rel.target))
      const dimmed = highlight !== null && !matched
      const stroke = isSelected ? '#2563eb' : isRelated ? '#93c5fd' : matched ? '#f59e0b' : '#94a3b8'
      return {
        id: rel.id,
        source: rel.source,
        target: rel.target,
        type: 'floating',
        markerEnd: { type: MarkerType.ArrowClosed, color: stroke },
        data: {
          label: `${rel.label} [${CARDINALITY_SYMBOL[rel.cardinality] ?? rel.cardinality}]`,
          stroke,
          strokeWidth: isSelected || isRelated || matched ? 2.5 : 1.5,
          opacity: dimmed ? 0.2 : 1,
          labelColor: isSelected ? '#2563eb' : '#52525b',
          parallelIndex,
          parallelCount,
          relationshipIri: rel.iri,
        } satisfies RelEdgeData,
      }
    })
    // Inferred / derived edges: dotted, faint, never selected for editing (UDR-0193 D18 / D20).
    const labelOf = (iri: string) =>
      relEdges.find((e) => e.iri === iri)?.label ?? compactIri(iri, model.document.prefixes)
    const faint = (
      edge: { id: string; source: string; target: string },
      data: Partial<RelEdgeData>,
      closed: boolean,
    ): Edge => {
      const key = pairKey(edge.source, edge.target)
      const parallelIndex = pairSeen.get(key) ?? 0
      pairSeen.set(key, parallelIndex + 1)
      const isRelated = selection?.kind === 'entity' && (edge.source === selection.iri || edge.target === selection.iri)
      const stroke = isRelated ? '#93c5fd' : '#64748b'
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'floating',
        deletable: false,
        markerEnd: closed ? { type: MarkerType.ArrowClosed, color: stroke } : { type: MarkerType.Arrow, color: stroke },
        data: {
          label: '',
          stroke,
          strokeWidth: 1.5,
          opacity: highlight !== null && !(highlight.has(edge.source) || highlight.has(edge.target)) ? 0.1 : 0.5,
          labelColor: '#64748b',
          parallelIndex,
          parallelCount: pairTotal.get(key) ?? 1,
          relationshipIri: '',
          inferred: 'inferred',
          ...data,
        } satisfies RelEdgeData,
      }
    }
    const inferred: Edge[] = []
    if (inferredEdges) {
      if (showIsa) {
        for (const edge of inferredEdges.isa) {
          inferred.push(faint(edge, { label: 'is a', isa: true, explain: edge.statement }, false))
        }
      }
      for (const edge of inferredEdges.same) {
        inferred.push(faint(edge, { label: 'same as', explain: edge.statement }, false))
      }
      for (const edge of inferredEdges.relationships) {
        inferred.push(
          faint(
            edge,
            edge.derived
              ? {
                  label: `${labelOf(edge.property)} (inverse of ${labelOf(edge.inverseOf ?? '')})`,
                  inferred: 'derived',
                  inverseOf: edge.inverseOf,
                  relationshipIri: edge.property,
                }
              : { label: labelOf(edge.property), explain: edge.statement, relationshipIri: edge.property },
            true,
          ),
        )
      }
    }
    return [...relationships, ...isa, ...inferred]
  }, [relEdges, isaEdges, showIsa, selection, related, highlight, inferredEdges, model.document.prefixes])

  /** elkjs layered layout of the given entities (all when `only` is undefined). */
  const computeLayout = useCallback(
    async (only?: Set<string>) => {
      const { default: ELK } = await import('elkjs/lib/elk.bundled.js')
      const elk = new ELK()
      const ids = [...views, ...ghostViews].map((v) => v.iri).filter((value) => !only || only.has(value))
      const idSet = new Set(ids)
      const result = await elk.layout({
        id: 'root',
        layoutOptions: {
          'elk.algorithm': 'layered',
          'elk.direction': 'RIGHT',
          'elk.spacing.nodeNode': '60',
          'elk.layered.spacing.nodeNodeBetweenLayers': '140',
        },
        children: ids.map((value) => ({ id: value, width: 110, height: 105 })),
        edges: [...relEdges, ...isaEdges]
          .filter((r) => idSet.has(r.source) && idSet.has(r.target))
          .map((r) => ({ id: r.id, sources: [r.source], targets: [r.target] })),
      })
      const positions = new Map<string, { x: number; y: number }>()
      for (const child of result.children ?? []) positions.set(child.id, { x: child.x ?? 0, y: child.y ?? 0 })
      return positions
    },
    [views, ghostViews, relEdges, isaEdges],
  )

  // Entities without a stored position get a TRANSIENT layout when an ontology
  // opens (they used to pile up at 0,0). Nothing is written until the user acts.
  const unpositioned = useMemo(
    () => [...views.filter((v) => v.x === null || v.y === null).map((v) => v.iri), ...ghostViews.map((g) => g.iri)],
    [views, ghostViews],
  )
  const unpositionedKey = unpositioned.join('\n')
  useEffect(() => {
    if (!unpositionedKey) return
    const missing = new Set(unpositionedKey.split('\n'))
    if ([...missing].every((value) => autoPositions.has(value))) return
    let cancelled = false
    const positionedCount = views.length + ghostViews.length - missing.size
    void computeLayout(missing)
      .then((positions) => {
        if (cancelled) return
        // Next to the positioned ones, so a partly laid-out model is not overlapped.
        const offsetX =
          positionedCount > 0 ? Math.max(...views.filter((v) => v.x !== null).map((v) => v.x as number)) + 200 : 0
        setAutoPositions((prev) => {
          const next = new Map(prev)
          for (const [value, p] of positions) if (!next.has(value)) next.set(value, { x: p.x + offsetX, y: p.y })
          return next
        })
      })
      .catch(() => {
        // the grid fallback stays in place
        if (!cancelled) setLayoutFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [unpositionedKey, views, ghostViews, autoPositions, computeLayout])

  // PRP-0211 (UDR-0193 D2): an import ends when its canvas is ready -- loaded, the
  // unpositioned entities laid out (or the layout failed), every node measured.
  useEffect(() => {
    if (!importSettling) return
    const layoutPending = !layoutFailed && unpositioned.some((value) => !autoPositions.has(value))
    const drawn = effectiveView !== 'schema' || views.length === 0 || nodesReady
    if (!loading && !layoutPending && drawn) {
      setImportSettling(false)
      setImporting(false)
    }
  }, [importSettling, layoutFailed, unpositioned, autoPositions, effectiveView, views.length, nodesReady, loading])
  useEffect(() => {
    if (!importSettling) return
    // Never block the editor for long: a canvas that cannot settle ends the wait anyway.
    const timer = window.setTimeout(() => {
      setImportSettling(false)
      setImporting(false)
    }, 20000)
    return () => window.clearTimeout(timer)
  }, [importSettling])

  const resetLayout = useCallback(async () => {
    if (views.length === 0) return
    setLayouting(true)
    try {
      const positions = await computeLayout()
      // Faded imported classes move with the layout but are never written (UDR-0189 D8).
      const ghosts = new Map([...positions].filter(([value]) => ghostRefs.has(value)))
      if (ghosts.size > 0) setAutoPositions((prev) => new Map([...prev, ...ghosts]))
      commitPositions(new Map([...positions].filter(([value]) => !ghostRefs.has(value))))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Auto-layout failed')
    } finally {
      setLayouting(false)
    }
  }, [views.length, computeLayout, commitPositions, ghostRefs])

  const downloadGraph = useCallback(async () => {
    const element = canvasRef.current?.querySelector<HTMLElement>('.react-flow')
    if (!element) return
    try {
      const { toPng } = await import('html-to-image')
      const dataUrl = await toPng(element, {
        backgroundColor: '#ffffff',
        filter: (node) => !(node instanceof HTMLElement && node.classList.contains('ontology-toolbar')),
      })
      const a = document.createElement('a')
      a.href = dataUrl
      a.download = `${catalog.find((c) => c.id === selectedId)?.name || 'ontology'}.png`
      a.click()
    } catch {
      // silent: download is best-effort
    }
  }, [catalog, selectedId])

  // ---- Search ----

  const applyResult = useCallback(
    (result: QueryResult) => {
      setQueryResult(result)
      if (result.kind === 'select') {
        const iris = new Set(
          result.entity_iris.filter((value) => views.some((e) => e.iri === value) || ghostRefs.has(value)),
        )
        setHighlight(iris.size > 0 ? iris : null)
      } else {
        setHighlight(null)
      }
    },
    [views, ghostRefs],
  )

  // "Include imports" (UDR-0189 D5): the switch, else the App Settings default.
  const effectiveIncludeImports = includeImports ?? importsInfo?.include_imports_default ?? true

  /** A new search's abort signal; Stop aborts it and the server stops the search (UDR-0193 D5). */
  const searchSignal = useCallback(() => {
    searchAbortRef.current?.abort()
    const controller = new AbortController()
    searchAbortRef.current = controller
    return controller.signal
  }, [])
  const stopSearch = useCallback(() => {
    searchAbortRef.current?.abort()
    searchAbortRef.current = null
  }, [])
  const searchFailure = useCallback(
    (err: unknown, fallback: string) =>
      err instanceof DOMException && err.name === 'AbortError'
        ? 'The search was stopped.'
        : err instanceof Error
          ? err.message
          : fallback,
    [],
  )

  const runSparql = useCallback(async () => {
    if (!selectedId || !sparql.trim()) return
    setQueryRunning(true)
    setError(null)
    try {
      // The search scope follows the canvas graph selector (UDR-0182 D4, PRP-0200 C5).
      const res = await fetch(`/api/ontology/${selectedId}/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sparql, scope: graphScope, imports: effectiveIncludeImports, entailment }),
        signal: searchSignal(),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) {
        applyResult({ kind: 'error', error: errorDetail(body, 'Query failed') })
        return
      }
      applyResult(body as QueryResult)
    } catch (err) {
      applyResult({ kind: 'error', error: searchFailure(err, 'Query failed') })
    } finally {
      setQueryRunning(false)
    }
  }, [selectedId, sparql, graphScope, applyResult, effectiveIncludeImports, entailment, searchSignal, searchFailure])

  const runNlQuery = useCallback(async () => {
    if (!selectedId || !nlQuestion.trim()) return
    setNlRunning(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${selectedId}/nl-query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: nlQuestion, scope: graphScope, imports: effectiveIncludeImports, entailment }),
        signal: searchSignal(),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) {
        applyResult({ kind: 'error', error: errorDetail(body, 'Natural-language search failed') })
        return
      }
      // The generated SPARQL lands in the editor for refinement (IMPL-6).
      if (typeof body?.sparql === 'string') setSparql(body.sparql)
      applyResult(body as QueryResult)
    } catch (err) {
      applyResult({ kind: 'error', error: searchFailure(err, 'Natural-language search failed') })
    } finally {
      setNlRunning(false)
    }
  }, [
    selectedId,
    nlQuestion,
    graphScope,
    applyResult,
    effectiveIncludeImports,
    entailment,
    searchSignal,
    searchFailure,
  ])

  // ---- SHACL validation: state, groups, polling (PRP-0212, UDR-0194 D9) ----

  const fetchShaclStatus = useCallback(async (id: string) => {
    try {
      let res = await fetch(`/api/ontology/${id}/shacl`)
      if (!res.ok) return
      let body = (await res.json()) as ShaclStatus
      // A missing result no job computes (validation turned on before this version, results
      // deleted, no background jobs) is asked for: queued as a job, or validated now.
      if (body.configured && body.state === 'missing') {
        res = await fetch(`/api/ontology/${id}/shacl?compute=true`)
        if (res.ok) body = (await res.json()) as ShaclStatus
      }
      setShaclStatus(body)
    } catch {
      // the state is a hint; editing still works
    }
  }, [])
  const shaclSettingsKey = JSON.stringify(shaclSettings)
  // biome-ignore lint/correctness/useExhaustiveDependencies: refetched when the saved version or the settings change
  useEffect(() => {
    setShaclStatus(null)
    if (open && selectedId) void fetchShaclStatus(selectedId)
  }, [open, selectedId, revision, shaclSettingsKey, fetchShaclStatus])
  const validating = shaclStatus?.configured && (shaclStatus.state === 'queued' || shaclStatus.state === 'running')
  useEffect(() => {
    if (!open || !selectedId || !validating) return
    const timer = window.setInterval(() => void fetchShaclStatus(selectedId), 2000)
    return () => window.clearInterval(timer)
  }, [open, selectedId, validating, fetchShaclStatus])
  const shaclCounts = useMemo(() => {
    const map = new Map<string, Record<ShaclSeverity, number>>()
    for (const [nt, counts] of Object.entries(shaclStatus?.focus_counts ?? {})) map.set(nt, counts)
    return map
  }, [shaclStatus])
  const shapeResultCounts = useMemo(() => {
    const map = new Map<string, number>()
    for (const group of shaclStatus?.groups ?? []) {
      if (group.shape) map.set(group.shape.nt, (map.get(group.shape.nt) ?? 0) + group.count)
    }
    return map
  }, [shaclStatus])
  const shaclSummary = useMemo(() => {
    const status = shaclStatus
    if (!status?.configured) return 'Validation (SHACL) is not configured for this ontology.'
    if (status.state === 'queued' || status.state === 'running' || status.state === 'missing') return 'Validating...'
    if (status.state !== 'ready') return `Not validated: ${status.message ?? status.state}`
    const c = status.counts ?? { violation: 0, warning: 0, info: 0 }
    const inference = status.inference === 'none' ? 'no inference' : `inference ${status.inference.toUpperCase()}`
    return `Last saved version: ${c.violation} violation(s), ${c.warning} warning(s), ${c.info} note(s) -- ${inference}${
      status.strict ? ', strict mode' : ''
    }`
  }, [shaclStatus])

  // ---- Stored entailment: state, consistency, Explain, inferred views (PRP-0211, UDR-0193) ----

  const fetchEntailmentStatus = useCallback(async (id: string) => {
    try {
      const res = await fetch(`/api/ontology/${id}/entailment`)
      if (!res.ok) return
      setEntailmentStatus((await res.json()) as EntailmentStatus)
    } catch {
      // the state is a hint; searches still work
    }
  }, [])
  // biome-ignore lint/correctness/useExhaustiveDependencies: refetched when the saved version changes
  useEffect(() => {
    setEntailmentStatus(null)
    setConsistency(null)
    setInferredStatements(null)
    if (open && selectedId) void fetchEntailmentStatus(selectedId)
  }, [open, selectedId, revision, fetchEntailmentStatus])
  // Poll while a mode is being computed (after a save, UDR-0193 D7).
  // A missing result is about to be queued when entailment is computed after saves.
  const computing = Object.values(entailmentStatus?.modes ?? {}).some(
    (m) => m?.state === 'queued' || m?.state === 'running' || (m?.state === 'missing' && entailmentStatus?.precompute),
  )
  useEffect(() => {
    if (!open || !selectedId || !computing) return
    const timer = window.setInterval(() => void fetchEntailmentStatus(selectedId), 2000)
    return () => window.clearInterval(timer)
  }, [open, selectedId, computing, fetchEntailmentStatus])
  // The saved version may be ready although no save happened in this session: ask once more
  // shortly after opening when nothing is known yet.
  const owlState = entailmentStatus?.modes['owl-rl']?.state
  // biome-ignore lint/correctness/useExhaustiveDependencies: refetched when the saved version changes
  useEffect(() => {
    if (!open || !selectedId || owlState !== 'ready') return
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch(`/api/ontology/${selectedId}/entailment/findings?mode=owl-rl`)
        if (!res.ok) return
        const body = await res.json()
        if (!cancelled) setConsistency(body)
      } catch {
        // Checks still lists the other findings
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, selectedId, owlState, entailmentStatus?.revision])

  const explain = useCallback(
    async (request: ExplainRequest, scope: GraphScope | 'all' = graphScope) => {
      if (!selectedId) return
      setExplainRequest(request)
      setExplainResult(null)
      setExplainError(null)
      setExplainNote(null)
      setExplainLoading(true)
      setRightTab('explain')
      try {
        const res = await fetch(`/api/ontology/${selectedId}/explain`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            mode: request.mode,
            ...('finding' in request ? { finding: request.finding } : { statement: request.statement }),
            scope,
            imports: effectiveIncludeImports,
          }),
        })
        const body = await res.json().catch(() => null)
        if (!res.ok) {
          setExplainError(errorDetail(body, 'The explanation failed'))
          return
        }
        setExplainResult(body as ExplainResult)
      } catch (err) {
        setExplainError(err instanceof Error ? err.message : 'The explanation failed')
      } finally {
        setExplainLoading(false)
      }
    },
    [selectedId, graphScope, effectiveIncludeImports],
  )
  /** Explain an inferred item of the views (the standard key: all graphs). */
  const explainInferred = useCallback(
    (statement: InferredStatement) => {
      if (inferredMode === 'off') return
      void explain(
        {
          mode: inferredMode,
          statement: { s: statement.s, p: statement.p, o: statement.o, graph: statement.graph },
          label: [statement.s, statement.p, statement.o].map((term) => compactTerm(term, model)).join(' '),
        },
        'all',
      )
    },
    [inferredMode, explain, model],
  )

  // ---- Derived detail-pane data ----

  const selectedEntity = useMemo(
    () => (selection?.kind === 'entity' ? (views.find((e) => e.iri === selection.iri) ?? null) : null),
    [selection, views],
  )
  const selectedRelationshipIri = selection?.kind === 'relationship' ? selection.iri : null
  const entityLabel = useCallback(
    (value: string) =>
      views.find((e) => e.iri === value)?.label ?? ghostViews.find((e) => e.iri === value)?.label ?? localName(value),
    [views, ghostViews],
  )

  // ---- Imports dialog (UDR-0189 D9) ----

  /** The owl:imports statements of the open model, saved or not. */
  const modelImports = useMemo<ModelImport[]>(() => {
    const out: ModelImport[] = []
    for (const resource of model.resources) {
      for (const s of resource.statements) {
        if (s.p === OWL_IMPORTS && s.o.type === 'iri')
          out.push({ iri: s.o.value, subject: resource.term, graph: s.g ?? null })
      }
    }
    return out
  }, [model])
  /** The ontology header in the model: its IRI, else the catalog IRI (D9). */
  const header = useMemo(() => {
    const own = model.resources.find(
      (r) =>
        r.term.type === 'iri' &&
        r.statements.some((s) => s.p === RDF_TYPE && s.o.type === 'iri' && s.o.value === OWL_ONTOLOGY),
    )
    if (own && own.term.type === 'iri') return { iri: own.term.value, exists: true }
    return {
      // PRP-0218 (UDR-0200 D9): the stored ontology IRI when there is one.
      iri: importsInfo?.catalog_iri ?? (selectedId ? irisOf(selectedId, selectedEntry?.namespace).ontology : ''),
      exists: false,
    }
  }, [model, importsInfo, selectedId, selectedEntry?.namespace])
  const importWarnings = useMemo(() => importWarningCount(importsInfo), [importsInfo])
  const openImports = useCallback((iri?: string) => {
    setMapIri(iri ?? null)
    setImportsTab('imports')
    setImportsOpen(true)
  }, [])
  const changeShowImported = useCallback((show: boolean) => {
    setShowImported(show)
    writeShowImported(show)
  }, [])

  // The confirmation before deleting an ontology names the ones that import it (D9).
  useEffect(() => {
    setDeleteImporters(null)
    if (!deleteTarget) return
    let cancelled = false
    void fetch(`/api/ontology/${deleteTarget.id}/importers`)
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => {
        if (!cancelled && body) setDeleteImporters((body.importers ?? []) as { id: string; name: string }[])
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [deleteTarget])

  const inspector = useMemo<InspectorContext>(
    () => ({
      model,
      prefixes: model.document.prefixes,
      diagnostics,
      readOnly: demoBlocked,
      onSelectResource: selectResource,
      graphs,
      targetGraph: model.targetGraph ?? null,
      onAnnotate: demoBlocked ? undefined : requestAnnotate,
      undeclared,
      imported,
    }),
    [model, diagnostics, demoBlocked, selectResource, graphs, requestAnnotate, undeclared, imported],
  )

  /** Statement list callbacks for one resource (all edits funnel through commitEdits). */
  const statementHandlers = useCallback(
    (key: string) => ({
      onEdit: (index: number, statement: StatementInput | null) => commitEdits(key, [{ index, statement }]),
      onAdd: (statement: StatementInput) => commitEdits(key, [{ index: null, statement }]),
    }),
    [commitEdits],
  )

  const busy = saving || importing || creating

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-screen w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-white p-0 text-zinc-900 sm:rounded-none">
          <DialogHeader className="flex shrink-0 flex-row items-center justify-between border-b px-3 py-2 text-left">
            <DialogTitle className="text-sm font-semibold text-zinc-900">Ontology</DialogTitle>
            <DialogDescription className="sr-only">
              Design concept models on a node canvas: add entities and relationships, search with SPARQL or natural
              language, and import or export RDF.
            </DialogDescription>
            <div className="mr-8 flex items-center gap-2">
              {error && (
                <span className="max-w-[480px] truncate text-xs text-red-600" title={error}>
                  {error}
                </span>
              )}
              {staleRevision && selectedId && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => guardDirty(() => void loadOntology(selectedId))}
                  title="Load the version saved elsewhere (your unsaved changes are discarded)">
                  <RotateCcw className="mr-1 h-3.5 w-3.5" /> Reload
                </Button>
              )}
              {shaclNotice && (
                <span className="max-w-[320px] truncate text-xs text-amber-700" title={shaclNotice}>
                  {shaclNotice}
                </span>
              )}
              {dirty && <span className="text-xs text-amber-600">Unsaved changes</span>}
              {mergeUndo && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={undoMerge}
                  title={`Remove the ${mergeUndo.count} ${mergeUndo.kind === 'import' ? 'imported' : 'merged'} statement change(s) -- edits made after the ${mergeUndo.kind} are undone too`}>
                  <RotateCcw className="mr-1 h-3.5 w-3.5" />{' '}
                  {mergeUndo.kind === 'import' ? 'Undo import' : 'Undo merge'}
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => openImports()}
                disabled={!selectedId || loading}
                title="The ontologies this one imports (owl:imports), resolved from the catalog">
                <Import className="mr-1 h-3.5 w-3.5" /> Imports
                {importWarnings > 0 && (
                  <span
                    className="ml-1 rounded-full bg-amber-100 px-1 text-[10px] text-amber-800"
                    title={`${importWarnings} import(s) not resolved`}>
                    {importWarnings}
                  </span>
                )}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setMergeOpen(true)}
                disabled={!selectedId || loading || busy || demoBlocked}
                title={
                  demoBlocked
                    ? 'Disabled in demo mode'
                    : 'Merge a file or another ontology into this one (added as unsaved changes)'
                }>
                <FilePlus2 className="mr-1 h-3.5 w-3.5" /> Merge
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setShaclDialogOpen(true)}
                disabled={!selectedId || loading}
                title={`Validation (SHACL): ${shaclSummary}`}>
                <ShieldCheck className="mr-1 h-3.5 w-3.5" /> Validation
                {shaclStatus?.state === 'ready' && (shaclStatus.counts?.violation ?? 0) > 0 && (
                  <span
                    className="ml-1 rounded-full bg-amber-100 px-1 text-[10px] text-amber-800"
                    title={`${shaclStatus.counts?.violation} SHACL violation(s) in the last saved version`}>
                    {shaclStatus.counts?.violation}
                  </span>
                )}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setDocumentOpen(true)}
                disabled={!selectedId || loading}
                title="Prefixes, base and VERSION of this ontology">
                <Braces className="mr-1 h-3.5 w-3.5" /> Document
              </Button>
              <Button
                size="sm"
                className="h-7"
                onClick={() => void save()}
                disabled={!selectedId || !dirty || busy || demoBlocked}
                title={demoBlocked ? 'Disabled in demo mode' : undefined}>
                {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                Save
              </Button>
            </div>
          </DialogHeader>

          {/* A rebased save that touched statements removed or changed elsewhere
              (409 stale_conflict, UDR-0183 D8): nothing was written; Reload decides. */}
          {saveConflicts && (
            <div
              className="flex max-h-40 shrink-0 items-start gap-2 overflow-y-auto border-b border-red-500/40 bg-red-500/10 px-4 py-2 text-[12px] text-red-700 dark:text-red-400"
              role="alert">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <div>
                  Not saved: {saveConflicts.count} change(s) touch statements that were removed or changed in another
                  tab or by another user. Reload to see the latest version (your unsaved changes are discarded).
                </div>
                <ul className="mt-1 space-y-0.5 font-mono text-[11px]">
                  {saveConflicts.items.map((item) => (
                    <li
                      key={`${item.op} ${termKey(item.statement.s)} ${item.statement.p} ${termKey(item.statement.o)} ${
                        item.statement.g ? termKey(item.statement.g) : ''
                      }`}
                      className="truncate">
                      {item.op}: {compactTerm(item.statement.s, model)} {compactTerm(iriTerm(item.statement.p), model)}{' '}
                      {compactTerm(item.statement.o, model)}
                      {item.statement.g ? ` (graph ${compactTerm(item.statement.g, model)})` : ''}
                    </li>
                  ))}
                  {saveConflicts.count > saveConflicts.items.length && (
                    <li>... and {saveConflicts.count - saveConflicts.items.length} more</li>
                  )}
                </ul>
              </div>
            </div>
          )}

          {/* Demo-mode notice (PRP-0139 / UDR-0122 D2/D4). Reads stay available on
              purpose -- the feature is shown, not hidden. */}
          {demoBlocked && (
            <div className="flex shrink-0 items-start gap-2 border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-[12px] text-amber-700 dark:text-amber-400">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                Demo mode: ontologies are read-only. Creating, importing, renaming, saving and deleting are disabled,
                because this store is shared by everyone using the demo. Browsing, exporting and running SPARQL still
                work.
              </span>
            </div>
          )}

          <ReactFlowProvider>
            <div className="relative flex min-h-0 flex-1" aria-busy={saving || importing ? true : undefined}>
              <PanelLayout
                left={
                  <CatalogPane
                    catalog={catalog}
                    selectedId={selectedId}
                    importing={importing}
                    onSelect={(id) => guardDirty(() => void loadOntology(id))}
                    onCreate={() => setCreateOpen(true)}
                    onImportClick={() => importInputRef.current?.click()}
                    onOpenTrash={() => setTrashOpen(true)}
                    onExport={(entry) => {
                      setExportRefusal(null)
                      setExportNotes(null)
                      setExportTarget(entry)
                    }}
                    onRename={(entry) => {
                      setRenameName(entry.name)
                      setRenameDescription(entry.description ?? '')
                      setRenameTarget(entry)
                    }}
                    onDelete={(entry) => setDeleteTarget(entry)}
                    onReorder={(ids) => void reorderCatalog(ids)}
                    readOnly={demoBlocked}
                  />
                }
                center={
                  effectiveView === 'shapes' && selectedId && !loading ? (
                    <div className="flex min-h-0 flex-1 flex-col">
                      <div className="flex shrink-0 items-center gap-1 border-b bg-zinc-50 px-2 py-1">
                        <ViewSwitch view={effectiveView} onChange={changeView} disabled={false} shapes />
                        <span className="ml-2 text-[11px] text-zinc-500">
                          SHACL shapes, SHACL Core. Edits are saved with Save.
                        </span>
                      </div>
                      <ShapesView
                        model={model}
                        prefixes={model.document.prefixes}
                        graphs={editableShapesGraphs}
                        graph={currentShapesGraph}
                        onGraphChange={(g) => setShapesGraphKey(g ? termKey(g) : '')}
                        graphLabel={(g) => (g ? `graph ${graphName(g)}` : 'the default graph (a shapes ontology)')}
                        readOnly={demoBlocked}
                        base={baseIri || 'https://chatwalaau.com/ontology/local#'}
                        classOptions={[...views.map((v) => v.iri), ...imported.classes]}
                        propertyOptions={[
                          ...model.resources
                            .filter((r) => r.role === 'object_property' || r.role === 'datatype_property')
                            .flatMap((r) => (r.term.type === 'iri' ? [r.term.value] : [])),
                          ...imported.properties.flatMap((r) => (r.term.type === 'iri' ? [r.term.value] : [])),
                        ]}
                        onUpdate={updateModel}
                        resultCounts={shapeResultCounts}
                        onShowResults={(shape) =>
                          setShaclList({
                            title: `Results of ${compactTerm(shape, model)}`,
                            query: { shape: termKey(shape) },
                          })
                        }
                        shapesOntologies={(shaclSettings?.shapes_ontologies ?? []).map((id) => {
                          const entry = catalog.find((e) => e.id === id)
                          return { id, name: entry?.name ?? id, missing: !entry }
                        })}
                        onOpenOntology={(id) => guardDirty(() => void loadOntology(id))}
                        highlight={shapeHighlight}
                        status={shaclSummary}
                        onConfigure={() => setShaclDialogOpen(true)}
                      />
                    </div>
                  ) : effectiveView === 'data' && selectedId && !loading ? (
                    <div className="flex min-h-0 flex-1 flex-col">
                      <div className="flex shrink-0 items-center gap-1 border-b bg-zinc-50 px-2 py-1">
                        <ViewSwitch
                          view={effectiveView}
                          onChange={changeView}
                          disabled={false}
                          shapes={shapesAvailable}
                        />
                        <div className="mx-1 h-4 w-px bg-zinc-200" />
                        <GraphScopeControl
                          graphs={graphs}
                          graphName={graphName}
                          scope={graphScope}
                          onScopeChange={changeScope}
                          onNewGraph={() => setNewGraphOpen(true)}
                          readOnly={demoBlocked}
                          disabled={false}
                        />
                        <span className="ml-2 text-[11px] text-zinc-500">
                          {dataIndex.keys.size} individual{dataIndex.keys.size === 1 ? '' : 's'}
                        </span>
                        <div className="mx-1 h-4 w-px bg-zinc-200" />
                        <InferredSwitch
                          mode={inferredMode}
                          onChange={changeInferredMode}
                          disabled={false}
                          note={inferredNote}
                        />
                      </div>
                      <IndividualsView
                        model={model}
                        index={dataIndex}
                        tree={dataTree}
                        scope={graphScope}
                        ctx={inspector}
                        classLabel={classLabel}
                        group={dataGroup}
                        onGroupChange={setDataGroup}
                        includeSubclasses={includeSubclasses}
                        onIncludeSubclassesChange={setIncludeSubclasses}
                        selectedKey={selection?.kind === 'individual' ? selection.key : null}
                        onSelectIndividual={(key) => {
                          setSelection({ kind: 'individual', key })
                          setRightTab('detail')
                        }}
                        onCommit={commitEdits}
                        onCreate={requestCreateIndividual}
                        inferred={dataInferred}
                        onExplainInferred={explainInferred}
                        shaclCounts={shaclCounts}
                        onShowShaclResults={(term) =>
                          setShaclList({
                            title: `SHACL results of ${compactTerm(term, model)}`,
                            query: { focus: termKey(term) },
                          })
                        }
                        onImportTable={() => setTableOpen(true)}
                        importTableDisabled={
                          demoBlocked ? 'Disabled in demo mode' : loading || busy ? 'Wait for the current action' : null
                        }
                      />
                    </div>
                  ) : (
                    <div ref={canvasRef} className="flex min-h-0 flex-1 flex-col">
                      <CanvasToolbar
                        onAddEntity={addEntity}
                        onResetLayout={() => void resetLayout()}
                        onDownload={() => void downloadGraph()}
                        layouting={layouting}
                        disabled={!selectedId || loading}
                        graphs={graphs}
                        graphName={graphName}
                        scope={graphScope}
                        onScopeChange={changeScope}
                        onNewGraph={() => setNewGraphOpen(true)}
                        readOnly={demoBlocked}
                        linkMode={linkMode}
                        onLinkModeChange={setLinkMode}
                        showIsa={showIsa}
                        onShowIsaChange={changeShowIsa}
                        showImported={imported.classes.size > 0 ? showImported : null}
                        onShowImportedChange={changeShowImported}
                        style={vocabStyle}
                        styleReason={vocabularyStyleReason(vocabStyle, model)}
                        view={effectiveView}
                        onViewChange={changeView}
                        shapesAvailable={shapesAvailable}
                        inferredMode={inferredMode}
                        onInferredModeChange={changeInferredMode}
                        inferredNote={
                          inferredEdges && inferredEdges.hidden > 0
                            ? `${inferredEdges.hidden} more inferred relationships are not drawn`
                            : inferredNote
                        }
                      />
                      <div className="relative min-h-0 flex-1">
                        {inferredMode !== 'off' && selectedId && !loading && <InferredLegend />}
                        {selectedId ? (
                          loading ? (
                            <div className="flex h-full items-center justify-center text-sm text-zinc-500">
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
                            </div>
                          ) : (
                            <ReactFlow
                              key={selectedId}
                              nodes={nodes}
                              edges={edges}
                              nodeTypes={nodeTypes}
                              edgeTypes={edgeTypes}
                              onNodesChange={onNodesChange}
                              onNodeDragStop={onNodeDragStop}
                              onConnect={onConnect}
                              connectionMode={ConnectionMode.Loose}
                              onNodeClick={(_, node) => {
                                setSelection(
                                  node.data.imported
                                    ? { kind: 'imported', iri: node.id }
                                    : { kind: 'entity', iri: node.id },
                                )
                                setRightTab('detail')
                              }}
                              onEdgeClick={(_, edge) => {
                                const data = edge.data as RelEdgeData | undefined
                                // PRP-0211 (UDR-0193 D20): an inferred edge is never edited -- it explains itself.
                                if (data?.inferred) {
                                  if (data.inferred === 'derived') {
                                    setExplainRequest({
                                      mode: inferredMode === 'owl-rl' ? 'owl-rl' : 'rdfs',
                                      finding: '',
                                      label: data.label,
                                    })
                                    setExplainResult(null)
                                    setExplainLoading(false)
                                    setExplainError(null)
                                    setExplainNote(
                                      `Derived from owl:inverseOf: ${data.label}. The edge mirrors the domain and range of ${compactIri(data.inverseOf ?? '', model.document.prefixes)} (stated); OWL 2 RL itself gives an inverse property no domain or range.`,
                                    )
                                    setRightTab('explain')
                                  } else if (data.explain && inferredMode !== 'off') {
                                    void explain(
                                      {
                                        mode: inferredMode,
                                        statement: data.explain,
                                        label: [data.explain.s, data.explain.p, data.explain.o]
                                          .map((term) => compactTerm(term, model))
                                          .join(' '),
                                      },
                                      'all',
                                    )
                                  }
                                  return
                                }
                                if (data?.isa) setSelection({ kind: 'isa', source: edge.source, target: edge.target })
                                else setSelection({ kind: 'relationship', iri: data?.relationshipIri ?? edge.id })
                                setRightTab('detail')
                              }}
                              onPaneClick={() => setSelection(null)}
                              fitView
                              minZoom={0.1}
                              proOptions={{ hideAttribution: false }}>
                              <Background gap={16} />
                              <NodesReady onChange={setNodesReady} />
                            </ReactFlow>
                          )
                        ) : (
                          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-zinc-500">
                            Select an ontology on the left, or create / import one to start designing.
                          </div>
                        )}
                      </div>
                    </div>
                  )
                }
                right={
                  <div className="flex min-h-0 flex-1 flex-col">
                    <PanelTabStrip active={rightTab} onSelect={setRightTab} checksBadge={warningCount} />
                    {rightTab === 'detail' ? (
                      selection?.kind === 'isa' ? (
                        <IsaDetail
                          source={selection.source}
                          target={selection.target}
                          exists={isaEdges.some((e) => e.source === selection.source && e.target === selection.target)}
                          entityLabel={entityLabel}
                          readOnly={demoBlocked}
                          onSelectEntity={(value) => setSelection({ kind: 'entity', iri: value })}
                          onRemove={() => removeIsa(selection.source, selection.target)}
                        />
                      ) : selection?.kind === 'imported' ? (
                        <ImportedDetail
                          iri={selection.iri}
                          imported={imported}
                          prefixes={model.document.prefixes}
                          formatTerm={(term) => compactTerm(term, model)}
                          onOpenSource={(id) => guardDirty(() => void loadOntology(id))}
                        />
                      ) : selection?.kind === 'individual' ? (
                        <IndividualDetail
                          key={selection.key}
                          resourceKey={selection.key}
                          model={model}
                          index={dataIndex}
                          ctx={inspector}
                          classLabel={classLabel}
                          classOptions={entityOptions}
                          onCommit={(edits) => commitEdits(selection.key, edits)}
                          {...statementHandlers(selection.key)}
                          onOpenClass={openData}
                          onDelete={() => requestDeleteIndividual(selection.key)}
                          inferred={
                            dataInferred
                              ? {
                                  types: dataInferred.types.get(selection.key) ?? [],
                                  values: dataInferred.values.get(selection.key) ?? [],
                                  stale: dirty,
                                  onExplain: explainInferred,
                                }
                              : undefined
                          }
                        />
                      ) : selection?.kind === 'resource' ? (
                        <ResourceDetail
                          resourceKey={selection.key}
                          ctx={inspector}
                          {...statementHandlers(selection.key)}
                          onDelete={() => {
                            updateModel((prev) => removeResource(prev, selection.key))
                            setSelection(null)
                          }}
                        />
                      ) : (
                        <DetailPane
                          model={model}
                          entity={selectedEntity}
                          relationshipIri={selectedRelationshipIri}
                          relationships={relEdges}
                          entityLabel={entityLabel}
                          inspector={inspector}
                          statementHandlers={statementHandlers}
                          onSetDisplayed={setDisplayed}
                          onSetFirst={setFirst}
                          onToggleKey={(prop, on) => updateModel((prev) => setIsKey(prev, prop, on))}
                          onAddProperty={(entity) => {
                            const value = mintIri(baseIri || `${entity}_`, 'property', takenIris(model))
                            updateModel((prev) => createDatatypeProperty(prev, value, entity, vocabStyle))
                          }}
                          onRemoveProperty={(prop, entity) =>
                            updateModel((prev) => removePropertyFromEntity(prev, prop, entity))
                          }
                          onDeleteEntity={requestDeleteEntity}
                          onDeleteRelationship={deleteRelationship}
                          onSelectRelationship={(value) => setSelection({ kind: 'relationship', iri: value })}
                          onSelectResource={selectResource}
                          onToggleCharacteristic={toggleCharacteristic}
                        />
                      )
                    ) : rightTab === 'history' ? (
                      <HistoryPanel
                        key={selectedId ?? 'none'}
                        ontologyId={selectedId}
                        revision={revision}
                        readOnly={demoBlocked}
                        formatTerm={(term) => compactTerm(term, model)}
                        guardDirty={guardDirty}
                        onRestored={() => {
                          if (!selectedId) return
                          void (async () => {
                            await loadOntology(selectedId, { keepView: true })
                            await fetchCatalog()
                          })()
                        }}
                      />
                    ) : rightTab === 'resources' ? (
                      <ResourcesPane
                        ctx={inspector}
                        selectedKey={selection?.kind === 'resource' ? selection.key : null}
                        onCreate={() => setNewResourceOpen(true)}
                        onOpenData={() => changeView('data')}
                        onOpenChecks={() => setRightTab('checks')}
                      />
                    ) : rightTab === 'checks' ? (
                      <OntologyChecksPane
                        findings={allFindings}
                        ignored={ignoredChecks.ignored}
                        onIgnore={ignoredChecks.ignore}
                        onUnignore={ignoredChecks.unignore}
                        prefixes={model.document.prefixes}
                        readOnly={demoBlocked || !selectedId}
                        onSelect={selectFindingSubject}
                        onDeclare={requestDeclare}
                        onDeclareAll={(namespace, classes) => setDeclareAll({ namespace, classes })}
                        onRemoveEdge={(edge: CheckEdge) => updateModel((prev) => removeCheckEdge(prev, edge))}
                        onOpenImports={(iri) => openImports(iri)}
                        onExplain={(finding) =>
                          finding.explainKey &&
                          void explain({ mode: 'owl-rl', finding: finding.explainKey, label: finding.message }, 'all')
                        }
                        onShowShape={(finding) =>
                          finding.shacl?.shape && openShape(finding.shacl.shape.nt, finding.shacl.path)
                        }
                        onShaclMore={(finding) => {
                          const info = finding.shacl
                          if (!info) return
                          const query: Record<string, string> = { severity: info.severity, component: info.component }
                          if (info.shape) query.shape = info.shape.nt
                          if (info.path) query.path = info.path
                          setShaclList({ title: finding.message, query })
                        }}
                      />
                    ) : rightTab === 'explain' ? (
                      <ExplainPane
                        request={explainRequest}
                        result={explainResult}
                        loading={explainLoading}
                        error={explainError}
                        note={explainNote}
                        onSelect={selectFindingSubject}
                      />
                    ) : (
                      <SearchPane
                        disabled={!selectedId}
                        sparql={sparql}
                        onSparqlChange={setSparql}
                        onRunSparql={() => void runSparql()}
                        queryRunning={queryRunning}
                        nlQuestion={nlQuestion}
                        onNlQuestionChange={setNlQuestion}
                        onRunNl={() => void runNlQuery()}
                        nlRunning={nlRunning}
                        result={queryResult}
                        scopeLabel={
                          graphs.length === 0
                            ? null
                            : graphScope === 'all'
                              ? 'all graphs'
                              : graphScope === 'default'
                                ? 'the default graph'
                                : graphName(graphScope)
                        }
                        inspector={inspector}
                        highlightActive={highlight !== null}
                        onClearHighlight={() => setHighlight(null)}
                        imports={
                          (importsInfo?.closure.length ?? 0) > 0
                            ? { include: effectiveIncludeImports, count: importsInfo?.closure.length ?? 0 }
                            : null
                        }
                        onIncludeImportsChange={setIncludeImports}
                        entailment={entailment}
                        onEntailmentChange={setEntailment}
                        entailmentState={
                          entailment === 'simple' ? null : entailmentStateText(entailmentStatus?.modes[entailment])
                        }
                        onStop={stopSearch}
                        onExplain={(row, mode) =>
                          void explain({
                            mode,
                            statement: { s: row.s, p: row.p, o: row.o, graph: row.graph },
                            label: [row.s, row.p, row.o].map((term) => compactTerm(term, model)).join(' '),
                          })
                        }
                      />
                    )}
                  </div>
                }
              />

              {/* Blocking indicator while a mutation is in flight (PRP-0090 precedent); a file
                  import lasts until its canvas is drawn (PRP-0211, UDR-0193 D2). */}
              {(saving || importing) && (
                <BusyOverlay label={saving ? 'Saving...' : `Importing ${importLabel ?? 'the file'}...`} />
              )}
            </div>
          </ReactFlowProvider>

          <input
            ref={importInputRef}
            type="file"
            accept={IMPORT_ACCEPT}
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              e.target.value = ''
              if (file) guardDirty(() => void importOntology(file))
            }}
          />
        </DialogContent>
      </Dialog>

      <DeletedOntologiesDialog
        open={trashOpen}
        onOpenChange={setTrashOpen}
        readOnly={demoBlocked}
        onRestored={(id) => {
          void fetchCatalog()
          guardDirty(() => void loadOntology(id))
        }}
      />

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={(o) => !creating && setCreateOpen(o)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New ontology</DialogTitle>
            <DialogDescription className="sr-only">Enter a name for the new ontology.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <label htmlFor="ontology-name" className="mb-1 block text-xs font-medium text-muted-foreground">
                Name
              </label>
              <Input
                id="ontology-name"
                value={createName}
                onChange={(e) => setCreateName(e.target.value)}
                placeholder="e.g. Plant Asset Model"
              />
            </div>
            <div>
              <label htmlFor="ontology-description" className="mb-1 block text-xs font-medium text-muted-foreground">
                Description (used by the assistant to pick this ontology)
              </label>
              <textarea
                id="ontology-description"
                value={createDescription}
                onChange={(e) => setCreateDescription(e.target.value)}
                rows={3}
                className="w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                placeholder="What domain does this concept model describe?"
              />
            </div>
            <NewOntologyIriFields
              base={iriBase}
              baseIsDefault={iriBaseIsDefault}
              shortName={createShortName}
              onShortName={setCreateShortName}
              customIri={createCustomIri}
              onCustomIri={setCreateCustomIri}
              useCustom={createUseCustom}
              onUseCustom={setCreateUseCustom}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={creating}>
              Cancel
            </Button>
            <Button
              onClick={() => void createOntology()}
              disabled={
                creating ||
                !createName.trim() ||
                !newOntologyIriReady({
                  useCustom: createUseCustom,
                  shortName: createShortName,
                  customIri: createCustomIri,
                })
              }>
              {creating ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* PRP-0212 (UDR-0194 D1): validation settings of the selected ontology. */}
      {tableOpen && selectedId && (
        <TableImportDialog
          ontologyId={selectedId}
          ontologyName={selectedEntry?.name ?? selectedId}
          model={model}
          baseIri={individualsIri || baseIri}
          revision={revision}
          dirty={dirty}
          onSave={() => void save()}
          onDiscard={() => void loadOntology(selectedId)}
          defaultClass={dataGroup?.kind === 'class' ? dataGroup.iri : null}
          graphName={graphName}
          readOnly={demoBlocked}
          onClose={() => setTableOpen(false)}
          onApply={applyTablePreview}
        />
      )}
      {mergeOpen && selectedId && (
        <MergeDialog
          ontologyId={selectedId}
          ontologyName={selectedEntry?.name ?? selectedId}
          model={model}
          catalog={catalog}
          shapesGraphs={shaclSettings?.shapes_graphs ?? []}
          graphName={graphName}
          onClose={() => setMergeOpen(false)}
          onMerge={(plan) => applyMergePlan(plan)}
        />
      )}
      {shaclDialogOpen && selectedId && (
        <ShaclSettingsDialog
          ontologyId={selectedId}
          settings={shaclSettings}
          catalog={catalog}
          graphs={graphs}
          graphName={graphName}
          status={shaclSummary}
          readOnly={demoBlocked}
          onClose={() => setShaclDialogOpen(false)}
          onSaved={async () => {
            await fetchCatalog()
            setShaclDialogOpen(false)
          }}
        />
      )}

      {/* PRP-0212 (UDR-0194 D13): strict mode refused the save. */}
      <Dialog open={strictRefusal !== null} onOpenChange={(o) => !o && setStrictRefusal(null)}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Not saved: SHACL violations</DialogTitle>
            <DialogDescription>{strictRefusal?.message}</DialogDescription>
          </DialogHeader>
          <ShaclResultsTable
            items={strictRefusal?.results ?? []}
            onSelect={(term) => {
              setStrictRefusal(null)
              selectFindingSubject(term)
            }}
          />
          {strictRefusal && strictRefusal.count > strictRefusal.results.length && (
            <p className="text-xs text-zinc-500">... and {strictRefusal.count - strictRefusal.results.length} more</p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setStrictRefusal(null)}>
              Fix
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setStrictRefusal(null)
                void save(true)
              }}
              title="Save this version although it has SHACL violations (recorded in History)">
              Save anyway
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* PRP-0212 (UDR-0194 D9): the stored results of one group, shape or node. */}
      {shaclList && selectedId && (
        <ShaclResultsDialog
          ontologyId={selectedId}
          title={shaclList.title}
          query={shaclList.query}
          stale={dirty}
          onClose={() => setShaclList(null)}
          onSelect={(term) => {
            setShaclList(null)
            selectFindingSubject(term)
          }}
        />
      )}

      {/* Rename (name + description); id and projection unchanged (PRP-0116, CTR-0171). */}
      <Dialog open={renameTarget !== null} onOpenChange={(o) => !renaming && !o && setRenameTarget(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Rename ontology</DialogTitle>
            <DialogDescription className="sr-only">Enter a new name for this ontology.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <label htmlFor="ontology-rename-name" className="mb-1 block text-xs font-medium text-muted-foreground">
                Name
              </label>
              <Input
                id="ontology-rename-name"
                value={renameName}
                onChange={(e) => setRenameName(e.target.value)}
                placeholder="e.g. Plant Asset Model"
              />
            </div>
            <div>
              <label
                htmlFor="ontology-rename-description"
                className="mb-1 block text-xs font-medium text-muted-foreground">
                Description (used by the assistant to pick this ontology)
              </label>
              <textarea
                id="ontology-rename-description"
                value={renameDescription}
                onChange={(e) => setRenameDescription(e.target.value)}
                rows={3}
                className="w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                placeholder="What domain does this concept model describe?"
              />
            </div>
            {renameTarget && (
              <OntologyIrisBlock
                key={renameTarget.id}
                id={renameTarget.id}
                namespace={renameTarget.namespace}
                base={iriBase}
                baseIsDefault={iriBaseIsDefault}
                readOnly={demoBlocked}
                onSet={(payload) => setOntologyIri(renameTarget.id, payload)}
              />
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRenameTarget(null)} disabled={renaming}>
              Cancel
            </Button>
            <Button onClick={() => void renameOntology()} disabled={renaming || !renameName.trim()}>
              {renaming ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation + blocking indicator (operator requirement). */}
      <ImportsDialog
        open={importsOpen}
        onOpenChange={setImportsOpen}
        ontologyId={selectedId}
        info={importsInfo}
        loading={importsLoading}
        modelImports={modelImports}
        selfIri={header.iri}
        catalog={catalog}
        readOnly={demoBlocked}
        prefixes={model.document.prefixes}
        tab={importsTab}
        onTabChange={setImportsTab}
        mapIri={mapIri}
        onAdd={(target) => updateModel((prev) => addImport(prev, header.iri, target, header.exists))}
        onRemove={(m) => updateModel((prev) => removeImport(prev, m.subject, m.iri, m.graph))}
        onMap={(iri) => {
          setMapIri(iri)
          setImportsTab('mapping')
        }}
        onMapSaved={() => {
          if (selectedId) void fetchImports(selectedId)
        }}
        onOpenOntology={(id) =>
          guardDirty(() => {
            setImportsOpen(false)
            void loadOntology(id)
          })
        }
      />

      <AlertDialog open={deleteTarget !== null} onOpenChange={(o) => !o && !deleting && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete ontology?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{deleteTarget?.name}&quot; will be removed from the catalog (a backup of its file is kept on disk).
              {deleteImporters && deleteImporters.length > 0 && (
                <span className="mt-2 block text-amber-700" data-delete-importers>
                  Imported by {deleteImporters.map((i) => i.name).join(', ')}: their imports of it will no longer
                  resolve.
                </span>
              )}
              This action cannot be undone from the app.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                void confirmDelete()
              }}
              disabled={deleting}>
              {deleting ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Delete'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Unsaved-changes confirmation */}
      <AlertDialog open={pendingDiscard !== null} onOpenChange={(o) => !o && setPendingDiscard(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              This ontology has unsaved changes. Discard them and continue?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const action = pendingDiscard
                setPendingDiscard(null)
                setDirty(false)
                action?.()
              }}>
              Discard
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Entity delete: references from other resources are KEPT and counted (PRP-0198 Q3). */}
      {/* + Individual (UDR-0187 D6): one rdf:type statement in the target graph, plus a label when given. */}
      <Dialog open={newIndividualClass !== null} onOpenChange={(o) => !o && setNewIndividualClass(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-sm">
              New individual of {newIndividualClass ? classLabel(newIndividualClass) : ''}
            </DialogTitle>
            <DialogDescription className="text-xs">
              Adds one statement, &quot;rdf:type {newIndividualClass ? classLabel(newIndividualClass) : ''}&quot;
              {model.targetGraph ? ` in the graph ${graphName(model.targetGraph)}` : ''}, plus its label when you enter
              one.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <div>
              <span className={fieldLabel}>Name (used for the IRI)</span>
              <input
                className={fieldInput}
                aria-label="Individual name"
                value={newIndividualName}
                placeholder="rex"
                onChange={(e) => setNewIndividualName(e.target.value)}
              />
            </div>
            <div>
              <span className={fieldLabel}>Label (optional)</span>
              <input
                className={fieldInput}
                aria-label="Individual label"
                value={newIndividualLabel}
                placeholder="Rex"
                onChange={(e) => setNewIndividualLabel(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') confirmCreateIndividual()
                }}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setNewIndividualClass(null)}>
              Cancel
            </Button>
            <Button size="sm" onClick={confirmCreateIndividual}>
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete an individual with the statements referring to it (UDR-0187 D7). */}
      {/* Declaring a class from another vocabulary (PRP-0206 Q1, UDR-0188 D3). */}
      <AlertDialog open={externalDeclare !== null} onOpenChange={(o) => !o && setExternalDeclare(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Declare a class from another vocabulary?</AlertDialogTitle>
            <AlertDialogDescription>
              {externalDeclare ? compactIri(externalDeclare.key, model.document.prefixes) : ''} belongs to another
              vocabulary ({externalDeclare?.namespace}). Declaring it here adds one statement and makes this ontology
              define it; usually such classes are reused from their own vocabulary instead.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmExternalDeclare}>Declare anyway</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={declareAll !== null} onOpenChange={(o) => !o && setDeclareAll(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Declare {declareAll?.classes.length ?? 0} classes?</AlertDialogTitle>
            <AlertDialogDescription>
              Adds one {vocabStyle === 'rdfs' ? 'rdfs:Class' : 'owl:Class'} statement for each undeclared class of{' '}
              {declareAll?.namespace}
              {model.targetGraph ? ` in the graph ${graphName(model.targetGraph)}` : ''}. They will be drawn on the
              canvas. You can undo this from History after saving.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmDeclareAll}>Declare</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={individualDeleteTarget !== null} onOpenChange={(o) => !o && setIndividualDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete individual?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{individualDeleteTarget?.label}&quot; and its {individualDeleteTarget?.refs.own ?? 0} statement
              {individualDeleteTarget?.refs.own === 1 ? '' : 's'} will be removed
              {individualDeleteTarget && individualDeleteTarget.refs.references > 0
                ? `, together with ${individualDeleteTarget.refs.references} statement${
                    individualDeleteTarget.refs.references === 1 ? '' : 's'
                  } of ${individualDeleteTarget.refs.resources} other resource${
                    individualDeleteTarget.refs.resources === 1 ? '' : 's'
                  } that refer to it`
                : ''}{' '}
              (in every graph).
              {individualDeleteTarget && individualDeleteTarget.refs.keptAnnotations > 0
                ? ` ${individualDeleteTarget.refs.keptAnnotations} annotation${
                    individualDeleteTarget.refs.keptAnnotations === 1 ? '' : 's'
                  } (reifiers) about these statements keep their other statements.`
                : ''}{' '}
              You can undo this from History after saving.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmDeleteIndividual}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={entityDeleteTarget !== null} onOpenChange={(o) => !o && setEntityDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete entity?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{entityDeleteTarget?.label}&quot; and its own statements will be removed. Properties and
              relationships that only belonged to it go with it; shared ones keep their other entities.
              {entityDeleteTarget && entityDeleteTarget.refs > 0
                ? ` ${entityDeleteTarget.refs} statement${entityDeleteTarget.refs === 1 ? '' : 's'} of other resources refer to it and will be kept (find them in the Resources tab).`
                : ' No other resource refers to it.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmDeleteEntity}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* A characteristic in an RDFS-only ontology adds OWL vocabulary: ask once (PRP-0204 Q1). */}
      <AlertDialog open={owlConfirm !== null} onOpenChange={(o) => !o && setOwlConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Add OWL vocabulary?</AlertDialogTitle>
            <AlertDialogDescription>
              This ontology is written in RDFS only. Turning on{' '}
              {CHARACTERISTICS.find((c) => c.key === owlConfirm?.characteristic)?.label ?? 'this characteristic'} adds
              the statement rdf:type owl:
              {CHARACTERISTICS.find((c) => c.key === owlConfirm?.characteristic)?.iri.split('#')[1] ?? ''}. After that,
              new entities, relationships and attributes are created as OWL terms. Continue?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmOwlCharacteristic}>Add OWL vocabulary</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Editing an annotated (reified) statement: reifiers follow by default (PRP-0198 Q4). */}
      <AlertDialog open={pendingReified !== null} onOpenChange={(o) => !o && setPendingReified(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Update the annotations too?</AlertDialogTitle>
            <AlertDialogDescription>
              This statement is annotated by {pendingReified?.reifierCount ?? 0} reifier
              {pendingReified?.reifierCount === 1 ? '' : 's'} (rdf:reifies). Let them follow the edit, or keep them
              pointing to the statement as it was?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel edit</AlertDialogCancel>
            <Button variant="outline" onClick={() => resolvePendingReified(false)}>
              Keep them on the old statement
            </Button>
            <AlertDialogAction onClick={() => resolvePendingReified(true)}>Follow the edit</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Deleting an annotated statement: the default is to KEEP the annotations (PRP-0200 Q3). */}
      <AlertDialog open={pendingDelete !== null} onOpenChange={(o) => !o && setPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete its annotations too?</AlertDialogTitle>
            <AlertDialogDescription>
              This statement is annotated by {pendingDelete?.reifierCount ?? 0} reifier
              {pendingDelete?.reifierCount === 1 ? '' : 's'} (rdf:reifies). Kept annotations stay valid RDF: they go on
              describing the statement, which is no longer asserted.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button variant="outline" onClick={() => resolvePendingDelete(true)}>
              Delete the annotations too
            </Button>
            <AlertDialogAction onClick={() => resolvePendingDelete(false)}>Keep the annotations</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AnnotateDialog
        open={annotateTarget !== null}
        ctx={inspector}
        subject={annotateTarget ? (findResource(model, annotateTarget.key)?.term ?? null) : null}
        statement={
          annotateTarget ? (findResource(model, annotateTarget.key)?.statements[annotateTarget.index] ?? null) : null
        }
        suggestedIri={annotateTarget?.iri ?? ''}
        onCancel={() => setAnnotateTarget(null)}
        onCreate={(reifierIri, annotation) => {
          const target = annotateTarget
          setAnnotateTarget(null)
          if (!target) return
          updateModel((prev) => annotateStatement(prev, target.key, target.index, reifierIri, annotation))
          setSelection({ kind: 'resource', key: termKey(iriTerm(reifierIri)) })
          setRightTab('detail')
        }}
      />

      {/* New named graph (UDR-0182 D1): it exists in the file once a statement is in it. */}
      <Dialog open={newGraphOpen} onOpenChange={(o) => !o && setNewGraphOpen(false)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New named graph</DialogTitle>
            <DialogDescription>
              Statements you add while this graph is selected go into it. A graph with no statements is not saved, and
              an ontology with a named graph is stored as TriG.
            </DialogDescription>
          </DialogHeader>
          <div>
            <label htmlFor="ontology-new-graph" className="mb-1 block text-xs font-medium text-muted-foreground">
              Graph IRI
            </label>
            <Input
              id="ontology-new-graph"
              value={newGraphIri}
              onChange={(e) => setNewGraphIri(e.target.value)}
              placeholder={`${baseIri || 'https://example.org/'}graph1`}
            />
            {newGraphIri.trim() && !/^[A-Za-z][A-Za-z0-9+.-]*:\S+$/.test(newGraphIri.trim()) && (
              <span className="text-[10px] text-red-600">Enter an absolute IRI.</span>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setNewGraphOpen(false)}>
              Cancel
            </Button>
            <Button disabled={!/^[A-Za-z][A-Za-z0-9+.-]*:\S+$/.test(newGraphIri.trim())} onClick={addGraph}>
              Add graph
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Export: six formats; a format that cannot carry the ontology says why (UDR-0182 D7). */}
      <Dialog open={exportTarget !== null} onOpenChange={(o) => !o && exporting === null && setExportTarget(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Export {exportTarget?.name}</DialogTitle>
            <DialogDescription>
              Every export is read back and compared before you get it. A format that cannot hold everything in the
              ontology is refused with the reason, never written with parts left out.
            </DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-3 gap-1.5">
            {EXPORT_FORMATS.map((format) => (
              <Button
                key={format.name}
                variant="outline"
                size="sm"
                className="h-auto flex-col items-start gap-0 py-1.5 text-left"
                disabled={exporting !== null}
                onClick={() => exportTarget && void exportOntology(exportTarget, format)}>
                <span className="flex items-center gap-1 text-xs font-medium">
                  {exporting === format.name ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                  {format.label}
                </span>
                <span className="text-[10px] font-normal text-zinc-500">
                  {format.extension} {format.graphs ? '- named graphs' : '- one graph'}
                </span>
              </Button>
            ))}
          </div>
          {exportNotes && (
            <p className="flex items-start gap-1 text-[11px] text-zinc-600">
              <Info className="mt-0.5 h-3 w-3 shrink-0" /> Downloaded. {exportNotes}
            </p>
          )}
          {exportRefusal && (
            <div className="space-y-1.5 rounded border border-amber-300 bg-amber-50 p-2 text-[11px] text-amber-800">
              <p className="flex items-start gap-1 font-medium">
                <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                {exportRefusal.message}
              </p>
              {exportRefusal.reasons.map((reason) => (
                <div key={reason.code}>
                  <p>
                    {reason.count} {EXPORT_REASON_TEXT[reason.code] ?? reason.code}
                    {reason.examples.length > 0 ? ', for example:' : '.'}
                  </p>
                  <ul className="ml-3 list-disc">
                    {reason.examples.map((example) => (
                      <li
                        key={`${termKey(example.s)} ${example.p} ${termKey(example.o)} ${termKey(example.g ?? example.s)}`}>
                        {exportTarget?.id === selectedId ? (
                          <button
                            type="button"
                            className="text-left text-blue-700 hover:underline"
                            onClick={() => {
                              setExportTarget(null)
                              selectResource(termKey(example.s))
                            }}>
                            {compactTerm(example.s, model)} {compactTerm(iriTerm(example.p), model)}{' '}
                            {compactTerm(example.o, model)}
                            {example.g ? ` (${graphName(example.g)})` : ''}
                          </button>
                        ) : (
                          <span>
                            {compactTerm(example.s, model)} {compactTerm(iriTerm(example.p), model)}{' '}
                            {compactTerm(example.o, model)}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setExportTarget(null)} disabled={exporting !== null}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DocumentDialog
        open={documentOpen}
        document={model.document}
        readOnly={demoBlocked}
        usesRdf12={usesRdf12(model)}
        onCancel={() => setDocumentOpen(false)}
        onApply={(document: OntologyDocument) => {
          setDocumentOpen(false)
          updateModel((prev) => ({ ...prev, document }))
        }}
      />

      <NewResourceDialog
        open={newResourceOpen}
        model={model}
        prefixes={model.document.prefixes}
        onCancel={() => setNewResourceOpen(false)}
        onCreate={(term, statement) => {
          setNewResourceOpen(false)
          commitEdits(termKey(term), [{ index: null, statement }])
          selectResource(termKey(term))
        }}
      />
    </>
  )
}

// ---- Pane layout (three freely resizable panes) ------------------------------

function PanelLayout(props: { left: React.ReactNode; center: React.ReactNode; right: React.ReactNode }) {
  return (
    <PanelGroup direction="horizontal" className="min-h-0 flex-1">
      <Panel defaultSize={18} minSize={12} className="flex min-w-0 flex-col">
        {props.left}
      </Panel>
      <PanelResizeHandle className="w-px bg-zinc-200 transition-colors hover:bg-blue-400 data-[resize-handle-state=drag]:bg-blue-500" />
      <Panel minSize={30} className="flex min-w-0 flex-col">
        {props.center}
      </Panel>
      <PanelResizeHandle className="w-px bg-zinc-200 transition-colors hover:bg-blue-400 data-[resize-handle-state=drag]:bg-blue-500" />
      <Panel defaultSize={26} minSize={16} className="flex min-w-0 flex-col">
        {props.right}
      </Panel>
    </PanelGroup>
  )
}

/**
 * Recognise the UDR-0122 typed 409 refusal (PRP-0139).
 *
 * The write buttons are already disabled when `demoBlocked` is set, so this path
 * only runs when the guard engages AFTER the modal was opened -- another tab, or
 * an operator flipping DEMO_MODE on a running server. Without it the operator
 * would see the raw slug instead of the notice.
 */
async function demoRefusal(res: Response): Promise<string | null> {
  if (res.status !== 409) return null
  const body = await res
    .clone()
    .json()
    .catch(() => null)
  const detail = body?.detail
  return detail?.error === 'demo_mode' ? (detail.message ?? 'Editing is disabled in demo mode.') : null
}

// ---- Left pane: the catalog ---------------------------------------------------

function CatalogPane(props: {
  catalog: CatalogEntry[]
  selectedId: string | null
  importing: boolean
  onSelect: (id: string) => void
  onCreate: () => void
  onImportClick: () => void
  onOpenTrash: () => void
  onExport: (entry: CatalogEntry) => void
  onRename: (entry: CatalogEntry) => void
  onDelete: (entry: CatalogEntry) => void
  /** The catalog ids in the new order (PRP-0213, UDR-0195 D8). */
  onReorder: (ids: string[]) => void
  readOnly: boolean
}) {
  // Drag and drop with a handle, and the keyboard (Space, arrows, Space); off in demo mode
  // and on a narrow viewport (UDR-0195 D9, the folder-order precedent).
  const reorderable = useEntryVisible('ontology.listReorder') && !props.readOnly && props.catalog.length > 1
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )
  const noSensors = useSensors()
  const ids = props.catalog.map((entry) => entry.id)
  const onDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const from = ids.indexOf(String(active.id))
    const to = ids.indexOf(String(over.id))
    if (from < 0 || to < 0) return
    props.onReorder(arrayMove(ids, from, to))
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b px-2 py-1.5">
        <span className="text-xs font-semibold text-zinc-700">Ontologies</span>
        <div className="flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-600"
            onClick={props.onCreate}
            disabled={props.readOnly}
            aria-label="New ontology"
            title={props.readOnly ? 'Disabled in demo mode' : 'New ontology'}>
            <Plus className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-600"
            onClick={props.onImportClick}
            disabled={props.importing || props.readOnly}
            aria-label="Import RDF file"
            title={
              props.readOnly ? 'Disabled in demo mode' : 'Import (Turtle, TriG, N-Triples, N-Quads, RDF/XML, JSON-LD)'
            }>
            {props.importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-600"
            onClick={props.onOpenTrash}
            aria-label="Deleted ontologies"
            title="Deleted ontologies (restore)">
            <ArchiveRestore className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {props.catalog.length === 0 ? (
          <p className="px-3 py-4 text-xs text-zinc-500">No ontologies yet. Create or import one.</p>
        ) : (
          <DndContext
            sensors={reorderable ? sensors : noSensors}
            collisionDetection={closestCenter}
            onDragEnd={onDragEnd}
            accessibility={{
              screenReaderInstructions: {
                draggable: 'To reorder, press Space, move with the arrow keys, and press Space again to drop.',
              },
            }}>
            <SortableContext items={ids} strategy={verticalListSortingStrategy}>
              {props.catalog.map((entry) => (
                <CatalogRow key={entry.id} entry={entry} reorderable={reorderable} {...props} />
              ))}
            </SortableContext>
          </DndContext>
        )}
      </div>
    </div>
  )
}

/** One catalog row: a sortable item with a drag handle (PRP-0213, UDR-0195 D9). */
function CatalogRow(props: {
  entry: CatalogEntry
  reorderable: boolean
  selectedId: string | null
  onSelect: (id: string) => void
  onExport: (entry: CatalogEntry) => void
  onRename: (entry: CatalogEntry) => void
  onDelete: (entry: CatalogEntry) => void
  readOnly: boolean
}) {
  const { entry } = props
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: entry.id,
    disabled: !props.reorderable,
  })
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      data-ontology-id={entry.id}
      className={cn(
        'group flex w-full items-start gap-1 border-b px-2 py-2 text-left',
        props.selectedId === entry.id ? 'bg-blue-50' : 'hover:bg-zinc-50',
        isDragging && 'relative z-10 opacity-80 shadow',
      )}>
      {props.reorderable && (
        // @dnd-kit drag handle: `attributes` supply role="button" and the roledescription at runtime.
        <button
          type="button"
          className="mt-0.5 shrink-0 cursor-grab text-zinc-300 opacity-0 transition-opacity hover:text-zinc-600 focus:opacity-100 group-hover:opacity-100"
          aria-label={`Reorder ${entry.name}`}
          title="Drag to reorder (or focus and press Space, then the arrow keys)"
          {...attributes}
          {...listeners}>
          <GripVertical className="h-3.5 w-3.5" />
        </button>
      )}
      <button type="button" className="min-w-0 flex-1 text-left" onClick={() => props.onSelect(entry.id)}>
        <div className="truncate text-xs font-medium text-zinc-800">{entry.name}</div>
        {entry.description && <div className="mt-0.5 line-clamp-2 text-[10px] text-zinc-500">{entry.description}</div>}
      </button>
      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
        <Button
          variant="ghost"
          size="icon"
          className="h-5 w-5 text-zinc-500"
          disabled={props.readOnly}
          onClick={() => props.onRename(entry)}
          aria-label={`Rename ${entry.name}`}
          title="Rename">
          <Pencil className="h-3 w-3" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-5 w-5 text-zinc-500"
          onClick={() => props.onExport(entry)}
          aria-label={`Export ${entry.name}`}
          title="Export (choose a format)">
          <Download className="h-3 w-3" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-5 w-5 text-zinc-500 hover:text-red-600"
          disabled={props.readOnly}
          onClick={() => props.onDelete(entry)}
          aria-label={`Delete ${entry.name}`}
          title="Delete">
          <Trash2 className="h-3 w-3" />
        </Button>
      </div>
    </div>
  )
}

// ---- Right pane: tab strip (PRP-0213, UDR-0195 D10 / D11) ---------------------------

type RightTab = 'detail' | 'resources' | 'search' | 'history' | 'checks' | 'explain'

const PANEL_TABS: { id: RightTab; label: string; Icon: typeof Info }[] = [
  { id: 'detail', label: 'Detail', Icon: Info },
  { id: 'resources', label: 'Resources', Icon: Users },
  { id: 'search', label: 'Search', Icon: Search },
  { id: 'history', label: 'History', Icon: History },
  { id: 'checks', label: 'Checks', Icon: ListChecks },
  { id: 'explain', label: 'Explain', Icon: Lightbulb },
]

function TabBadge(props: { count: number }) {
  if (props.count <= 0) return null
  return (
    <span
      className="rounded-full bg-amber-100 px-1 text-[10px] text-amber-800"
      title={`${props.count} warning${props.count === 1 ? '' : 's'} (not ignored)`}>
      {props.count}
    </span>
  )
}

/**
 * The right pane's tabs. A hidden copy measures each tab with and without its label;
 * the strip picks full / compact / icons by its own width (no fixed breakpoints). The
 * name stays as title and aria-label at every level; never first letters.
 */
function PanelTabStrip(props: { active: RightTab; onSelect: (tab: RightTab) => void; checksBadge: number }) {
  const stripRef = useRef<HTMLDivElement>(null)
  const measureRef = useRef<HTMLDivElement>(null)
  const [level, setLevel] = useState<TabLevel>('full')
  const activeIndex = PANEL_TABS.findIndex((tab) => tab.id === props.active)
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-measure when the badge changes the widths
  useEffect(() => {
    const strip = stripRef.current
    const measure = measureRef.current
    if (!strip || !measure) return
    const update = () => {
      const full = [...measure.querySelectorAll<HTMLElement>('[data-measure="full"]')].map((el) => el.offsetWidth)
      const icon = [...measure.querySelectorAll<HTMLElement>('[data-measure="icon"]')].map((el) => el.offsetWidth)
      setLevel(chooseTabLevel(strip.clientWidth, full, icon, activeIndex))
    }
    update()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(update)
    observer.observe(strip)
    return () => observer.disconnect()
  }, [activeIndex, props.checksBadge])
  return (
    <div ref={stripRef} role="tablist" data-tab-level={level} className="relative flex shrink-0 border-b text-xs">
      {/* Hidden measuring copy: each tab with its label and as an icon only. */}
      <div
        ref={measureRef}
        aria-hidden="true"
        className="pointer-events-none invisible absolute left-0 top-0 flex whitespace-nowrap font-medium">
        {PANEL_TABS.map(({ id, label, Icon }) => (
          <span key={id} className="flex">
            <span data-measure="full" className="inline-flex items-center gap-1 px-3 py-2">
              <Icon className="h-3 w-3" /> {label}
              {id === 'checks' && <TabBadge count={props.checksBadge} />}
            </span>
            <span data-measure="icon" className="inline-flex items-center gap-1 px-2 py-2">
              <Icon className="h-3 w-3" />
              {id === 'checks' && <TabBadge count={props.checksBadge} />}
            </span>
          </span>
        ))}
      </div>
      {PANEL_TABS.map(({ id, label, Icon }, index) => {
        const labelled = showsLabel(level, index, activeIndex)
        return (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={props.active === id}
            aria-label={label}
            title={label}
            className={cn(
              'min-w-0 py-2 font-medium',
              labelled ? 'flex-1 px-3' : 'flex-1 px-2',
              props.active === id ? 'border-b-2 border-blue-500 text-blue-600' : 'text-zinc-500 hover:text-zinc-700',
            )}
            onClick={() => props.onSelect(id)}>
            <span className="inline-flex items-center gap-1 whitespace-nowrap">
              <Icon className="h-3 w-3" />
              {labelled && label}
              {id === 'checks' && <TabBadge count={props.checksBadge} />}
            </span>
          </button>
        )
      })}
    </div>
  )
}

// ---- Right pane: Detail --------------------------------------------------------

const ENTITY_FORM_PREDICATES = new Set([RDF_TYPE, RDFS_LABEL, RDFS_COMMENT, CW_EMOJI, CW_COLOR])
const RELATIONSHIP_FORM_PREDICATES = new Set([
  RDF_TYPE,
  RDFS_LABEL,
  RDFS_COMMENT,
  RDFS_DOMAIN,
  RDFS_RANGE,
  CW_CARDINALITY,
])

/** The other values of a predicate (other languages, other datatypes) as read-only chips. */
function OtherValues(props: { model: OntologyModel; value: string; predicate: string; inspector: InspectorContext }) {
  const others = otherLiterals(findResource(props.model, termKey(iriTerm(props.value))), props.predicate)
  if (others.length === 0) return null
  return (
    <div className="mt-1 flex flex-wrap gap-1 text-[10px]">
      {others.map((term) => (
        <span
          key={termKey(term)}
          className="rounded border px-1 py-0.5"
          title="Another value (edit it in All statements)">
          <TermView term={term} ctx={props.inspector} />
        </span>
      ))}
    </div>
  )
}

/** The Detail of a selected "is a" edge: one rdfs:subClassOf statement (UDR-0185 D3). */
function IsaDetail(props: {
  source: string
  target: string
  exists: boolean
  entityLabel: (iri: string) => string
  readOnly: boolean
  onSelectEntity: (iri: string) => void
  onRemove: () => void
}) {
  if (!props.exists) return <p className="p-4 text-xs text-zinc-500">This "is a" link no longer exists.</p>
  const entity = (value: string) => (
    <button
      type="button"
      className="font-medium text-blue-700 hover:underline"
      title={value}
      onClick={() => props.onSelectEntity(value)}>
      {props.entityLabel(value)}
    </button>
  )
  return (
    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
      <div>
        <span className={fieldLabel}>Is a (rdfs:subClassOf)</span>
        <div className="rounded border bg-zinc-50 px-2 py-1.5 text-xs text-zinc-700">
          {entity(props.source)} is a {entity(props.target)}
        </div>
      </div>
      {!props.readOnly && (
        <Button variant="destructive" size="sm" className="h-7 w-full text-xs" onClick={props.onRemove}>
          <Trash2 className="mr-1 h-3 w-3" /> Remove this "is a" link
        </Button>
      )}
    </div>
  )
}

/**
 * The OWL property characteristics of a relationship as toggles (UDR-0186 D3): each
 * chip is one `rdf:type` statement. Contradictions and OWL 2 DL limits are notices,
 * never refusals (D4).
 */
function CharacteristicsBlock(props: {
  resource: Resource
  readOnly: boolean
  graphName: (g: GraphTerm) => string
  showGraphs: boolean
  onToggle: (characteristic: Characteristic, on: boolean) => void
}) {
  const set = propertyCharacteristics(props.resource)
  const notices = characteristicNotices(set)
  return (
    <div>
      <span className={fieldLabel}>Characteristics</span>
      <div className="flex flex-wrap gap-1">
        {CHARACTERISTICS.map((c) => {
          const on = set.has(c.key)
          const graphs = props.showGraphs
            ? characteristicGraphs(props.resource, c.key).map((g) => (g ? props.graphName(g) : 'default graph'))
            : []
          return (
            <button
              key={c.key}
              type="button"
              aria-pressed={on}
              disabled={props.readOnly}
              className={cn(
                'rounded-full border px-2 py-0.5 text-[11px]',
                on ? 'border-blue-500 bg-blue-50 text-blue-700' : 'text-zinc-500 hover:bg-zinc-50',
              )}
              title={`owl:${c.iri.slice(c.iri.indexOf('#') + 1)}${graphs.length > 0 ? ` (in ${graphs.join(', ')})` : ''}`}
              onClick={() => props.onToggle(c.key, !on)}>
              {c.label}
            </button>
          )
        })}
      </div>
      {notices.map((notice) => (
        <p key={notice} className="mt-1 flex items-start gap-1 text-[10px] text-amber-700">
          <TriangleAlert className="mt-px h-3 w-3 shrink-0" />
          {notice}
        </p>
      ))}
    </div>
  )
}

/** Clickable IRIs (superclasses, subclasses, super-properties) for the Detail forms (UDR-0185 D6). */
function HierarchyLinks(props: {
  label: string
  iris: string[]
  entityLabel: (iri: string) => string
  onSelect: (iri: string) => void
}) {
  if (props.iris.length === 0) return null
  return (
    <div>
      <span className={fieldLabel}>{props.label}</span>
      <div className="flex flex-wrap gap-1">
        {props.iris.map((value) => (
          <button
            key={value}
            type="button"
            className="max-w-full truncate rounded border px-1.5 py-0.5 text-[11px] text-zinc-600 hover:bg-zinc-50"
            title={value}
            onClick={() => props.onSelect(value)}>
            {props.entityLabel(value)}
          </button>
        ))}
      </div>
    </div>
  )
}

function DetailPane(props: {
  model: OntologyModel
  entity: EntityView | null
  relationshipIri: string | null
  relationships: RelationshipEdgeView[]
  entityLabel: (iri: string) => string
  inspector: InspectorContext
  statementHandlers: (key: string) => {
    onEdit: (index: number, statement: StatementInput | null) => void
    onAdd: (statement: StatementInput) => void
  }
  onSetDisplayed: (iri: string, predicate: string, text: string) => void
  onSetFirst: (iri: string, predicate: string, term: Term | null) => void
  onToggleKey: (propertyIri: string, on: boolean) => void
  onAddProperty: (entityIri: string) => void
  onRemoveProperty: (propertyIri: string, entityIri: string) => void
  onDeleteEntity: (iri: string) => void
  onDeleteRelationship: (iri: string) => void
  onSelectRelationship: (iri: string) => void
  onSelectResource: (key: string) => void
  onToggleCharacteristic: (propertyIri: string, characteristic: Characteristic, on: boolean) => void
}) {
  const { entity, model, inspector } = props
  const readOnly = inspector.readOnly

  if (entity) {
    const key = termKey(iriTerm(entity.iri))
    const seen = new Set<string>()
    const incident = props.relationships.filter((r) => {
      if (r.source !== entity.iri && r.target !== entity.iri) return false
      if (seen.has(r.id)) return false
      seen.add(r.id)
      return true
    })
    const hierarchy = classHierarchy(model, entity.iri)
    return (
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <div>
          <span className={fieldLabel}>Entity type</span>
          <div className="break-all text-[10px] text-zinc-400">{entity.iri}</div>
        </div>
        <div>
          <label htmlFor="entity-label" className={fieldLabel}>
            Label
          </label>
          <input
            id="entity-label"
            className={fieldInput}
            value={displayLiteral(findResource(model, key), RDFS_LABEL)}
            placeholder={localName(entity.iri)}
            readOnly={readOnly}
            onChange={(e) => props.onSetDisplayed(entity.iri, RDFS_LABEL, e.target.value)}
          />
          <OtherValues model={model} value={entity.iri} predicate={RDFS_LABEL} inspector={inspector} />
        </div>
        <div>
          <label htmlFor="entity-emoji" className={fieldLabel}>
            Emoji
          </label>
          <input
            id="entity-emoji"
            className={fieldInput}
            value={entity.emoji}
            maxLength={8}
            placeholder="e.g. 🏭"
            readOnly={readOnly}
            onChange={(e) => props.onSetDisplayed(entity.iri, CW_EMOJI, e.target.value)}
          />
        </div>
        <div>
          <span className={fieldLabel}>Color</span>
          <div className="flex items-center gap-1.5">
            {COLOR_PRESETS.map((color) => (
              <button
                key={color || 'none'}
                type="button"
                disabled={readOnly}
                className={cn(
                  'h-5 w-5 rounded-full border',
                  entity.color === color && 'ring-2 ring-blue-500 ring-offset-1',
                )}
                style={{ backgroundColor: color || '#ffffff' }}
                title={color || 'Default'}
                aria-label={color || 'Default color'}
                onClick={() => props.onSetDisplayed(entity.iri, CW_COLOR, color)}
              />
            ))}
          </div>
        </div>
        <div>
          <label htmlFor="entity-comment" className={fieldLabel}>
            Description
          </label>
          <textarea
            id="entity-comment"
            className={fieldInput}
            rows={2}
            value={entity.comment}
            readOnly={readOnly}
            onChange={(e) => props.onSetDisplayed(entity.iri, RDFS_COMMENT, e.target.value)}
          />
          <OtherValues model={model} value={entity.iri} predicate={RDFS_COMMENT} inspector={inspector} />
        </div>
        <div>
          <span className={fieldLabel}>Properties</span>
          <div className="space-y-1.5">
            {entity.properties.map((prop) => (
              <div key={prop.iri} className="flex items-center gap-1">
                <input
                  className={cn(fieldInput, 'flex-1')}
                  value={displayLiteral(findResource(model, prop.key), RDFS_LABEL)}
                  placeholder={localName(prop.iri)}
                  aria-label="Property name"
                  readOnly={readOnly}
                  onChange={(e) => props.onSetDisplayed(prop.iri, RDFS_LABEL, e.target.value)}
                />
                {prop.rangeIri?.startsWith(XSD) ? (
                  <select
                    className="rounded-md border bg-transparent px-1 py-1.5 text-xs"
                    value={(prop.rangeIri ?? '').slice(XSD.length)}
                    aria-label="Property type"
                    disabled={readOnly}
                    onChange={(e) => props.onSetFirst(prop.iri, RDFS_RANGE, iriTerm(`${XSD}${e.target.value}`))}>
                    {[...new Set([...XSD_RANGES, (prop.rangeIri ?? '').slice(XSD.length)])].map((range) => (
                      <option key={range} value={range}>
                        {range}
                      </option>
                    ))}
                  </select>
                ) : (
                  <button
                    type="button"
                    className="max-w-[90px] truncate rounded-md border border-dashed px-1 py-1.5 text-[10px] text-zinc-500 hover:bg-zinc-50"
                    title={
                      prop.rangeIsExpression
                        ? 'The range is an expression or has several values: edit it in the statements list'
                        : prop.rangeIri
                          ? prop.rangeIri
                          : 'No range: add one in the statements list'
                    }
                    onClick={() => props.onSelectResource(prop.key)}>
                    {prop.rangeIsExpression ? 'expression' : prop.rangeIri ? localName(prop.rangeIri) : 'no range'}
                  </button>
                )}
                {prop.shared && (
                  <span
                    className="shrink-0 rounded bg-violet-50 px-1 text-[9px] text-violet-700"
                    title="This property has several domains (their intersection in OWL); it is listed under each of them">
                    shared
                  </span>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    'h-6 w-6 shrink-0',
                    prop.isKey ? 'bg-amber-100 text-amber-700 hover:bg-amber-200' : 'text-zinc-400',
                  )}
                  aria-label={`Toggle key attribute for ${prop.label}`}
                  aria-pressed={prop.isKey}
                  disabled={readOnly}
                  title={prop.isKey ? 'Key attribute (click to unset)' : 'Mark as key attribute'}
                  onClick={() => props.onToggleKey(prop.iri, !prop.isKey)}>
                  <KeyRound className="h-3 w-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 shrink-0 text-zinc-500"
                  aria-label={`Statements of ${prop.label}`}
                  title="All statements of this property"
                  onClick={() => props.onSelectResource(prop.key)}>
                  <Braces className="h-3 w-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 shrink-0 text-zinc-500 hover:text-red-600"
                  aria-label={`Remove property ${prop.label}`}
                  disabled={readOnly}
                  title={prop.shared ? 'Remove from this entity only' : 'Remove property'}
                  onClick={() => props.onRemoveProperty(prop.iri, entity.iri)}>
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            ))}
            {!readOnly && (
              <Button
                variant="outline"
                size="sm"
                className="h-6 w-full text-xs"
                onClick={() => props.onAddProperty(entity.iri)}>
                <Plus className="mr-1 h-3 w-3" /> Add property
              </Button>
            )}
          </div>
        </div>
        <HierarchyLinks
          label="Is a (superclasses)"
          iris={hierarchy.supers}
          entityLabel={props.entityLabel}
          onSelect={(value) => props.onSelectResource(termKey(iriTerm(value)))}
        />
        <HierarchyLinks
          label="Subclasses"
          iris={hierarchy.subs}
          entityLabel={props.entityLabel}
          onSelect={(value) => props.onSelectResource(termKey(iriTerm(value)))}
        />
        {incident.length > 0 && (
          <div>
            <span className={fieldLabel}>Relationships</span>
            <div className="space-y-1">
              {incident.map((rel) => (
                <button
                  key={rel.id}
                  type="button"
                  className="block w-full truncate rounded border px-2 py-1 text-left text-[11px] text-zinc-600 hover:bg-zinc-50"
                  onClick={() => props.onSelectRelationship(rel.iri)}>
                  {props.entityLabel(rel.source)} → {props.entityLabel(rel.target)}: {rel.label} [
                  {CARDINALITY_SYMBOL[rel.cardinality] ?? rel.cardinality}]
                </button>
              ))}
            </div>
          </div>
        )}
        <AllStatements
          resourceKey={key}
          ctx={inspector}
          formPredicates={ENTITY_FORM_PREDICATES}
          {...props.statementHandlers(key)}
        />
        {!readOnly && (
          <Button
            variant="destructive"
            size="sm"
            className="h-7 w-full text-xs"
            onClick={() => props.onDeleteEntity(entity.iri)}>
            <Trash2 className="mr-1 h-3 w-3" /> Delete entity
          </Button>
        )}
      </div>
    )
  }

  if (props.relationshipIri) {
    const value = props.relationshipIri
    const key = termKey(iriTerm(value))
    const resource = findResource(model, key)
    if (!resource) return <p className="p-4 text-xs text-zinc-500">This relationship no longer exists.</p>
    const pairs = props.relationships.filter((r) => r.iri === value)
    const cardinality = displayLiteral(resource, CW_CARDINALITY)
    const custom = cardinality !== '' && !(CARDINALITIES as readonly string[]).includes(cardinality)
    const loose = [...iriObjects(resource, RDFS_DOMAIN), ...iriObjects(resource, RDFS_RANGE)].filter(
      (v) => !props.relationships.some((r) => r.source === v || r.target === v),
    )
    return (
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <div>
          <span className={fieldLabel}>Relationship</span>
          <div className="break-all text-[10px] text-zinc-400">{value}</div>
        </div>
        <div className="space-y-1">
          {pairs.map((pair) => (
            <div key={pair.id} className="rounded border bg-zinc-50 px-2 py-1.5 text-xs text-zinc-700">
              {props.entityLabel(pair.source)} → {props.entityLabel(pair.target)}
            </div>
          ))}
          {loose.length > 0 && (
            <p className="text-[10px] text-zinc-500">
              Also links non-entities ({loose.map((v) => localName(v)).join(', ')}); see All statements.
            </p>
          )}
        </div>
        <HierarchyLinks
          label="Super-properties"
          iris={[...new Set(iriObjects(resource, RDFS_SUB_PROPERTY_OF))].sort()}
          entityLabel={(v) => localName(v)}
          onSelect={(v) => props.onSelectResource(termKey(iriTerm(v)))}
        />
        <div>
          <label htmlFor="rel-label" className={fieldLabel}>
            Label
          </label>
          <input
            id="rel-label"
            className={fieldInput}
            value={displayLiteral(resource, RDFS_LABEL)}
            placeholder={localName(value)}
            readOnly={readOnly}
            onChange={(e) => props.onSetDisplayed(value, RDFS_LABEL, e.target.value)}
          />
          <OtherValues model={model} value={value} predicate={RDFS_LABEL} inspector={inspector} />
        </div>
        <div>
          <label htmlFor="rel-cardinality" className={fieldLabel}>
            Cardinality
          </label>
          <select
            id="rel-cardinality"
            className={cn(fieldInput, 'appearance-auto')}
            value={cardinality || 'one-to-many'}
            disabled={readOnly}
            onChange={(e) => props.onSetDisplayed(value, CW_CARDINALITY, e.target.value)}>
            {custom && <option value={cardinality}>{cardinality} (custom)</option>}
            {CARDINALITIES.map((c) => (
              <option key={c} value={c}>
                {c} [{CARDINALITY_SYMBOL[c]}]
              </option>
            ))}
          </select>
        </div>
        <CharacteristicsBlock
          resource={resource}
          readOnly={readOnly}
          graphName={(g) => graphLabel(g, model, model.document.prefixes)}
          showGraphs={(inspector.graphs ?? []).length > 0}
          onToggle={(characteristic, on) => props.onToggleCharacteristic(value, characteristic, on)}
        />
        <div>
          <label htmlFor="rel-comment" className={fieldLabel}>
            Description
          </label>
          <textarea
            id="rel-comment"
            className={fieldInput}
            rows={2}
            value={displayLiteral(resource, RDFS_COMMENT)}
            readOnly={readOnly}
            onChange={(e) => props.onSetDisplayed(value, RDFS_COMMENT, e.target.value)}
          />
          <OtherValues model={model} value={value} predicate={RDFS_COMMENT} inspector={inspector} />
        </div>
        <AllStatements
          resourceKey={key}
          ctx={inspector}
          formPredicates={RELATIONSHIP_FORM_PREDICATES}
          {...props.statementHandlers(key)}
        />
        {!readOnly && (
          <Button
            variant="destructive"
            size="sm"
            className="h-7 w-full text-xs"
            onClick={() => props.onDeleteRelationship(value)}>
            <Trash2 className="mr-1 h-3 w-3" /> Delete relationship
          </Button>
        )}
      </div>
    )
  }

  return (
    <p className="p-4 text-xs text-zinc-500">
      Click an Entity or a Relationship on the canvas to see and edit its detail. Drag from a node&apos;s outer ring to
      another node to create a directional relationship (or, with Link: Is a, an &quot;is a&quot; link from the subclass
      to the superclass). Everything else in the ontology is listed in the Resources tab.
    </p>
  )
}

// ---- Right pane: Search ---------------------------------------------------------

function SearchPane(props: {
  disabled: boolean
  sparql: string
  onSparqlChange: (value: string) => void
  onRunSparql: () => void
  queryRunning: boolean
  nlQuestion: string
  onNlQuestionChange: (value: string) => void
  onRunNl: () => void
  nlRunning: boolean
  result: QueryResult | null
  /** What the search covers, from the canvas graph selector; null for a single-graph ontology. */
  scopeLabel: string | null
  inspector: InspectorContext
  highlightActive: boolean
  onClearHighlight: () => void
  /** The imported ontologies the search can include (UDR-0189 D5); null = none. */
  imports: { include: boolean; count: number } | null
  onIncludeImportsChange: (include: boolean) => void
  /** The entailment mode of the next search (UDR-0191 D10). */
  entailment: Entailment
  onEntailmentChange: (mode: Entailment) => void
  /** The stored result's state for the chosen mode (PRP-0211, UDR-0193 D12); null for None. */
  entailmentState: string | null
  /** Stop the running search (UDR-0193 D5). */
  onStop: () => void
  /** Why? on an inferred triples-table row (UDR-0193 D15). */
  onExplain: (row: TripleRow, mode: 'rdfs' | 'owl-rl') => void
}) {
  const current = ENTAILMENT_OPTIONS.find((option) => option.value === props.entailment)
  const running = props.queryRunning || props.nlRunning
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 space-y-2 border-b p-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {props.imports && (
            <label
              className="flex items-center gap-1 text-[11px] text-zinc-600"
              title="Also answer from the ontologies this one imports (read-only). The starting value is the App Settings default.">
              <input
                type="checkbox"
                checked={props.imports.include}
                disabled={props.disabled}
                onChange={(e) => props.onIncludeImportsChange(e.target.checked)}
              />
              Include imports ({props.imports.count} ontolog{props.imports.count === 1 ? 'y' : 'ies'})
            </label>
          )}
          <label className="flex items-center gap-1 text-[11px] text-zinc-600" title={current?.title}>
            Entailment
            <select
              aria-label="Entailment"
              className="h-6 rounded border bg-white px-1 text-[11px]"
              value={props.entailment}
              disabled={props.disabled}
              onChange={(e) => props.onEntailmentChange(e.target.value as Entailment)}>
              {ENTAILMENT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value} title={option.title}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          {props.entailmentState && (
            <span className="text-[10px] text-zinc-500" data-entailment-state>
              {props.entailmentState}
            </span>
          )}
        </div>
        {props.scopeLabel && (
          <p className="text-[10px] text-teal-700" data-search-scope>
            Searching {props.scopeLabel} (change it with the graph selector above the canvas). GRAPH patterns can still
            name any graph.
          </p>
        )}
        <div>
          <span className={fieldLabel}>Natural language</span>
          <div className="flex items-start gap-1">
            <textarea
              className={cn(fieldInput, 'flex-1')}
              rows={2}
              placeholder="e.g. Which entities are related to Person?"
              value={props.nlQuestion}
              onChange={(e) => props.onNlQuestionChange(e.target.value)}
              disabled={props.disabled}
            />
            <Button
              size="icon"
              className="h-8 w-8 shrink-0"
              onClick={props.onRunNl}
              disabled={props.disabled || props.nlRunning || !props.nlQuestion.trim()}
              aria-label="Convert to SPARQL and run"
              title="Convert to SPARQL and run">
              {props.nlRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <SendHorizontal className="h-4 w-4" />}
            </Button>
          </div>
        </div>
        <div>
          <span className={fieldLabel}>SPARQL</span>
          <div className="h-[160px] overflow-hidden rounded-md border">
            <Editor
              language="sparql"
              value={props.sparql}
              theme="vs"
              onChange={(value) => props.onSparqlChange(value ?? '')}
              options={{
                minimap: { enabled: false },
                fontSize: 12,
                automaticLayout: true,
                scrollBeyondLastLine: false,
                lineNumbers: 'off',
                tabSize: 2,
                wordWrap: 'on',
              }}
            />
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            <Button
              size="sm"
              className="h-6 text-xs"
              onClick={props.onRunSparql}
              disabled={props.disabled || props.queryRunning}>
              {props.queryRunning ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
              Run
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-6 text-xs"
              onClick={() => props.onSparqlChange(DEFAULT_SPARQL)}
              disabled={props.disabled || props.sparql === DEFAULT_SPARQL}
              title="Restore the default SPARQL query">
              <RotateCcw className="mr-1 h-3 w-3" /> Reset
            </Button>
            {running && (
              <Button
                variant="outline"
                size="sm"
                className="h-6 text-xs"
                onClick={props.onStop}
                title="Stop the running search (the server stops it too)">
                <Square className="mr-1 h-3 w-3" /> Stop
              </Button>
            )}
            {props.highlightActive && (
              <Button variant="outline" size="sm" className="h-6 text-xs" onClick={props.onClearHighlight}>
                Clear highlight
              </Button>
            )}
          </div>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-2">
        <QueryResultView result={props.result} inspector={props.inspector} onExplain={props.onExplain} />
      </div>
    </div>
  )
}

/** Content-derived row keys (duplicate rows get a stable occurrence suffix). */
function keyRows(rows: string[][]): { key: string; row: string[]; index: number }[] {
  const seen = new Map<string, number>()
  return rows.map((row, index) => {
    const base = row.join('')
    const occurrence = seen.get(base) ?? 0
    seen.set(base, occurrence + 1)
    return { key: occurrence === 0 ? base : `${base}#${occurrence}`, row, index }
  })
}

/**
 * A CONSTRUCT / DESCRIBE answer: the triples first, the prefix lines folded below them
 * (eight `@prefix` lines at the top looked like the query echoed back).
 */
function TurtleAnswer({ text, className, empty }: { text: string; className: string; empty: string }) {
  const lines = text.split('\n')
  const isPrefix = (line: string) => /^\s*(@prefix|@base|PREFIX|BASE)\b/i.test(line)
  const prefixes = lines.filter(isPrefix)
  const body = lines
    .filter((line) => !isPrefix(line))
    .join('\n')
    .trim()
  return (
    <div className={cn('rounded', className)}>
      <pre className="overflow-x-auto p-2 text-[11px] leading-relaxed" data-answer-body>
        {body || empty}
      </pre>
      {prefixes.length > 0 && (
        <details className="px-2 pb-1 text-[10px] text-zinc-500">
          <summary className="cursor-pointer">Prefixes ({prefixes.length})</summary>
          <pre className="overflow-x-auto pt-1 text-[10px] leading-relaxed">{prefixes.join('\n')}</pre>
        </details>
      )}
    </div>
  )
}

/** The mark of an answer that exists only through inference (UDR-0191 D6 / D10). */
function InferredBadge() {
  return (
    <span
      className="ml-1 rounded bg-violet-100 px-1 text-[10px] font-medium text-violet-700"
      title="Inferred by entailment; not written in any ontology file">
      Inferred
    </span>
  )
}

/**
 * One muted line per notice: what the query engine could not give back as written. An
 * OWL 2 RL inconsistency is a warning box listing its findings (UDR-0192 D5 / D10).
 */
function QueryNotices({ notices }: { notices?: QueryNotice[] }) {
  if (!notices?.length) return null
  const inconsistent = notices.find((notice) => notice.code === 'entailment_inconsistent')
  const lines = notices.filter((notice) => notice !== inconsistent)
  return (
    <div className="mb-1 space-y-0.5">
      {inconsistent && <InconsistencyBox notice={inconsistent} />}
      {lines.map((notice) => (
        <p key={notice.code} className="flex items-start gap-1 text-[10px] text-amber-700" data-notice={notice.code}>
          <Info className="mt-px h-3 w-3 shrink-0" />
          {notice.message}
        </p>
      ))}
    </div>
  )
}

/** The inconsistencies of an OWL 2 RL answer: the answer is still shown below (UDR-0192 D5). */
function InconsistencyBox({ notice }: { notice: QueryNotice }) {
  const items = notice.items ?? []
  const count = notice.count ?? items.length
  const more = count - items.length
  return (
    <section
      aria-label="Inconsistencies"
      data-notice={notice.code}
      className="mb-1 rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] text-red-800">
      <p className="flex items-start gap-1 font-medium">
        <TriangleAlert className="mt-px h-3 w-3 shrink-0" />
        Inconsistent ontology (OWL 2 RL) -- {count} finding{count === 1 ? '' : 's'}
      </p>
      <ul className="mt-0.5 list-disc space-y-0.5 pl-5">
        {items.map((item) => (
          <li key={`${item.kind}:${item.message}`} data-kind={item.kind}>
            {item.message}
            {item.graph && <span className="ml-1 rounded bg-red-100 px-1 text-[10px]">{item.graph}</span>}
          </li>
        ))}
        {more > 0 && <li>and {more} more</li>}
      </ul>
      <p className="mt-0.5 text-[10px] text-red-700">{notice.message}</p>
    </section>
  )
}

/** A typed SELECT cell (CTR-0171 v4); falls back to the plain string from an older server. */
function QueryCellView(props: { cell: QueryCell | undefined; text: string; inspector: InspectorContext }) {
  if (props.cell === undefined) return <>{props.text}</>
  if (props.cell === null) return null
  const { lexical_forms: forms, ...term } = props.cell
  return (
    <span>
      <TermView term={term as Term} ctx={props.inspector} />
      {forms && (
        <span
          className="ml-1 rounded bg-amber-50 px-1 text-[10px] text-amber-700"
          title={`Written in the file as: ${forms.join(', ')}`}>
          ambiguous: {forms.join(' / ')}
        </span>
      )}
    </span>
  )
}

type TripleRow = { s: Term; p: Term; o: Term; graph: Term | null; inferred: boolean }

function QueryResultView({
  result,
  inspector,
  onExplain,
}: {
  result: QueryResult | null
  inspector: InspectorContext
  onExplain?: (row: TripleRow, mode: 'rdfs' | 'owl-rl') => void
}) {
  if (!result) return <p className="text-xs text-zinc-400">Run a query to see results here.</p>
  if (result.kind === 'error') return <p className="whitespace-pre-wrap text-xs text-red-600">{result.error}</p>
  if (result.kind === 'ask') {
    return (
      <div>
        <QueryNotices notices={result.notices} />
        <p className="text-sm font-medium">
          {result.value ? 'Yes' : 'No'}
          {result.inferred && <InferredBadge />}
        </p>
      </div>
    )
  }
  if (result.kind === 'construct') {
    const inferredCount = result.inferred_count ?? 0
    const entailed = result.inferred_turtle !== undefined
    const dataset = result.format === 'trig'
    const triples = result.triples
    return (
      <div>
        <QueryNotices notices={result.notices} />
        <p className="mb-1 text-[10px] text-zinc-500">
          {result.triple_count} triple{result.triple_count === 1 ? '' : 's'}
          {entailed ? `, ${inferredCount} inferred` : ''}
          {result.truncated || result.triples_truncated ? ' (truncated)' : ''}
        </p>
        {triples ? (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[11px]" aria-label="Triples">
              <thead>
                <tr>
                  {['subject', 'predicate', 'object', ...(dataset ? ['graph'] : [])].map((column) => (
                    <th key={column} className="border bg-zinc-50 px-2 py-1 text-left font-medium">
                      {column}
                    </th>
                  ))}
                  {entailed && <th className="border bg-zinc-50 px-2 py-1" aria-label="Inferred" />}
                </tr>
              </thead>
              <tbody>
                {triples.map((row, index) => (
                  <tr
                    // Rows have no natural key: a triple may repeat once per graph.
                    // biome-ignore lint/suspicious/noArrayIndexKey: the answer is replaced as a whole
                    key={index}
                    data-inferred={row.inferred ? 'true' : undefined}>
                    {[row.s, row.p, row.o, ...(dataset ? [row.graph] : [])].map((term, column) => (
                      // biome-ignore lint/suspicious/noArrayIndexKey: fixed column order
                      <td key={column} className="break-all border px-2 py-1 align-top">
                        {term ? (
                          <TermView term={term} ctx={inspector} />
                        ) : (
                          <span className="text-zinc-400">default</span>
                        )}
                      </td>
                    ))}
                    {entailed && (
                      <td className="border px-1 py-1 align-top">
                        {row.inferred && <InferredBadge />}
                        {row.inferred && onExplain && result.entailment && result.entailment !== 'simple' && (
                          <button
                            type="button"
                            className="ml-1 text-[10px] text-blue-700 hover:underline"
                            title="Why is this inferred?"
                            onClick={() => onExplain(row, result.entailment as 'rdfs' | 'owl-rl')}>
                            Why?
                          </button>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        <details className="mt-2 text-[10px] text-zinc-500" open={!triples}>
          <summary className="cursor-pointer">{dataset ? 'TriG' : 'Turtle'}</summary>
          <div className="mt-1 space-y-2">
            <TurtleAnswer text={result.turtle} className="bg-zinc-50" empty="(no stated triples)" />
            {entailed && (
              <section aria-label="Inferred triples">
                <p className="mb-1 flex items-center">
                  <InferredBadge />
                  <span className="ml-1">
                    {ENTAILMENT_LABELS[result.entailment ?? 'rdfs']}, not written in any ontology file
                  </span>
                </p>
                <TurtleAnswer
                  text={result.inferred_turtle ?? ''}
                  className="bg-violet-50"
                  empty="(no further triples)"
                />
              </section>
            )}
          </div>
        </details>
      </div>
    )
  }
  const cells = result.cells
  const inferredRows = result.row_inferred
  return (
    <div>
      <QueryNotices notices={result.notices} />
      <p className="mb-1 text-[10px] text-zinc-500">
        {result.row_count} row{result.row_count === 1 ? '' : 's'}
        {inferredRows ? `, ${result.inferred_count ?? 0} inferred` : ''}
        {result.truncated ? ' (truncated)' : ''} — matched entities are highlighted on the canvas
      </p>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[11px]">
          <thead>
            <tr>
              {result.columns.map((column) => (
                <th key={column} className="border bg-zinc-50 px-2 py-1 text-left font-medium">
                  {column}
                </th>
              ))}
              {inferredRows && <th className="border bg-zinc-50 px-2 py-1" aria-label="Inferred" />}
            </tr>
          </thead>
          <tbody>
            {keyRows(result.rows).map(({ key, row, index }) => (
              <tr key={key} data-inferred={inferredRows?.[index] ? 'true' : undefined}>
                {result.columns.map((column, columnIndex) => (
                  <td key={column} className="break-all border px-2 py-1 align-top">
                    <QueryCellView
                      cell={cells ? cells[index]?.[columnIndex] : undefined}
                      text={row[columnIndex]}
                      inspector={inspector}
                    />
                  </td>
                ))}
                {inferredRows && (
                  <td className="border px-1 py-1 align-top">{inferredRows[index] && <InferredBadge />}</td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
