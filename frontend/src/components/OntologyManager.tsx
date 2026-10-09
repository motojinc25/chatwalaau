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
  useReactFlow,
} from '@xyflow/react'
import {
  ArchiveRestore,
  Braces,
  Download,
  History,
  ImageDown,
  Info,
  KeyRound,
  Layers,
  LayoutGrid,
  Loader2,
  Maximize,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  SendHorizontal,
  Trash2,
  TriangleAlert,
  Upload,
  Users,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { DeletedOntologiesDialog, HistoryPanel } from '@/components/OntologyHistory'
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
import {
  addSubclassOf,
  annotateStatement,
  applyStatementEdit,
  assertedElsewhere,
  CARDINALITIES,
  CHARACTERISTICS,
  type Characteristic,
  CW_CARDINALITY,
  CW_COLOR,
  CW_EMOJI,
  characteristicGraphs,
  characteristicNotices,
  classHierarchy,
  compactIri,
  createDatatypeProperty,
  createEntity,
  createRelationship,
  type Diagnostic,
  deleteEntity as deleteEntityFromModel,
  diffStatements,
  displayedLiteralEdit,
  displayLiteral,
  documentKey,
  EMPTY_MODEL,
  type EntityView,
  entityViews,
  externalReferenceCount,
  findResource,
  firstObjectEdit,
  fromProjection,
  type GraphScope,
  type GraphTerm,
  graphsOf,
  iriObjects,
  iri as iriTerm,
  localName,
  mintReifierIri,
  type OntologyDocument,
  type OntologyModel,
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
  reifiersOf,
  relationshipEdges,
  removePropertyFromEntity,
  removeResource,
  removeStatement,
  removeSubclassOf,
  renameBlankNodes,
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
  XSD,
} from '@/lib/ontologyModel'
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
}

/** A query notice: what the query engine could not give back as written (UDR-0181 D3). */
interface QueryNotice {
  code: string
  message: string
}

/** A typed SELECT cell; `lexical_forms` lists the file's spellings when it is ambiguous. */
type QueryCell = (Term & { lexical_forms?: string[] }) | null

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
    }
  | {
      kind: 'construct'
      /** CTR-0171 v5: "trig" for an ontology with named graphs (each triple under its graphs). */
      format?: 'turtle' | 'trig'
      turtle: string
      triple_count: number
      truncated: boolean
      notices?: QueryNotice[]
    }
  | { kind: 'ask'; value: boolean; notices?: QueryNotice[] }
  | { kind: 'error'; error: string }

/**
 * What the Detail pane shows: an entity or a relationship drawn on the canvas
 * (by IRI), or any resource by its term key (inspector, UDR-0180 D9).
 */
type Selection =
  | { kind: 'entity' | 'relationship'; iri: string }
  | { kind: 'resource'; key: string }
  | { kind: 'isa'; source: string; target: string }

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
        style={{ borderColor: data.color || '#d4d4d8', opacity: data.dimmed ? 0.25 : 1 }}
      />
      {/* Inner disc = the move/drag surface (dragHandle '.entity-drag'), on top. */}
      <div
        className={cn(
          ENTITY_DRAG_CLASS,
          'absolute inset-[7px] flex cursor-move items-center justify-center rounded-full border border-zinc-200 bg-white text-zinc-900',
        )}
        style={{ opacity: data.dimmed ? 0.25 : 1 }}
        title="Drag to move; drag from the outer ring to connect">
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
      {/* Label below the circle (kept outside the shape per the design spec). */}
      <span className="pointer-events-none absolute left-1/2 top-full mt-1.5 max-w-[130px] -translate-x-1/2 truncate text-center text-xs font-medium text-zinc-700">
        {data.label}
      </span>
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
    strokeDasharray: d.isa ? '6 4' : undefined,
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
  /** The detected vocabulary style for new terms and why (UDR-0185 D4). */
  style: VocabularyStyle
  styleReason: string
}) {
  const { zoomIn, zoomOut, fitView } = useReactFlow()
  const iconButton = 'h-7 w-7 text-zinc-600'
  return (
    <div className="ontology-toolbar flex shrink-0 items-center gap-1 border-b bg-zinc-50 px-2 py-1">
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
      <span
        className="rounded bg-zinc-200 px-1.5 py-0.5 text-[10px] font-semibold text-zinc-600"
        title={props.styleReason}>
        <span className="sr-only">Vocabulary style: </span>
        {props.style.toUpperCase()}
      </span>
      <div className="mx-1 h-4 w-px bg-zinc-200" />
      {props.graphs.length > 0 ? (
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
      )}
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

  // Right pane
  const [rightTab, setRightTab] = useState<'detail' | 'resources' | 'search' | 'history'>('detail')
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
  const [creating, setCreating] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<CatalogEntry | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [renameTarget, setRenameTarget] = useState<CatalogEntry | null>(null)
  const [renameName, setRenameName] = useState('')
  const [renameDescription, setRenameDescription] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [importing, setImporting] = useState(false)
  const importInputRef = useRef<HTMLInputElement | null>(null)

  // Unsaved-changes confirmation: the action to run once the user discards.
  const [pendingDiscard, setPendingDiscard] = useState<(() => void) | null>(null)
  // Advertised by GET /api/ontology/catalog (PRP-0139 / UDR-0122 D4). Derived from
  // the server payload, never inferred client-side, so the notice cannot claim a
  // restriction the backend is not enforcing.
  const [demoBlocked, setDemoBlocked] = useState(false)

  const fetchCatalog = useCallback(async () => {
    setError(null)
    try {
      const res = await fetch('/api/ontology/catalog')
      if (!res.ok) throw new Error('Failed to load the ontology catalog')
      const data = await res.json()
      setCatalog((data.ontologies ?? []) as CatalogEntry[])
      setDemoBlocked(data.demo_mode === true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load the ontology catalog')
    }
  }, [])

  /**
   * Load an ontology. `keepView` (after a rebased save, UDR-0183 D8) keeps the graph
   * selector, the selection, the search result and the transient layout, so the
   * canvas only gains what another tab saved.
   */
  const loadOntology = useCallback(async (id: string, options: { keepView?: boolean } = {}) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${id}`)
      if (!res.ok) throw new Error('Failed to load the ontology')
      const data = await res.json()
      const loaded = fromProjection(data)
      setSelectedId(id)
      setBaseIri((data.base_iri as string) ?? '')
      setModel(loaded)
      saveBaseRef.current = saveBase(loaded)
      setDiagnostics((data.diagnostics ?? []) as Diagnostic[])
      setRevision((data.revision as string) ?? null)
      setStaleRevision(false)
      setSaveConflicts(null)
      setDirty(false)
      if (options.keepView) return
      setAutoPositions(new Map())
      layoutCommittedRef.current = false
      reifierChoiceRef.current = new Map()
      setGraphScope('all')
      setSelection(null)
      setHighlight(null)
      setQueryResult(null)
      setRightTab('detail')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load the ontology')
    } finally {
      setLoading(false)
    }
  }, [])

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
  const saveStatements = useCallback(async (): Promise<boolean> => {
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
      body: JSON.stringify({ revision, operations, fresh_blank_nodes: freshBlankNodes, on_stale: 'rebase' }),
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
      if (detail?.pointer && typeof detail?.message === 'string') {
        throw new Error(`Cannot save: ${detail.pointer} -- ${detail.message}`)
      }
      if (typeof detail?.message === 'string') throw new Error(detail.message)
      throw new Error(typeof detail === 'string' ? detail : 'Failed to save the ontology')
    }
    const body = await res.json().catch(() => null)
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
  }, [selectedId, model, revision, loadOntology])

  const save = useCallback(async () => {
    if (!selectedId) return
    setSaving(true)
    setError(null)
    setSaveConflicts(null)
    try {
      if (await saveStatements()) {
        await fetchCatalog()
        return
      }
      const res = await fetch(`/api/ontology/${selectedId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...toPayload(model), revision }),
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
        if (detail?.error === 'invalid_projection') {
          throw new Error(`Cannot save: ${detail.pointer} -- ${detail.message}`)
        }
        if (typeof detail?.message === 'string') throw new Error(detail.message)
        throw new Error(typeof detail === 'string' ? detail : 'Failed to save the ontology')
      }
      const body = await res.json().catch(() => null)
      setRevision((body?.revision as string) ?? null)
      saveBaseRef.current = saveBase(model)
      setDirty(false)
      await fetchCatalog()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save the ontology')
    } finally {
      setSaving(false)
    }
  }, [selectedId, model, revision, fetchCatalog, saveStatements])

  // ---- Catalog actions ----

  const createOntology = useCallback(async () => {
    setCreating(true)
    setError(null)
    try {
      const res = await fetch('/api/ontology/catalog', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: createName, description: createDescription }),
      })
      if (!res.ok) {
        const refused = await demoRefusal(res)
        if (refused) setDemoBlocked(true)
        throw new Error(refused ?? 'Failed to create the ontology')
      }
      const entry = (await res.json()) as CatalogEntry
      setCreateOpen(false)
      setCreateName('')
      setCreateDescription('')
      await fetchCatalog()
      await loadOntology(entry.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create the ontology')
    } finally {
      setCreating(false)
    }
  }, [createName, createDescription, fetchCatalog, loadOntology])

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
      setError(null)
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
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to import the file')
      } finally {
        setImporting(false)
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

  // The canvas draws the selected graph(s) only; layout stays in the default graph
  // and is always shown (UDR-0182 D3 / D4). Edits still go to the full model.
  const graphs = useMemo(() => graphsOf(model), [model])
  const viewModel = useMemo(() => scopeModel(model, graphScope), [model, graphScope])
  const views = useMemo(() => entityViews(viewModel), [viewModel])
  const relEdges = useMemo(() => relationshipEdges(viewModel), [viewModel])
  const isaEdges = useMemo(() => subclassEdges(viewModel), [viewModel])
  const vocabStyle = useMemo(() => vocabularyStyle(model), [model])
  const undeclared = useMemo(() => undeclaredClasses(model), [model])
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
      if (value && resource?.role === 'entity') setSelection({ kind: 'entity', iri: value })
      else if (value && relEdges.some((e) => e.iri === value)) setSelection({ kind: 'relationship', iri: value })
      else setSelection({ kind: 'resource', key })
      setRightTab('detail')
    },
    [model, relEdges],
  )

  // ---- React Flow derivation ----

  // Neighborhood of the current selection: clicking an ENTITY activates its
  // In/Out edges plus the connected nodes; clicking a RELATIONSHIP activates its
  // endpoint nodes plus the edges flowing in/out of those endpoints. Cleared by
  // clicking another area (the pane).
  const related = useMemo(() => {
    const nodes = new Set<string>()
    const edges = new Set<string>()
    if (!selection || selection.kind === 'resource') return { nodes, edges }
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
      views.map((entity, index) => ({
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
        },
      })),
    [views, positionOf, selection, related, highlight],
  )

  // FLICKER FIX: node positions live in local React Flow state during a drag
  // (applyNodeChanges clones only the dragged node, so the memoized siblings do
  // not re-render per pointer move) and are written back into the model on
  // drag stop. The list is rebuilt synchronously (derived-state-during-render)
  // whenever the underlying data / selection / highlight actually changes, so
  // an ontology switch never shows a stale frame.
  const [nodes, setNodes] = useState<EntityFlowNode[]>([])
  const nodesDepsRef = useRef<readonly unknown[] | null>(null)
  const nodesDeps = [views, autoPositions, selection, related, highlight] as const
  if (nodesDepsRef.current === null || nodesDeps.some((dep, index) => dep !== nodesDepsRef.current?.[index])) {
    nodesDepsRef.current = nodesDeps
    setNodes(buildNodes())
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
      const moved = new Map(draggedNodes.map((n) => [n.id, n.position]))
      if (moved.size === 0) return
      // The FIRST layout action of the session saves the picture the user sees:
      // every entity's current position, not only the dragged one.
      const positions = layoutCommittedRef.current
        ? moved
        : new Map(nodes.map((n) => [n.id, moved.get(n.id) ?? n.position]))
      commitPositions(positions)
    },
    [nodes, commitPositions],
  )

  const edges = useMemo<Edge[]>(() => {
    // Fan-out bookkeeping: how many relationships share each unordered node pair,
    // and this edge's index within that group (UDR-0098 D2).
    const pairKey = (s: string, t: string) => (s < t ? `${s}|${t}` : `${t}|${s}`)
    const shownIsa = showIsa ? isaEdges : []
    const pairTotal = new Map<string, number>()
    for (const rel of [...relEdges, ...shownIsa]) {
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
    return [...relationships, ...isa]
  }, [relEdges, isaEdges, showIsa, selection, related, highlight])

  /** elkjs layered layout of the given entities (all when `only` is undefined). */
  const computeLayout = useCallback(
    async (only?: Set<string>) => {
      const { default: ELK } = await import('elkjs/lib/elk.bundled.js')
      const elk = new ELK()
      const ids = views.map((v) => v.iri).filter((value) => !only || only.has(value))
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
    [views, relEdges, isaEdges],
  )

  // Entities without a stored position get a TRANSIENT layout when an ontology
  // opens (they used to pile up at 0,0). Nothing is written until the user acts.
  const unpositioned = useMemo(() => views.filter((v) => v.x === null || v.y === null).map((v) => v.iri), [views])
  const unpositionedKey = unpositioned.join('\n')
  useEffect(() => {
    if (!unpositionedKey) return
    const missing = new Set(unpositionedKey.split('\n'))
    if ([...missing].every((value) => autoPositions.has(value))) return
    let cancelled = false
    const positionedCount = views.length - missing.size
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
      })
    return () => {
      cancelled = true
    }
  }, [unpositionedKey, views, autoPositions, computeLayout])

  const resetLayout = useCallback(async () => {
    if (views.length === 0) return
    setLayouting(true)
    try {
      commitPositions(await computeLayout())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Auto-layout failed')
    } finally {
      setLayouting(false)
    }
  }, [views.length, computeLayout, commitPositions])

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
        const iris = new Set(result.entity_iris.filter((value) => views.some((e) => e.iri === value)))
        setHighlight(iris.size > 0 ? iris : null)
      } else {
        setHighlight(null)
      }
    },
    [views],
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
        body: JSON.stringify({ sparql, scope: graphScope }),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) {
        applyResult({ kind: 'error', error: typeof body?.detail === 'string' ? body.detail : 'Query failed' })
        return
      }
      applyResult(body as QueryResult)
    } catch (err) {
      applyResult({ kind: 'error', error: err instanceof Error ? err.message : 'Query failed' })
    } finally {
      setQueryRunning(false)
    }
  }, [selectedId, sparql, graphScope, applyResult])

  const runNlQuery = useCallback(async () => {
    if (!selectedId || !nlQuestion.trim()) return
    setNlRunning(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${selectedId}/nl-query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: nlQuestion, scope: graphScope }),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) {
        applyResult({
          kind: 'error',
          error: typeof body?.detail === 'string' ? body.detail : 'Natural-language search failed',
        })
        return
      }
      // The generated SPARQL lands in the editor for refinement (IMPL-6).
      if (typeof body?.sparql === 'string') setSparql(body.sparql)
      applyResult(body as QueryResult)
    } catch (err) {
      applyResult({ kind: 'error', error: err instanceof Error ? err.message : 'Natural-language search failed' })
    } finally {
      setNlRunning(false)
    }
  }, [selectedId, nlQuestion, graphScope, applyResult])

  // ---- Derived detail-pane data ----

  const selectedEntity = useMemo(
    () => (selection?.kind === 'entity' ? (views.find((e) => e.iri === selection.iri) ?? null) : null),
    [selection, views],
  )
  const selectedRelationshipIri = selection?.kind === 'relationship' ? selection.iri : null
  const entityLabel = useCallback(
    (value: string) => views.find((e) => e.iri === value)?.label ?? localName(value),
    [views],
  )

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
    }),
    [model, diagnostics, demoBlocked, selectResource, graphs, requestAnnotate, undeclared],
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
              {dirty && <span className="text-xs text-amber-600">Unsaved changes</span>}
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
            <div className="relative flex min-h-0 flex-1">
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
                    readOnly={demoBlocked}
                  />
                }
                center={
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
                      style={vocabStyle}
                      styleReason={vocabularyStyleReason(vocabStyle, model)}
                    />
                    <div className="min-h-0 flex-1">
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
                              setSelection({ kind: 'entity', iri: node.id })
                              setRightTab('detail')
                            }}
                            onEdgeClick={(_, edge) => {
                              const data = edge.data as RelEdgeData | undefined
                              if (data?.isa) setSelection({ kind: 'isa', source: edge.source, target: edge.target })
                              else setSelection({ kind: 'relationship', iri: data?.relationshipIri ?? edge.id })
                              setRightTab('detail')
                            }}
                            onPaneClick={() => setSelection(null)}
                            fitView
                            minZoom={0.1}
                            proOptions={{ hideAttribution: false }}>
                            <Background gap={16} />
                          </ReactFlow>
                        )
                      ) : (
                        <div className="flex h-full items-center justify-center px-6 text-center text-sm text-zinc-500">
                          Select an ontology on the left, or create / import one to start designing.
                        </div>
                      )}
                    </div>
                  </div>
                }
                right={
                  <div className="flex min-h-0 flex-1 flex-col">
                    <div className="flex shrink-0 border-b text-xs">
                      {(['detail', 'resources', 'search', 'history'] as const).map((tab) => (
                        <button
                          key={tab}
                          type="button"
                          className={cn(
                            'flex-1 px-3 py-2 font-medium capitalize',
                            rightTab === tab
                              ? 'border-b-2 border-blue-500 text-blue-600'
                              : 'text-zinc-500 hover:text-zinc-700',
                          )}
                          onClick={() => setRightTab(tab)}>
                          {tab === 'search' ? (
                            <span className="inline-flex items-center gap-1">
                              <Search className="h-3 w-3" /> Search
                            </span>
                          ) : tab === 'resources' ? (
                            <span className="inline-flex items-center gap-1">
                              <Users className="h-3 w-3" /> Resources
                            </span>
                          ) : tab === 'history' ? (
                            <span className="inline-flex items-center gap-1">
                              <History className="h-3 w-3" /> History
                            </span>
                          ) : (
                            'Detail'
                          )}
                        </button>
                      ))}
                    </div>
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
                      />
                    )}
                  </div>
                }
              />

              {/* Blocking indicator while a mutation is in flight (PRP-0090 precedent). */}
              {(saving || importing) && (
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/70">
                  <div className="flex items-center gap-2 text-sm text-zinc-700">
                    <Loader2 className="h-5 w-5 animate-spin" />
                    {saving ? 'Saving...' : 'Importing...'}
                  </div>
                </div>
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
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={creating}>
              Cancel
            </Button>
            <Button onClick={() => void createOntology()} disabled={creating || !createName.trim()}>
              {creating ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
      <AlertDialog open={deleteTarget !== null} onOpenChange={(o) => !o && !deleting && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete ontology?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{deleteTarget?.name}&quot; will be removed from the catalog (a backup of its file is kept on disk).
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
  readOnly: boolean
}) {
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
          props.catalog.map((entry) => (
            <div
              key={entry.id}
              className={cn(
                'group flex w-full items-start gap-1 border-b px-2 py-2 text-left',
                props.selectedId === entry.id ? 'bg-blue-50' : 'hover:bg-zinc-50',
              )}>
              <button type="button" className="min-w-0 flex-1 text-left" onClick={() => props.onSelect(entry.id)}>
                <div className="truncate text-xs font-medium text-zinc-800">{entry.name}</div>
                {entry.description && (
                  <div className="mt-0.5 line-clamp-2 text-[10px] text-zinc-500">{entry.description}</div>
                )}
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
          ))
        )}
      </div>
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
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 space-y-2 border-b p-2">
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
            {props.highlightActive && (
              <Button variant="outline" size="sm" className="h-6 text-xs" onClick={props.onClearHighlight}>
                Clear highlight
              </Button>
            )}
          </div>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-2">
        <QueryResultView result={props.result} inspector={props.inspector} />
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

/** One muted line per notice: what the query engine could not give back as written. */
function QueryNotices({ notices }: { notices?: QueryNotice[] }) {
  if (!notices?.length) return null
  return (
    <div className="mb-1 space-y-0.5">
      {notices.map((notice) => (
        <p key={notice.code} className="flex items-start gap-1 text-[10px] text-amber-700" data-notice={notice.code}>
          <Info className="mt-px h-3 w-3 shrink-0" />
          {notice.message}
        </p>
      ))}
    </div>
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

function QueryResultView({ result, inspector }: { result: QueryResult | null; inspector: InspectorContext }) {
  if (!result) return <p className="text-xs text-zinc-400">Run a query to see results here.</p>
  if (result.kind === 'error') return <p className="whitespace-pre-wrap text-xs text-red-600">{result.error}</p>
  if (result.kind === 'ask') {
    return (
      <div>
        <QueryNotices notices={result.notices} />
        <p className="text-sm font-medium">{result.value ? 'Yes' : 'No'}</p>
      </div>
    )
  }
  if (result.kind === 'construct') {
    return (
      <div>
        <QueryNotices notices={result.notices} />
        <p className="mb-1 text-[10px] text-zinc-500">
          {result.triple_count} triple{result.triple_count === 1 ? '' : 's'}
          {result.truncated ? ' (truncated)' : ''}
          {result.format === 'trig' ? ' -- TriG: each triple is shown under the graphs that hold it' : ''}
        </p>
        <pre className="overflow-x-auto rounded bg-zinc-50 p-2 text-[11px] leading-relaxed">{result.turtle}</pre>
      </div>
    )
  }
  const cells = result.cells
  return (
    <div>
      <QueryNotices notices={result.notices} />
      <p className="mb-1 text-[10px] text-zinc-500">
        {result.row_count} row{result.row_count === 1 ? '' : 's'}
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
            </tr>
          </thead>
          <tbody>
            {keyRows(result.rows).map(({ key, row, index }) => (
              <tr key={key}>
                {result.columns.map((column, columnIndex) => (
                  <td key={column} className="break-all border px-2 py-1 align-top">
                    <QueryCellView
                      cell={cells ? cells[index]?.[columnIndex] : undefined}
                      text={row[columnIndex]}
                      inspector={inspector}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
