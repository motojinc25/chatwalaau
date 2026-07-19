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
 * The frontend never parses RDF (UDR-0084 D6): it edits the CTR-0169 JSON
 * graph projection served by CTR-0171 and lets the backend own the Turtle
 * codec. Saving is backup-then-atomic server-side; closing (or switching
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
  Download,
  ImageDown,
  KeyRound,
  LayoutGrid,
  Loader2,
  Maximize,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  SendHorizontal,
  Trash2,
  Upload,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
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
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import '@/lib/monaco-setup'
import '@xyflow/react/dist/style.css'

// ---- Projection types (the CTR-0169 JSON graph projection) -----------------

interface OntologyProperty {
  iri: string
  label: string
  range: string
  comment?: string
  /** Key attribute of its entity (persisted as a cw:isKey annotation). */
  is_key?: boolean
}

interface OntologyEntity {
  iri: string
  label: string
  comment: string
  emoji: string
  color: string
  x: number
  y: number
  properties: OntologyProperty[]
}

interface OntologyRelationship {
  iri: string
  label: string
  comment: string
  source: string
  target: string
  cardinality: string
}

interface CatalogEntry {
  id: string
  name: string
  description: string
  updated_at?: string
}

type QueryResult =
  | {
      kind: 'select'
      columns: string[]
      rows: string[][]
      row_count: number
      truncated: boolean
      entity_iris: string[]
    }
  | { kind: 'construct'; turtle: string; triple_count: number; truncated: boolean }
  | { kind: 'ask'; value: boolean }
  | { kind: 'error'; error: string }

interface Selection {
  kind: 'entity' | 'relationship'
  iri: string
}

const CARDINALITIES = ['one-to-one', 'one-to-many', 'many-to-one', 'many-to-many'] as const

const CARDINALITY_SYMBOL: Record<string, string> = {
  'one-to-one': '1:1',
  'one-to-many': '1:N',
  'many-to-one': 'N:1',
  'many-to-many': 'N:M',
}

const COLOR_PRESETS = ['', '#3b82f6', '#22c55e', '#eab308', '#f97316', '#ef4444', '#a855f7', '#14b8a6']

const XSD = 'http://www.w3.org/2001/XMLSchema#'
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

function localName(iri: string): string {
  const hash = iri.split('#')
  const tail = hash[hash.length - 1].split('/')
  return tail[tail.length - 1] || iri
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
  const baseStyle = { stroke: d.stroke, strokeWidth: d.strokeWidth, opacity: d.opacity }

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

function CanvasToolbar(props: {
  onAddEntity: () => void
  onResetLayout: () => void
  onDownload: () => void
  layouting: boolean
  disabled: boolean
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

  // The loaded projection (the editing SSOT of this modal)
  const [baseIri, setBaseIri] = useState('')
  const [entities, setEntities] = useState<OntologyEntity[]>([])
  const [relationships, setRelationships] = useState<OntologyRelationship[]>([])
  const [extraTurtle, setExtraTurtle] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  // Canvas interaction
  const [selection, setSelection] = useState<Selection | null>(null)
  const [highlight, setHighlight] = useState<Set<string> | null>(null)
  const [layouting, setLayouting] = useState(false)
  const canvasRef = useRef<HTMLDivElement | null>(null)

  // Right pane
  const [rightTab, setRightTab] = useState<'detail' | 'search'>('detail')
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

  const fetchCatalog = useCallback(async () => {
    setError(null)
    try {
      const res = await fetch('/api/ontology/catalog')
      if (!res.ok) throw new Error('Failed to load the ontology catalog')
      const data = await res.json()
      setCatalog((data.ontologies ?? []) as CatalogEntry[])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load the ontology catalog')
    }
  }, [])

  const loadOntology = useCallback(async (id: string) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${id}`)
      if (!res.ok) throw new Error('Failed to load the ontology')
      const data = await res.json()
      setSelectedId(id)
      setBaseIri((data.base_iri as string) ?? '')
      setEntities((data.entities ?? []) as OntologyEntity[])
      setRelationships((data.relationships ?? []) as OntologyRelationship[])
      setExtraTurtle((data.extra_turtle as string) ?? '')
      setDirty(false)
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

  const save = useCallback(async () => {
    if (!selectedId) return
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${selectedId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entities, relationships, extra_turtle: extraTurtle }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(typeof detail?.detail === 'string' ? detail.detail : 'Failed to save the ontology')
      }
      setDirty(false)
      await fetchCatalog()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save the ontology')
    } finally {
      setSaving(false)
    }
  }, [selectedId, entities, relationships, extraTurtle, fetchCatalog])

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
      if (!res.ok) throw new Error('Failed to create the ontology')
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
      if (!res.ok) throw new Error('Failed to rename the ontology')
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
      if (!res.ok) throw new Error('Failed to delete the ontology')
      if (selectedId === deleteTarget.id) {
        setSelectedId(null)
        setEntities([])
        setRelationships([])
        setExtraTurtle('')
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
          const detail = await res.json().catch(() => null)
          throw new Error(typeof detail?.detail === 'string' ? detail.detail : 'Failed to import the file')
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

  const exportOntology = useCallback(async (entry: CatalogEntry) => {
    try {
      const res = await fetch(`/api/ontology/${entry.id}/export`)
      if (!res.ok) return
      const blob = await res.blob()
      const objectUrl = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = objectUrl
      a.download = `${entry.name || entry.id}.ttl`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(objectUrl)
    } catch {
      // silent: download is best-effort
    }
  }, [])

  // ---- Editing the projection ----

  const markDirty = useCallback(() => setDirty(true), [])

  const updateEntity = useCallback(
    (iri: string, patch: Partial<OntologyEntity>) => {
      setEntities((prev) => prev.map((e) => (e.iri === iri ? { ...e, ...patch } : e)))
      markDirty()
    },
    [markDirty],
  )

  const updateRelationship = useCallback(
    (iri: string, patch: Partial<OntologyRelationship>) => {
      setRelationships((prev) => prev.map((r) => (r.iri === iri ? { ...r, ...patch } : r)))
      markDirty()
    },
    [markDirty],
  )

  const addEntity = useCallback(() => {
    const taken = new Set(entities.map((e) => e.iri))
    const iri = mintIri(baseIri || 'https://chatwalaau.com/ontology/local#', 'Entity', taken)
    const entity: OntologyEntity = {
      iri,
      label: 'New Entity',
      comment: '',
      emoji: '',
      color: '',
      x: 40 + (entities.length % 5) * 60,
      y: 40 + (entities.length % 7) * 40,
      properties: [],
    }
    setEntities((prev) => [...prev, entity])
    setSelection({ kind: 'entity', iri })
    setRightTab('detail')
    markDirty()
  }, [entities, baseIri, markDirty])

  const deleteEntity = useCallback(
    (iri: string) => {
      setEntities((prev) => prev.filter((e) => e.iri !== iri))
      setRelationships((prev) => prev.filter((r) => r.source !== iri && r.target !== iri))
      setSelection(null)
      markDirty()
    },
    [markDirty],
  )

  const deleteRelationship = useCallback(
    (iri: string) => {
      setRelationships((prev) => prev.filter((r) => r.iri !== iri))
      setSelection(null)
      markDirty()
    },
    [markDirty],
  )

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return
      const taken = new Set(relationships.map((r) => r.iri))
      const iri = mintIri(baseIri || 'https://chatwalaau.com/ontology/local#', 'relatesTo', taken)
      const relationship: OntologyRelationship = {
        iri,
        label: 'relates to',
        comment: '',
        source: connection.source,
        target: connection.target,
        cardinality: 'one-to-many',
      }
      setRelationships((prev) => [...prev, relationship])
      setSelection({ kind: 'relationship', iri })
      setRightTab('detail')
      markDirty()
    },
    [relationships, baseIri, markDirty],
  )

  // ---- React Flow derivation ----

  // Neighborhood of the current selection: clicking an ENTITY activates its
  // In/Out edges plus the connected nodes; clicking a RELATIONSHIP activates its
  // endpoint nodes plus the edges flowing in/out of those endpoints. Cleared by
  // clicking another area (the pane).
  const related = useMemo(() => {
    const nodes = new Set<string>()
    const edges = new Set<string>()
    if (!selection) return { nodes, edges }
    if (selection.kind === 'entity') {
      for (const rel of relationships) {
        if (rel.source === selection.iri || rel.target === selection.iri) {
          edges.add(rel.iri)
          nodes.add(rel.source)
          nodes.add(rel.target)
        }
      }
      nodes.delete(selection.iri) // the primary node has its own stronger style
    } else {
      const rel = relationships.find((r) => r.iri === selection.iri)
      if (rel) {
        nodes.add(rel.source)
        nodes.add(rel.target)
        for (const other of relationships) {
          if (other.iri === rel.iri) continue
          const endpoints = [other.source, other.target]
          if (endpoints.includes(rel.source) || endpoints.includes(rel.target)) edges.add(other.iri)
        }
      }
    }
    return { nodes, edges }
  }, [selection, relationships])

  const buildNodes = useCallback(
    (): EntityFlowNode[] =>
      entities.map((entity) => ({
        id: entity.iri,
        type: 'entity' as const,
        position: { x: entity.x, y: entity.y },
        // Drag only from the inner disc; the outer ring is the connection zone (UDR-0098 D1).
        dragHandle: `.${ENTITY_DRAG_CLASS}`,
        data: {
          label: entity.label,
          emoji: entity.emoji,
          color: entity.color,
          propertyCount: entity.properties.length,
          keyCount: entity.properties.filter((p) => p.is_key).length,
          isSelected: selection?.kind === 'entity' && selection.iri === entity.iri,
          related: related.nodes.has(entity.iri),
          dimmed: highlight !== null && !highlight.has(entity.iri),
          matched: Boolean(highlight?.has(entity.iri)),
        },
      })),
    [entities, selection, related, highlight],
  )

  // FLICKER FIX: node positions live in local React Flow state during a drag
  // (applyNodeChanges clones only the dragged node, so the memoized siblings do
  // not re-render per pointer move) and are written back into the entities on
  // drag stop. The list is rebuilt synchronously (derived-state-during-render)
  // whenever the underlying data / selection / highlight actually changes, so
  // an ontology switch never shows a stale frame.
  const [nodes, setNodes] = useState<EntityFlowNode[]>([])
  const nodesDepsRef = useRef<readonly unknown[] | null>(null)
  const nodesDeps = [entities, selection, related, highlight] as const
  if (nodesDepsRef.current === null || nodesDeps.some((dep, index) => dep !== nodesDepsRef.current?.[index])) {
    nodesDepsRef.current = nodesDeps
    setNodes(buildNodes())
  }

  const onNodesChange = useCallback((changes: NodeChange<EntityFlowNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current))
  }, [])

  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, _node: EntityFlowNode, draggedNodes: EntityFlowNode[]) => {
      const moved = new Map(draggedNodes.map((n) => [n.id, n.position]))
      if (moved.size === 0) return
      setEntities((prev) =>
        prev.map((entity) => {
          const position = moved.get(entity.iri)
          return position ? { ...entity, x: position.x, y: position.y } : entity
        }),
      )
      markDirty()
    },
    [markDirty],
  )

  const edges = useMemo<Edge[]>(() => {
    // Fan-out bookkeeping: how many relationships share each unordered node pair,
    // and this edge's index within that group (UDR-0098 D2).
    const pairKey = (s: string, t: string) => (s < t ? `${s}|${t}` : `${t}|${s}`)
    const pairTotal = new Map<string, number>()
    for (const rel of relationships) {
      const key = pairKey(rel.source, rel.target)
      pairTotal.set(key, (pairTotal.get(key) ?? 0) + 1)
    }
    const pairSeen = new Map<string, number>()
    return relationships.map((rel) => {
      const key = pairKey(rel.source, rel.target)
      const parallelIndex = pairSeen.get(key) ?? 0
      pairSeen.set(key, parallelIndex + 1)
      const parallelCount = pairTotal.get(key) ?? 1
      const isSelected = selection?.kind === 'relationship' && selection.iri === rel.iri
      const isRelated = !isSelected && related.edges.has(rel.iri)
      const matched = highlight !== null && (highlight.has(rel.source) || highlight.has(rel.target))
      const dimmed = highlight !== null && !matched
      const stroke = isSelected ? '#2563eb' : isRelated ? '#93c5fd' : matched ? '#f59e0b' : '#94a3b8'
      return {
        id: rel.iri,
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
        } satisfies RelEdgeData,
      }
    })
  }, [relationships, selection, related, highlight])

  const resetLayout = useCallback(async () => {
    if (entities.length === 0) return
    setLayouting(true)
    try {
      const { default: ELK } = await import('elkjs/lib/elk.bundled.js')
      const elk = new ELK()
      const result = await elk.layout({
        id: 'root',
        layoutOptions: {
          'elk.algorithm': 'layered',
          'elk.direction': 'RIGHT',
          'elk.spacing.nodeNode': '60',
          'elk.layered.spacing.nodeNodeBetweenLayers': '140',
        },
        children: entities.map((e) => ({ id: e.iri, width: 110, height: 105 })),
        edges: relationships.map((r) => ({ id: r.iri, sources: [r.source], targets: [r.target] })),
      })
      setEntities((prev) =>
        prev.map((entity) => {
          const child = result.children?.find((c) => c.id === entity.iri)
          return child ? { ...entity, x: child.x ?? entity.x, y: child.y ?? entity.y } : entity
        }),
      )
      markDirty()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Auto-layout failed')
    } finally {
      setLayouting(false)
    }
  }, [entities, relationships, markDirty])

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
        const iris = new Set(result.entity_iris.filter((iri) => entities.some((e) => e.iri === iri)))
        setHighlight(iris.size > 0 ? iris : null)
      } else {
        setHighlight(null)
      }
    },
    [entities],
  )

  const runSparql = useCallback(async () => {
    if (!selectedId || !sparql.trim()) return
    setQueryRunning(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${selectedId}/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sparql }),
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
  }, [selectedId, sparql, applyResult])

  const runNlQuery = useCallback(async () => {
    if (!selectedId || !nlQuestion.trim()) return
    setNlRunning(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${selectedId}/nl-query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: nlQuestion }),
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
  }, [selectedId, nlQuestion, applyResult])

  // ---- Derived detail-pane data ----

  const selectedEntity = useMemo(
    () => (selection?.kind === 'entity' ? (entities.find((e) => e.iri === selection.iri) ?? null) : null),
    [selection, entities],
  )
  const selectedRelationship = useMemo(
    () => (selection?.kind === 'relationship' ? (relationships.find((r) => r.iri === selection.iri) ?? null) : null),
    [selection, relationships],
  )
  const entityLabel = useCallback(
    (iri: string) => entities.find((e) => e.iri === iri)?.label ?? localName(iri),
    [entities],
  )

  const busy = saving || importing || creating

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-screen w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-white p-0 text-zinc-900 sm:rounded-none">
          <DialogHeader className="flex shrink-0 flex-row items-center justify-between border-b px-3 py-2 text-left">
            <DialogTitle className="text-sm font-semibold text-zinc-900">Ontology</DialogTitle>
            <div className="mr-8 flex items-center gap-2">
              {error && <span className="max-w-[480px] truncate text-xs text-red-600">{error}</span>}
              {dirty && <span className="text-xs text-amber-600">Unsaved changes</span>}
              <Button size="sm" className="h-7" onClick={() => void save()} disabled={!selectedId || !dirty || busy}>
                {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                Save
              </Button>
            </div>
          </DialogHeader>

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
                    onExport={(entry) => void exportOntology(entry)}
                    onRename={(entry) => {
                      setRenameName(entry.name)
                      setRenameDescription(entry.description ?? '')
                      setRenameTarget(entry)
                    }}
                    onDelete={(entry) => setDeleteTarget(entry)}
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
                              setSelection({ kind: 'relationship', iri: edge.id })
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
                      {(['detail', 'search'] as const).map((tab) => (
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
                          ) : (
                            'Detail'
                          )}
                        </button>
                      ))}
                    </div>
                    {rightTab === 'detail' ? (
                      <DetailPane
                        entity={selectedEntity}
                        relationship={selectedRelationship}
                        relationships={relationships}
                        entityLabel={entityLabel}
                        onUpdateEntity={updateEntity}
                        onUpdateRelationship={updateRelationship}
                        onDeleteEntity={deleteEntity}
                        onDeleteRelationship={deleteRelationship}
                        onSelectRelationship={(iri) => setSelection({ kind: 'relationship', iri })}
                        baseIri={baseIri}
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
            accept=".ttl,.turtle,.rdf,.owl,.xml"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              e.target.value = ''
              if (file) guardDirty(() => void importOntology(file))
            }}
          />
        </DialogContent>
      </Dialog>

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={(o) => !creating && setCreateOpen(o)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New ontology</DialogTitle>
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

// ---- Left pane: the catalog ---------------------------------------------------

function CatalogPane(props: {
  catalog: CatalogEntry[]
  selectedId: string | null
  importing: boolean
  onSelect: (id: string) => void
  onCreate: () => void
  onImportClick: () => void
  onExport: (entry: CatalogEntry) => void
  onRename: (entry: CatalogEntry) => void
  onDelete: (entry: CatalogEntry) => void
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
            aria-label="New ontology"
            title="New ontology">
            <Plus className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-600"
            onClick={props.onImportClick}
            disabled={props.importing}
            aria-label="Import RDF file"
            title="Import (.ttl / .rdf / .owl)">
            {props.importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
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
                  title="Export (Turtle)">
                  <Download className="h-3 w-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-5 w-5 text-zinc-500 hover:text-red-600"
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

const fieldLabel = 'mb-1 block text-[10px] font-medium uppercase tracking-wide text-zinc-500'
const fieldInput = 'w-full rounded-md border bg-transparent px-2 py-1.5 text-xs'

function DetailPane(props: {
  entity: OntologyEntity | null
  relationship: OntologyRelationship | null
  relationships: OntologyRelationship[]
  entityLabel: (iri: string) => string
  onUpdateEntity: (iri: string, patch: Partial<OntologyEntity>) => void
  onUpdateRelationship: (iri: string, patch: Partial<OntologyRelationship>) => void
  onDeleteEntity: (iri: string) => void
  onDeleteRelationship: (iri: string) => void
  onSelectRelationship: (iri: string) => void
  baseIri: string
}) {
  const { entity, relationship } = props

  if (entity) {
    const incident = props.relationships.filter((r) => r.source === entity.iri || r.target === entity.iri)
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
            value={entity.label}
            onChange={(e) => props.onUpdateEntity(entity.iri, { label: e.target.value })}
          />
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
            onChange={(e) => props.onUpdateEntity(entity.iri, { emoji: e.target.value })}
          />
        </div>
        <div>
          <span className={fieldLabel}>Color</span>
          <div className="flex items-center gap-1.5">
            {COLOR_PRESETS.map((color) => (
              <button
                key={color || 'none'}
                type="button"
                className={cn(
                  'h-5 w-5 rounded-full border',
                  entity.color === color && 'ring-2 ring-blue-500 ring-offset-1',
                )}
                style={{ backgroundColor: color || '#ffffff' }}
                title={color || 'Default'}
                aria-label={color || 'Default color'}
                onClick={() => props.onUpdateEntity(entity.iri, { color })}
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
            onChange={(e) => props.onUpdateEntity(entity.iri, { comment: e.target.value })}
          />
        </div>
        <div>
          <span className={fieldLabel}>Properties</span>
          <div className="space-y-1.5">
            {entity.properties.map((prop) => (
              <div key={prop.iri} className="flex items-center gap-1">
                <input
                  className={cn(fieldInput, 'flex-1')}
                  value={prop.label}
                  aria-label="Property name"
                  onChange={(e) =>
                    props.onUpdateEntity(entity.iri, {
                      properties: entity.properties.map((p) =>
                        p.iri === prop.iri ? { ...p, label: e.target.value } : p,
                      ),
                    })
                  }
                />
                <select
                  className="rounded-md border bg-transparent px-1 py-1.5 text-xs"
                  value={prop.range.startsWith(XSD) ? prop.range.slice(XSD.length) : 'string'}
                  aria-label="Property type"
                  onChange={(e) =>
                    props.onUpdateEntity(entity.iri, {
                      properties: entity.properties.map((p) =>
                        p.iri === prop.iri ? { ...p, range: `${XSD}${e.target.value}` } : p,
                      ),
                    })
                  }>
                  {XSD_RANGES.map((range) => (
                    <option key={range} value={range}>
                      {range}
                    </option>
                  ))}
                </select>
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    'h-6 w-6 shrink-0',
                    prop.is_key ? 'bg-amber-100 text-amber-700 hover:bg-amber-200' : 'text-zinc-400',
                  )}
                  aria-label={`Toggle key attribute for ${prop.label}`}
                  aria-pressed={Boolean(prop.is_key)}
                  title={prop.is_key ? 'Key attribute (click to unset)' : 'Mark as key attribute'}
                  onClick={() =>
                    props.onUpdateEntity(entity.iri, {
                      properties: entity.properties.map((p) => (p.iri === prop.iri ? { ...p, is_key: !p.is_key } : p)),
                    })
                  }>
                  <KeyRound className="h-3 w-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 shrink-0 text-zinc-500 hover:text-red-600"
                  aria-label={`Remove property ${prop.label}`}
                  onClick={() =>
                    props.onUpdateEntity(entity.iri, {
                      properties: entity.properties.filter((p) => p.iri !== prop.iri),
                    })
                  }>
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              className="h-6 w-full text-xs"
              onClick={() => {
                const taken = new Set(entity.properties.map((p) => p.iri))
                const iri = mintIri(props.baseIri || `${entity.iri}_`, 'property', taken)
                props.onUpdateEntity(entity.iri, {
                  properties: [...entity.properties, { iri, label: 'property', range: `${XSD}string` }],
                })
              }}>
              <Plus className="mr-1 h-3 w-3" /> Add property
            </Button>
          </div>
        </div>
        {incident.length > 0 && (
          <div>
            <span className={fieldLabel}>Relationships</span>
            <div className="space-y-1">
              {incident.map((rel) => (
                <button
                  key={rel.iri}
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
        <Button
          variant="destructive"
          size="sm"
          className="h-7 w-full text-xs"
          onClick={() => props.onDeleteEntity(entity.iri)}>
          <Trash2 className="mr-1 h-3 w-3" /> Delete entity
        </Button>
      </div>
    )
  }

  if (relationship) {
    return (
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <div>
          <span className={fieldLabel}>Relationship</span>
          <div className="break-all text-[10px] text-zinc-400">{relationship.iri}</div>
        </div>
        <div className="rounded border bg-zinc-50 px-2 py-1.5 text-xs text-zinc-700">
          {props.entityLabel(relationship.source)} → {props.entityLabel(relationship.target)}
        </div>
        <div>
          <label htmlFor="rel-label" className={fieldLabel}>
            Label
          </label>
          <input
            id="rel-label"
            className={fieldInput}
            value={relationship.label}
            onChange={(e) => props.onUpdateRelationship(relationship.iri, { label: e.target.value })}
          />
        </div>
        <div>
          <label htmlFor="rel-cardinality" className={fieldLabel}>
            Cardinality
          </label>
          <select
            id="rel-cardinality"
            className={cn(fieldInput, 'appearance-auto')}
            value={relationship.cardinality}
            onChange={(e) => props.onUpdateRelationship(relationship.iri, { cardinality: e.target.value })}>
            {CARDINALITIES.map((cardinality) => (
              <option key={cardinality} value={cardinality}>
                {cardinality} [{CARDINALITY_SYMBOL[cardinality]}]
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="rel-comment" className={fieldLabel}>
            Description
          </label>
          <textarea
            id="rel-comment"
            className={fieldInput}
            rows={2}
            value={relationship.comment}
            onChange={(e) => props.onUpdateRelationship(relationship.iri, { comment: e.target.value })}
          />
        </div>
        <Button
          variant="destructive"
          size="sm"
          className="h-7 w-full text-xs"
          onClick={() => props.onDeleteRelationship(relationship.iri)}>
          <Trash2 className="mr-1 h-3 w-3" /> Delete relationship
        </Button>
      </div>
    )
  }

  return (
    <p className="p-4 text-xs text-zinc-500">
      Click an Entity or a Relationship on the canvas to see and edit its detail. Drag from a node&apos;s right handle
      to another node to create a directional relationship.
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
  highlightActive: boolean
  onClearHighlight: () => void
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 space-y-2 border-b p-2">
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
        <QueryResultView result={props.result} />
      </div>
    </div>
  )
}

/** Content-derived row keys (duplicate rows get a stable occurrence suffix). */
function keyRows(rows: string[][]): { key: string; row: string[] }[] {
  const seen = new Map<string, number>()
  return rows.map((row) => {
    const base = row.join('')
    const occurrence = seen.get(base) ?? 0
    seen.set(base, occurrence + 1)
    return { key: occurrence === 0 ? base : `${base}#${occurrence}`, row }
  })
}

function QueryResultView({ result }: { result: QueryResult | null }) {
  if (!result) return <p className="text-xs text-zinc-400">Run a query to see results here.</p>
  if (result.kind === 'error') return <p className="whitespace-pre-wrap text-xs text-red-600">{result.error}</p>
  if (result.kind === 'ask') return <p className="text-sm font-medium">{result.value ? 'Yes' : 'No'}</p>
  if (result.kind === 'construct') {
    return (
      <div>
        <p className="mb-1 text-[10px] text-zinc-500">
          {result.triple_count} triple{result.triple_count === 1 ? '' : 's'}
          {result.truncated ? ' (truncated)' : ''}
        </p>
        <pre className="overflow-x-auto rounded bg-zinc-50 p-2 text-[11px] leading-relaxed">{result.turtle}</pre>
      </div>
    )
  }
  return (
    <div>
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
            {keyRows(result.rows).map(({ key, row }) => (
              <tr key={key}>
                {result.columns.map((column, columnIndex) => (
                  <td key={column} className="break-all border px-2 py-1 align-top">
                    {row[columnIndex]}
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
