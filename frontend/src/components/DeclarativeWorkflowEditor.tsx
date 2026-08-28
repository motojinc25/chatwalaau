import Editor from '@monaco-editor/react'
import {
  applyNodeChanges,
  Background,
  BaseEdge,
  EdgeLabelRenderer,
  type EdgeProps,
  getSmoothStepPath,
  Handle,
  type Node,
  type NodeChange,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { closestCenter, DndContext, type DragEndEvent, PointerSensor, useSensor, useSensors } from '@dnd-kit/core'
import { arrayMove, SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  ArrowDown,
  ArrowUp,
  Braces,
  CornerDownRight,
  Eraser,
  GitBranch,
  Globe,
  GripVertical,
  Inbox,
  ListTree,
  Loader2,
  MessageCircleQuestion,
  MessageSquare,
  MessagesSquare,
  Play,
  Plug,
  Plus,
  Repeat,
  RotateCcw,
  SkipForward,
  Split,
  Square,
  StepForward,
  StopCircle,
  Table,
  TriangleAlert,
  Type,
  Variable,
  Workflow as WorkflowIcon,
  Wrench,
  X,
} from 'lucide-react'
import {
  type ComponentType,
  createContext,
  memo,
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import '@/lib/monaco-setup'
import {
  useWorkflowAuthoring,
  type WorkflowAction,
  type WorkflowDocument,
  type WorkflowValidationResult,
} from '@/hooks/useWorkflowAuthoring'
import { cn } from '@/lib/utils'
import { actionLanes, actionSummary, buildEdges, buildWorkflowGraph, NODE_W, str } from '@/lib/workflowGraph'

/**
 * Declarative Workflow Editor (CTR-0184 v2, FEAT-0062, PRP-0121, UDR-0101 D9).
 *
 * A full-screen React-Flow-PRIMARY DAG editor over the FULL Microsoft Agent Framework
 * declarative action surface (23 kinds). CENTER: a React Flow canvas rendering the
 * workflow's actions, with If / Foreach / ConditionGroup drawn as nested CONTAINER
 * nodes (React Flow parentId + child lanes), laid out hierarchically by elkjs. LEFT:
 * workflow-level fields, an optional inputs/outputs editor, and a RECURSIVE action
 * tree where control-flow branches (then/else, loop body, condition lanes) are edited
 * inline. RIGHT: a monaco pane showing the backend-canonical YAML (live preview) with a
 * raw-edit escape hatch. Validation + serialization are the backend's (UDR-0101 D9):
 * the editor sends a structured `document` (or raw `yaml`) to
 * /api/workflows/authoring/validate and renders the returned canonical YAML + warnings
 * (a warning blocks activation). There is NO client-side validation.
 */

const CONTROL =
  'w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-ring'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create a new workflow; otherwise the id of the workflow being edited. */
  editId: string | null
  onSaved: (id?: string) => void
}

// ---- action palette (23 MAF kinds, grouped by category) --------------------
interface KindMeta {
  kind: string
  label: string
  icon: ComponentType<{ className?: string }>
}

const ACTION_CATEGORIES: Array<{ category: string; kinds: KindMeta[] }> = [
  {
    category: 'Variable',
    kinds: [
      { kind: 'SetVariable', label: 'Set variable', icon: Variable },
      { kind: 'SetMultipleVariables', label: 'Set multiple variables', icon: Braces },
      { kind: 'SetTextVariable', label: 'Set text variable', icon: Type },
      { kind: 'ResetVariable', label: 'Reset variable', icon: RotateCcw },
      { kind: 'ClearAllVariables', label: 'Clear all variables', icon: Eraser },
      { kind: 'ParseValue', label: 'Parse value', icon: Braces },
      { kind: 'EditTableV2', label: 'Edit table', icon: Table },
    ],
  },
  {
    category: 'Control flow',
    kinds: [
      { kind: 'If', label: 'Condition (If)', icon: GitBranch },
      { kind: 'ConditionGroup', label: 'Condition group', icon: Split },
      { kind: 'Foreach', label: 'Foreach', icon: Repeat },
      { kind: 'BreakLoop', label: 'Break loop', icon: SkipForward },
      { kind: 'ContinueLoop', label: 'Continue loop', icon: StepForward },
      { kind: 'GotoAction', label: 'Goto action', icon: CornerDownRight },
    ],
  },
  {
    category: 'Output',
    kinds: [{ kind: 'SendActivity', label: 'Send message', icon: MessageSquare }],
  },
  {
    category: 'Agent',
    kinds: [{ kind: 'InvokeAzureAgent', label: 'Invoke agent', icon: Play }],
  },
  {
    category: 'Tool',
    kinds: [
      { kind: 'InvokeFunctionTool', label: 'Invoke function tool', icon: Wrench },
      { kind: 'InvokeMcpTool', label: 'Invoke MCP tool', icon: Plug },
    ],
  },
  {
    category: 'HTTP',
    kinds: [{ kind: 'HttpRequestAction', label: 'HTTP request', icon: Globe }],
  },
  {
    category: 'Human-in-the-loop',
    kinds: [
      { kind: 'Question', label: 'Question', icon: MessageCircleQuestion },
      { kind: 'RequestExternalInput', label: 'Request external input', icon: Inbox },
    ],
  },
  {
    category: 'Workflow control',
    kinds: [
      { kind: 'EndWorkflow', label: 'End workflow', icon: Square },
      { kind: 'EndConversation', label: 'End conversation', icon: StopCircle },
      { kind: 'CreateConversation', label: 'Create conversation', icon: MessagesSquare },
    ],
  },
]

/** Kinds only offered inside a loop body. */
const LOOP_ONLY_KINDS = new Set(['BreakLoop', 'ContinueLoop'])
/** Jailed kinds requiring an opt-in backend flag. */
const JAILED_NOTE: Record<string, string> = {
  InvokeFunctionTool: 'Requires WORKFLOW_FUNCTION_ACTIONS_ENABLED in the backend .env (off by default).',
  InvokeMcpTool: 'Requires WORKFLOW_MCP_ACTIONS_ENABLED in the backend .env (off by default).',
  HttpRequestAction: 'Requires WORKFLOW_HTTP_ACTIONS_ENABLED in the backend .env (off by default).',
}

const ICON_BY_KIND: Record<string, ComponentType<{ className?: string }>> = Object.fromEntries(
  ACTION_CATEGORIES.flatMap((c) => c.kinds.map((k) => [k.kind, k.icon])),
)

function emptyDocument(): WorkflowDocument {
  return { name: '', displayName: '', description: '', maxTurns: null, actions: [] }
}

/** A short (4-char base36) id so a new action gets a stable, unique-ish id instead of a
 * positional suffix (v0.115.2). Editable in the form; the backend rejects duplicates. */
function shortId(): string {
  return Math.random().toString(36).slice(2, 6).padEnd(4, '0')
}

// ---- variable namespace assistance (CTR-0184 v3, UDR-0105 D3/D4) -----------
// The four namespaces the authoring surface presents. A CUSTOM namespace still works
// at runtime (MAF creates one for any unknown prefix) but is never SUGGESTED, and a
// bare name is normalized to `Local.` by the backend on save / compile.
// ParseValue target types the executor understands (_executors_basic.py:469-).
const PARSE_VALUE_TYPES = [
  'string',
  'number',
  'integer',
  'float',
  'boolean',
  'bool',
  'object',
  'record',
  'array',
  'table',
  'list',
] as const

// EditTableV2 operations the executor implements (_executors_basic.py:379-).
const EDIT_TABLE_OPERATIONS = ['add', 'addOrUpdate', 'update', 'remove', 'clear'] as const

// v0.122.0 (operator feedback on UDR-0111 D7): the candidate list holds REAL variables
// only -- never a bare namespace stub like `Local.`. A stub is not a value: picking one
// leaves the field holding an incomplete path that the backend rejects, and it sat at the
// top of every list ahead of the names the author actually wanted. The namespaces are
// taught by the placeholders (`Local.name`) and by the docs, not by fake candidates.
interface VariableCandidates {
  /** Known names valid as a WRITE destination (no read-only Workflow.Inputs). */
  write: string[]
  /** Known names valid to READ in a value / condition / source. */
  read: string[]
}

const VariableCandidatesContext = createContext<VariableCandidates>({ write: [], read: [] })

/**
 * Normalize a name into exactly ONE `Local.` prefix (UDR-0111 D7).
 *
 * Idempotent by construction: a value that already carries the prefix (however many
 * times) yields a single one, and a bare name acquires one -- which is what the backend
 * does on save / compile, so the candidate list mirrors that rule instead of
 * re-inventing it per call site. A name in ANOTHER namespace is left alone, and an
 * empty name yields '' (the caller drops it).
 */
function toLocalPath(value: unknown): string {
  let s = str(value).trim()
  if (!s) return ''
  if (/^(Workflow|System)\./.test(s)) return s
  while (s.startsWith('Local.')) s = s.slice('Local.'.length).trim()
  return s ? `Local.${s}` : ''
}

/** Collect every `Local.*` name assigned anywhere in the document, recursively. */
function collectLocalNames(actions: WorkflowAction[] | undefined, out: Set<string>): void {
  for (const a of actions ?? []) {
    // UDR-0111 D7: ONE normalizer for both halves. Previously the general path admitted
    // only already-prefixed names (so a bare `count` vanished) while the Foreach path
    // prefixed unconditionally (so `Local.item` became `Local.Local.item`).
    const add = (v: unknown) => {
      const s = toLocalPath(v)
      if (s.startsWith('Local.')) out.add(s)
    }
    add(a.variable)
    add(a.table)
    add(a.conversationId)
    for (const as of a.assignments ?? []) add(as.variable)
    for (const k of ['responseObject', 'messages', 'result'] as const) add(a.output?.[k])
    if (a.kind === 'Foreach') {
      for (const n of [a.itemName, a.indexName]) add(n)
    }
    collectLocalNames(a.then, out)
    collectLocalNames(a.else, out)
    collectLocalNames(a.actions, out)
    collectLocalNames(a.elseActions, out)
    for (const c of a.conditions ?? []) collectLocalNames(c.actions, out)
  }
}

/**
 * Derive the candidate lists from the document already in memory (no new endpoint).
 *
 * Every candidate is a COMPLETE path that already exists in this document: a `Local.*`
 * name assigned somewhere in the actions, or a name declared in `inputs:` / `outputs:`.
 * No bare namespace stubs (v0.122.0) -- see VariableCandidates above. An empty document
 * therefore offers nothing, which is correct: there is no variable to reuse yet.
 */
function buildVariableCandidates(doc: WorkflowDocument): VariableCandidates {
  const locals = new Set<string>()
  collectLocalNames(doc.actions, locals)
  const inputs = Object.keys(doc.inputs ?? {}).map((n) => `Workflow.Inputs.${n}`)
  const outputs = Object.keys(doc.outputs ?? {}).map((n) => `Workflow.Outputs.${n}`)
  const sorted = [...locals].sort()
  return {
    write: [...sorted, ...outputs],
    read: [...sorted, ...inputs, ...outputs],
  }
}

/**
 * A variable-path input suggesting the variables this document already has (CTR-0184 v3;
 * namespace stubs dropped in v0.122.0). The placeholder teaches the syntax; the datalist
 * offers only complete, existing paths, so picking one always yields a usable value.
 *
 * Purely an input aid: the backend remains the single validator (UDR-0101 D9), so a
 * value typed by hand is accepted here and judged server-side.
 */
function VariableInput({
  value,
  onChange,
  placeholder,
  mode = 'write',
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  mode?: 'write' | 'read'
}) {
  const candidates = useContext(VariableCandidatesContext)
  const listId = useId()
  const options = mode === 'write' ? candidates.write : candidates.read
  return (
    <>
      <input
        className={CONTROL}
        list={listId}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder ?? 'Local.name'}
      />
      <datalist id={listId}>
        {options.map((o) => (
          <option key={o} value={o} />
        ))}
      </datalist>
    </>
  )
}

function newAction(kind: string): WorkflowAction {
  const id = `${kind.toLowerCase()}_${shortId()}`
  switch (kind) {
    // Variable
    case 'SetVariable':
      return { kind, id, variable: '', value: '' }
    case 'SetMultipleVariables':
      return { kind, id, assignments: [{ variable: '', value: '' }] }
    case 'SetTextVariable':
      // The executor reads `text`, not `value` (PRP-0122 FACT 2).
      return { kind, id, variable: '', text: '' }
    case 'ResetVariable':
      return { kind, id, variable: '' }
    case 'ClearAllVariables':
      return { kind, id }
    case 'ParseValue':
      // The executor reads `value` (+ optional `valueType`), not `source`.
      return { kind, id, variable: '', value: '', valueType: '' }
    case 'EditTableV2':
      // The executor reads `item` / `value` + `key` / `index`, not `row`.
      return { kind, id, table: '', operation: 'add', item: {}, key: '' }
    // Control flow (nested)
    case 'If':
      // biome-ignore lint/suspicious/noThenProperty: MAF If action serializes a `then` branch, not a thenable
      return { kind, id, condition: '', then: [] }
    case 'ConditionGroup':
      return { kind, id, conditions: [{ condition: '', id: `${id}_c1`, actions: [] }] }
    case 'Foreach':
      return { kind, id, source: '', actions: [] }
    case 'BreakLoop':
      return { kind, id }
    case 'ContinueLoop':
      return { kind, id }
    case 'GotoAction':
      return { kind, id, actionId: '' }
    // Output
    case 'SendActivity':
      return { kind, id, activity: { text: '' } }
    // Agent
    case 'InvokeAzureAgent':
      return { kind, id, agentName: '' }
    // Tool (jailed)
    case 'InvokeFunctionTool':
      return { kind, id, functionName: '' }
    case 'InvokeMcpTool':
      return { kind, id, serverLabel: '', toolName: '' }
    // HTTP (jailed)
    case 'HttpRequestAction':
      return { kind, id, method: 'GET', url: '' }
    // Human-in-the-loop
    case 'Question':
      // allowFreeText is ALWAYS written to the YAML (never left implicit) so the
      // authored document states the answer policy outright (PRP-0122 follow-up).
      return { kind, id, question: { text: '' }, variable: '', allowFreeText: true }
    case 'RequestExternalInput':
      return { kind, id, prompt: { text: '' }, variable: '' }
    // Workflow control
    case 'EndWorkflow':
      return { kind, id }
    case 'EndConversation':
      return { kind, id }
    case 'CreateConversation':
      return { kind, id, conversationId: '' }
    default:
      return { kind, id }
  }
}

// ===========================================================================
// React Flow nodes
// ===========================================================================
interface StepData extends Record<string, unknown> {
  label: string
  sub?: string
  selected: boolean
  onSelect: () => void
}
interface LaneData extends Record<string, unknown> {
  label: string
}

const StepNode = memo(({ data }: NodeProps<Node<StepData>>) => {
  const Icon = ICON_BY_KIND[data.label] ?? WorkflowIcon
  return (
    <button
      type="button"
      onClick={data.onSelect}
      style={{ width: NODE_W }}
      className={cn(
        'flex h-full items-center gap-2 rounded-md border px-3 py-2 text-left text-xs shadow-sm',
        data.selected ? 'border-primary bg-primary/10' : 'border-border bg-background',
      )}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0">
        <span className="block max-w-[140px] truncate font-medium">{data.label}</span>
        {data.sub && <span className="block max-w-[140px] truncate text-[10px] text-muted-foreground">{data.sub}</span>}
      </span>
    </button>
  )
})
StepNode.displayName = 'StepNode'

const ContainerNode = memo(({ data }: NodeProps<Node<StepData>>) => {
  const Icon = ICON_BY_KIND[data.label] ?? GitBranch
  return (
    <div
      className={cn(
        'h-full w-full rounded-md border-2 border-dashed',
        data.selected ? 'border-primary bg-primary/5' : 'border-border bg-muted/20',
      )}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <button
        type="button"
        onClick={data.onSelect}
        className="flex w-full items-center gap-1.5 rounded-t px-2 py-1 text-left text-[11px] font-medium hover:bg-accent/40">
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="truncate">{data.label}</span>
        {data.sub && <span className="truncate text-[10px] text-muted-foreground">{data.sub}</span>}
      </button>
    </div>
  )
})
ContainerNode.displayName = 'ContainerNode'

const LaneNode = memo(({ data }: NodeProps<Node<LaneData>>) => (
  <div className="h-full w-full rounded border border-border/70 bg-background/40">
    <div className="px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
      {data.label}
    </div>
  </div>
))
LaneNode.displayName = 'LaneNode'

const nodeTypes = { step: StepNode, container: ContainerNode, lane: LaneNode }

/**
 * Editable smooth-step edge used for the GotoAction back-edge (v0.115.2). It renders a
 * smoothstep (orthogonal, rounded) path whose bend CENTER is draggable, so the jump edge
 * bows off the node column by default (clearing the nodes between the jump and its
 * target) and can be re-routed off any node it still overlaps. The bend offset is
 * ephemeral (like node drag positions); the YAML carries no visual routing.
 */
const EditableStepEdge = memo(
  ({ id, sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, markerEnd, style }: EdgeProps) => {
    const { getViewport } = useReactFlow()
    // Default bend: left of the column (further for longer jumps) so the step route
    // clears the intervening nodes.
    const bow = Math.min(240, 70 + Math.abs(sourceY - targetY) * 0.25)
    const defaultCenter = { x: (sourceX + targetX) / 2 - bow, y: (sourceY + targetY) / 2 }
    const [center, setCenter] = useState<{ x: number; y: number } | null>(null)
    const c = center ?? defaultCenter
    const dragRef = useRef<{ x: number; y: number } | null>(null)

    const [edgePath] = getSmoothStepPath({
      sourceX,
      sourceY,
      sourcePosition,
      targetX,
      targetY,
      targetPosition,
      borderRadius: 12,
      centerX: c.x,
      centerY: c.y,
    })

    const onPointerDown = (e: ReactPointerEvent) => {
      e.stopPropagation()
      ;(e.target as Element).setPointerCapture(e.pointerId)
      dragRef.current = { x: e.clientX, y: e.clientY }
    }
    const onPointerMove = (e: ReactPointerEvent) => {
      const start = dragRef.current
      if (!start) return
      const zoom = getViewport().zoom || 1
      const dx = (e.clientX - start.x) / zoom
      const dy = (e.clientY - start.y) / zoom
      dragRef.current = { x: e.clientX, y: e.clientY }
      setCenter((prev) => {
        const base = prev ?? defaultCenter
        return { x: base.x + dx, y: base.y + dy }
      })
    }
    const onPointerUp = () => {
      dragRef.current = null
    }

    return (
      <>
        <BaseEdge id={id} path={edgePath} markerEnd={markerEnd} style={style} />
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan absolute flex items-center gap-1"
            style={{ transform: `translate(-50%, -50%) translate(${c.x}px, ${c.y}px)`, pointerEvents: 'all' }}>
            <button
              type="button"
              aria-label="Drag to move the goto edge"
              title="Drag to move the goto edge"
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              className="h-3 w-3 cursor-move rounded-full border-2 border-primary bg-background"
            />
            <span className="rounded bg-background/90 px-1 text-[9px] font-medium text-primary">goto</span>
          </div>
        </EdgeLabelRenderer>
      </>
    )
  },
)
EditableStepEdge.displayName = 'EditableStepEdge'

// Override the built-in `smoothstep` type with the editable variant (v0.115.2): every
// edge that asks for `type: 'smoothstep'` (only the GotoAction back-edge here) becomes a
// draggable, animated step edge.
const edgeTypes = { smoothstep: EditableStepEdge }

// ===========================================================================
export function DeclarativeWorkflowEditor({ open, onOpenChange, editId, onSaved }: Props) {
  const api = useWorkflowAuthoring()
  const [doc, setDoc] = useState<WorkflowDocument>(emptyDocument)
  const [rawMode, setRawMode] = useState(false)
  const [rawYaml, setRawYaml] = useState('')
  const [agentNames, setAgentNames] = useState<string[]>([])
  const [validation, setValidation] = useState<WorkflowValidationResult | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [leaveConfirm, setLeaveConfirm] = useState(false)
  const [showIO, setShowIO] = useState(false)
  const [showMeta, setShowMeta] = useState(false)
  // UDR-0111 D2: a create-mode editor ADOPTS the id its first successful save assigns,
  // so every later save in the session is an UPDATE of that id. Without this, D1
  // (save no longer closes) would let a second save create a second workflow silently.
  // Held only in the component instance -- the manager unmounts the editor on close.
  const [savedId, setSavedId] = useState<string | null>(null)
  // UDR-0111 D1: a successful save is reported INLINE (the editor stays open, so the
  // dismissal that used to signal success is gone). Auto-dismissed below.
  const [saveNotice, setSaveNotice] = useState<string | null>(null)

  // ---- initial load: Prompt agent names + source for edit ----
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setDirty(false)
    setRawMode(false)
    setSelected(null)
    setSavedId(null)
    setSaveNotice(null)
    ;(async () => {
      try {
        const agentsRes = await fetch('/api/agents').then((r) => (r.ok ? r.json() : { agents: [] }))
        if (!cancelled) {
          const names = (agentsRes.agents ?? [])
            .filter((a: { source?: string }) => a.source !== 'core')
            .map((a: { name?: string }) => a.name)
            .filter(Boolean) as string[]
          // CORE is invokable too (by its display name).
          const core = (agentsRes.agents ?? []).find((a: { source?: string }) => a.source === 'core') as
            | { name?: string }
            | undefined
          setAgentNames(core?.name ? [core.name, ...names] : names)
        }
        if (editId) {
          const src = await api.loadSource(editId)
          if (cancelled) return
          setDoc({ ...emptyDocument(), ...src.document, actions: src.document.actions ?? [] })
          setRawYaml(src.yaml)
        } else {
          setDoc(emptyDocument())
          setRawYaml('')
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load editor')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, editId, api])

  // ---- debounced validation (single source of truth: the backend) ----
  const validateTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!open || loading) return
    if (validateTimer.current) clearTimeout(validateTimer.current)
    validateTimer.current = setTimeout(async () => {
      try {
        const body = rawMode ? { yaml: rawYaml } : { document: doc }
        setValidation(await api.validate(body))
      } catch (err) {
        setValidation({
          valid: false,
          error: err instanceof Error ? err.message : 'Validation failed',
          warnings: [],
          yaml: null,
        })
      }
    }, 400)
    return () => {
      if (validateTimer.current) clearTimeout(validateTimer.current)
    }
  }, [doc, rawYaml, rawMode, open, loading, api])

  const patch = useCallback((p: Partial<WorkflowDocument>) => {
    setDoc((d) => ({ ...d, ...p }))
    setDirty(true)
  }, [])

  const setActions = useCallback((next: WorkflowAction[]) => {
    setDoc((d) => ({ ...d, actions: next }))
    setDirty(true)
  }, [])

  // ---- canvas: elk layout (recomputed on structure change) ----
  const [layoutNodes, setLayoutNodes] = useState<Node[]>([])
  useEffect(() => {
    if (!open) return
    let cancelled = false
    // CTR-0188: the ONE document -> laid-out-graph derivation, shared with the run
    // canvas (CTR-0187) so the two surfaces cannot disagree about a workflow's shape.
    buildWorkflowGraph(doc.actions).then((g) => {
      if (!cancelled) setLayoutNodes(g.nodes)
    })
    return () => {
      cancelled = true
    }
  }, [doc.actions, open])

  // Local node state seeded from the elk layout so nodes can be dragged (a structural
  // change re-runs elk and reseeds; manual positions are ephemeral, v0.115.1).
  const [rfNodes, setRfNodes] = useState<Node[]>([])
  useEffect(() => {
    setRfNodes(layoutNodes)
  }, [layoutNodes])
  const onNodesChange = useCallback(
    (changes: Array<NodeChange<Node>>) => setRfNodes((nds) => applyNodeChanges(changes, nds)),
    [],
  )

  const nodes = useMemo(
    () =>
      rfNodes.map((n) =>
        n.type === 'lane'
          ? n
          : { ...n, data: { ...n.data, selected: selected === n.id, onSelect: () => setSelected(n.id) } },
      ),
    [rfNodes, selected],
  )
  const edges = useMemo(() => buildEdges(doc.actions), [doc.actions])

  // Variable-name candidates for every variable input, derived from the document in
  // memory (CTR-0184 v3, UDR-0105 D3). No endpoint, no backend state.
  const variableCandidates = useMemo(() => buildVariableCandidates(doc), [doc])

  // ---- save / close ----
  // The effective write target (UDR-0111 D2): the prop in edit mode, else the id
  // adopted from the first successful save. `null` means "nothing exists yet".
  const boundId = editId ?? savedId

  const handleSave = useCallback(async () => {
    setSaving(true)
    setError(null)
    setSaveNotice(null)
    try {
      const body = rawMode ? { yaml: rawYaml } : { document: doc, name: doc.name }
      const result = await api.save(body, boundId)
      const id = result.id ?? boundId ?? undefined
      setDirty(false)
      // Adopt the identity BEFORE reporting, so a fast second click updates (D2).
      if (id) setSavedId(id)
      setSaveNotice('Saved. This editor stays open -- use Close when you are done.')
      onSaved(id)
      // UDR-0111 D1: no onOpenChange(false) here. Close is the only exit.
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save workflow')
    } finally {
      setSaving(false)
    }
  }, [rawMode, rawYaml, doc, boundId, api, onSaved])

  // Auto-dismiss the inline save notice (UDR-0111 D1).
  useEffect(() => {
    if (!saveNotice) return
    const t = setTimeout(() => setSaveNotice(null), 4000)
    return () => clearTimeout(t)
  }, [saveNotice])

  const requestClose = useCallback(() => {
    if (dirty) {
      setLeaveConfirm(true)
      return
    }
    onOpenChange(false)
  }, [dirty, onOpenChange])

  const warnings = validation?.warnings ?? []
  const validationError = validation?.error ?? null
  const canSave =
    !saving && !loading && (rawMode ? rawYaml.trim().length > 0 : doc.name.trim().length > 0) && !validationError

  return (
    <VariableCandidatesContext.Provider value={variableCandidates}>
      <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(true) : requestClose())}>
        <DialogContent className="flex h-screen w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-background p-0 sm:rounded-none [&>button]:hidden">
          {/* UDR-0111 D4: the VISIBLE title names the entity KIND (constant for the whole
              session, which now spans create AND edit); the ACCESSIBLE name keeps the
              operation, because a screen-reader user cannot see the primary button label. */}
          <DialogTitle className="sr-only">
            {editId ? 'Edit Declarative Workflow' : 'Create Declarative Workflow'}
          </DialogTitle>
          <DialogDescription className="sr-only">
            Compose a declarative workflow graph: sequential actions, control flow and agent invocations.
          </DialogDescription>
          <div className="flex shrink-0 items-center justify-between border-b px-5 py-3">
            <div className="flex items-center gap-2">
              <WorkflowIcon className="h-5 w-5 text-primary" />
              <div>
                <div className="text-sm font-semibold">Declarative Workflow</div>
                <div className="text-[11px] text-muted-foreground">
                  A workflow orchestrates declarative Prompt agents. Credentials and sampling are resolved by
                  ChatWalaʻau.
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <input type="checkbox" checked={rawMode} onChange={(e) => setRawMode(e.target.checked)} />
                Edit raw YAML
              </label>
              <Button variant="outline" size="sm" onClick={requestClose} disabled={saving}>
                Close
              </Button>
              <Button size="sm" onClick={handleSave} disabled={!canSave}>
                {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                {/* UDR-0111 D3: the label states the write it will perform -- Create while
                    nothing exists, Save once an id is bound (prop or adopted). */}
                {boundId ? 'Save' : 'Create'}
              </Button>
            </div>
          </div>

          {loading ? (
            <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
            </div>
          ) : (
            <PanelGroup direction="horizontal" className="min-h-0 flex-1">
              {/* LEFT: workflow fields + inputs/outputs + recursive action tree */}
              <Panel defaultSize={36} minSize={24}>
                <div
                  className={cn('h-full space-y-3 overflow-y-auto p-4', rawMode && 'pointer-events-none opacity-50')}>
                  <Field label="Name (identifier)">
                    <input
                      className={CONTROL}
                      value={doc.name}
                      onChange={(e) => patch({ name: e.target.value })}
                      placeholder="TriageFlow"
                    />
                  </Field>
                  {/* display name / description / max turns (collapsible) */}
                  <div className="rounded-md border">
                    <button
                      type="button"
                      onClick={() => setShowMeta((s) => !s)}
                      className="flex w-full items-center justify-between px-2 py-1.5 text-[11px] font-medium text-muted-foreground hover:bg-accent">
                      <span>Display name / description / max turns</span>
                      <span className="text-[10px]">{showMeta ? 'Hide' : 'Show'}</span>
                    </button>
                    {showMeta && (
                      <div className="space-y-3 border-t p-2">
                        <Field label="Display name (optional)">
                          <input
                            className={CONTROL}
                            value={doc.displayName ?? ''}
                            onChange={(e) => patch({ displayName: e.target.value })}
                            placeholder="Triage Flow"
                          />
                        </Field>
                        <Field label="Description">
                          <textarea
                            className={cn(CONTROL, 'h-14 resize-none')}
                            value={doc.description ?? ''}
                            onChange={(e) => patch({ description: e.target.value })}
                          />
                        </Field>
                        <Field label="Max turns (optional)">
                          <input
                            className={CONTROL}
                            type="number"
                            min={1}
                            value={doc.maxTurns ?? ''}
                            onChange={(e) => patch({ maxTurns: e.target.value ? Number(e.target.value) : null })}
                          />
                        </Field>
                      </div>
                    )}
                  </div>

                  {/* inputs / outputs (collapsible) */}
                  <div className="rounded-md border">
                    <button
                      type="button"
                      onClick={() => setShowIO((s) => !s)}
                      className="flex w-full items-center justify-between px-2 py-1.5 text-[11px] font-medium text-muted-foreground hover:bg-accent">
                      <span>Inputs / outputs (optional)</span>
                      <span className="text-[10px]">
                        {Object.keys(doc.inputs ?? {}).length} in · {Object.keys(doc.outputs ?? {}).length} out
                      </span>
                    </button>
                    {showIO && (
                      <div className="space-y-3 border-t p-2">
                        <IOEditor
                          title="Inputs"
                          seedKey={`${editId}:${loading}`}
                          value={doc.inputs ?? {}}
                          withDescription
                          onChange={(v) => patch({ inputs: v })}
                        />
                        <IOEditor
                          title="Outputs"
                          seedKey={`${editId}:${loading}`}
                          value={doc.outputs ?? {}}
                          onChange={(v) => patch({ outputs: v })}
                        />
                      </div>
                    )}
                  </div>

                  <div className="flex items-center justify-between pt-1">
                    <span className="text-[11px] font-medium text-muted-foreground">Actions</span>
                  </div>
                  <ActionList
                    actions={doc.actions}
                    pathKey="a"
                    agentNames={agentNames}
                    selected={selected}
                    onSelect={setSelected}
                    allowLoop={false}
                    update={setActions}
                  />
                </div>
              </Panel>

              <PanelResizeHandle className="w-1 bg-border data-[resize-handle-state=drag]:bg-primary" />

              {/* CENTER: React Flow nested container graph */}
              <Panel defaultSize={38} minSize={22}>
                <div className="h-full">
                  <ReactFlowProvider>
                    <ReactFlow
                      nodes={nodes}
                      edges={edges}
                      nodeTypes={nodeTypes}
                      edgeTypes={edgeTypes}
                      onNodesChange={onNodesChange}
                      nodesConnectable={false}
                      nodesDraggable
                      fitView
                      minZoom={0.15}
                      proOptions={{ hideAttribution: true }}>
                      <Background gap={16} />
                    </ReactFlow>
                  </ReactFlowProvider>
                </div>
              </Panel>

              <PanelResizeHandle className="w-1 bg-border data-[resize-handle-state=drag]:bg-primary" />

              {/* RIGHT: monaco YAML + warnings */}
              <Panel defaultSize={26} minSize={16}>
                <div className="flex h-full flex-col">
                  <div className="flex items-center justify-between border-b px-3 py-1.5 text-[11px] text-muted-foreground">
                    <span>{rawMode ? 'YAML (editing)' : 'YAML (canonical preview)'}</span>
                  </div>
                  <div className="min-h-0 flex-1">
                    <Editor
                      language="yaml"
                      theme="vs"
                      value={rawMode ? rawYaml : (validation?.yaml ?? '')}
                      onChange={(v) => {
                        if (rawMode) {
                          setRawYaml(v ?? '')
                          setDirty(true)
                        }
                      }}
                      options={{
                        readOnly: !rawMode,
                        minimap: { enabled: false },
                        fontSize: 12,
                        automaticLayout: true,
                        scrollBeyondLastLine: false,
                        tabSize: 2,
                        wordWrap: 'on',
                      }}
                    />
                  </div>
                  {(validationError || warnings.length > 0) && (
                    <div className="max-h-48 shrink-0 overflow-y-auto border-t bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
                      {validationError && (
                        <div className="mb-1 flex items-start gap-1.5 font-medium">
                          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {validationError}
                        </div>
                      )}
                      {warnings.length > 0 && (
                        <>
                          <div className="mb-1 flex items-center gap-1.5 font-medium">
                            <TriangleAlert className="h-3.5 w-3.5 shrink-0" /> Resolve before running:
                          </div>
                          <ul className="space-y-0.5 pl-5">
                            {warnings.map((w) => (
                              <li key={w} className="list-disc">
                                {w}
                              </li>
                            ))}
                          </ul>
                        </>
                      )}
                    </div>
                  )}
                </div>
              </Panel>
            </PanelGroup>
          )}

          {error && (
            <div className="shrink-0 border-t bg-destructive/10 px-4 py-2 text-xs text-destructive">{error}</div>
          )}
          {/* UDR-0111 D1: inline save acknowledgement, in the header-error position (a
              global toast would render behind this full-screen overlay). */}
          {!error && saveNotice && (
            <output className="block shrink-0 border-t bg-primary/10 px-4 py-2 text-xs text-primary">
              {saveNotice}
            </output>
          )}

          {leaveConfirm && (
            <div className="absolute inset-0 z-20 flex items-center justify-center bg-background/80">
              <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
                <p className="text-sm font-medium">Discard unsaved changes?</p>
                <p className="mt-1 text-xs text-muted-foreground">Your edits to this workflow have not been saved.</p>
                <div className="mt-3 flex justify-end gap-2">
                  <Button variant="outline" size="sm" onClick={() => setLeaveConfirm(false)}>
                    Keep editing
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => {
                      setLeaveConfirm(false)
                      onOpenChange(false)
                    }}>
                    Discard
                  </Button>
                </div>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </VariableCandidatesContext.Provider>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <span className="mb-1 block text-[11px] font-medium text-muted-foreground">{label}</span>
      {children}
    </div>
  )
}

/**
 * A comma-separated list bound to a string[] field (v0.117.1).
 *
 * The naive binding -- value={list.join(', ')} with onChange splitting and filtering --
 * cannot be typed in: the keystroke that adds the separator produces a trailing empty
 * item, the filter drops it, the join puts the value back WITHOUT the comma, and the
 * character the operator just typed disappears. The same happens to the space after it.
 *
 * So the input owns the RAW TEXT while it is being edited and publishes the parsed array
 * upward on every keystroke. The text is re-seeded from the model only when the value
 * arrives from elsewhere (a different action selected, a YAML edit), never from the array
 * this component itself just produced -- otherwise the round trip reappears.
 */
function CsvInput({
  value,
  onChange,
  placeholder,
}: {
  value: string[]
  onChange: (next: string[]) => void
  placeholder?: string
}) {
  const parse = (text: string) =>
    text
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
  const [text, setText] = useState(() => value.join(', '))
  // Re-seed only when the incoming list differs from what the current text parses to.
  // `text` and `parse` are read but deliberately NOT dependencies: depending on them
  // would re-run this on every keystroke and fight the user's typing. The suppression
  // must sit directly above the hook -- placed inside the callback it is inert, which
  // is how the rule went unsuppressed.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-seed on external change only
  useEffect(() => {
    const mine = parse(text)
    if (mine.length !== value.length || mine.some((v, i) => v !== value[i])) setText(value.join(', '))
  }, [value])
  return (
    <input
      className={CONTROL}
      value={text}
      onChange={(e) => {
        setText(e.target.value)
        onChange(parse(e.target.value))
      }}
      placeholder={placeholder}
    />
  )
}

// ---- Add-action palette dropdown (grouped by category) ---------------------
function AddActionMenu({ onAdd, allowLoop }: { onAdd: (kind: string) => void; allowLoop: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="relative">
      <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]" onClick={() => setOpen((s) => !s)}>
        <Plus className="mr-1 h-3 w-3" /> Add
      </Button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} aria-hidden />
          <div className="absolute right-0 top-full z-20 mt-1 max-h-96 w-56 overflow-y-auto rounded-md border bg-background p-1 shadow-lg">
            {ACTION_CATEGORIES.map(({ category, kinds }) => {
              const items = kinds.filter((k) => allowLoop || !LOOP_ONLY_KINDS.has(k.kind))
              if (items.length === 0) return null
              return (
                <div key={category} className="py-0.5">
                  <div className="px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-muted-foreground/70">
                    {category}
                  </div>
                  {items.map(({ kind, label, icon: Icon }) => (
                    <button
                      key={kind}
                      type="button"
                      onClick={() => {
                        onAdd(kind)
                        setOpen(false)
                      }}
                      className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs hover:bg-accent">
                      <Icon className="h-3.5 w-3.5 text-muted-foreground" /> {label}
                    </button>
                  ))}
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}

// ---- recursive action tree (LEFT panel) ------------------------------------
interface ActionListProps {
  actions: WorkflowAction[]
  pathKey: string
  agentNames: string[]
  selected: string | null
  onSelect: (key: string | null) => void
  allowLoop: boolean
  update: (next: WorkflowAction[]) => void
}

function ActionList({ actions, pathKey, agentNames, selected, onSelect, allowLoop, update }: ActionListProps) {
  const patchAt = (i: number, p: Partial<WorkflowAction>) =>
    update(actions.map((a, idx) => (idx === i ? { ...a, ...p } : a)))
  const removeAt = (i: number) => {
    onSelect(null)
    update(actions.filter((_, idx) => idx !== i))
  }
  const moveAt = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (j < 0 || j >= actions.length) return
    const next = [...actions]
    ;[next[i], next[j]] = [next[j], next[i]]
    update(next)
  }
  const addKind = (kind: string) => {
    // Insert right AFTER the selected sibling in this list; otherwise append (v0.115.1).
    let insertAt = actions.length
    for (let idx = 0; idx < actions.length; idx++) {
      if (selected === `${pathKey}/${idx}`) {
        insertAt = idx + 1
        break
      }
    }
    const next = [...actions.slice(0, insertAt), newAction(kind), ...actions.slice(insertAt)]
    onSelect(`${pathKey}/${insertAt}`)
    update(next)
  }

  // Drag-and-drop reordering within this list (v0.115.2). Each list is its own
  // DndContext so nested lanes reorder independently. Sortable ids are the (unique)
  // action ids, de-duplicated against a transient duplicate-id edit state.
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }))
  const seenIds = new Set<string>()
  const rowIds = actions.map((a, i) => {
    let rid = str(a.id).trim() || `${pathKey}#${i}`
    if (seenIds.has(rid)) rid = `${rid}#${i}`
    seenIds.add(rid)
    return rid
  })
  const onDragEnd = (e: DragEndEvent) => {
    const from = rowIds.indexOf(String(e.active.id))
    const to = e.over ? rowIds.indexOf(String(e.over.id)) : -1
    if (from < 0 || to < 0 || from === to) return
    onSelect(null)
    update(arrayMove(actions, from, to))
  }

  return (
    <div className="space-y-1">
      <div className="flex justify-end">
        <AddActionMenu onAdd={addKind} allowLoop={allowLoop} />
      </div>
      {actions.length === 0 && <p className="text-[11px] text-muted-foreground">No actions. Add a step.</p>}
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={rowIds} strategy={verticalListSortingStrategy}>
          {actions.map((a, i) => (
            <ActionRow
              key={rowIds[i]}
              sortId={rowIds[i]}
              action={a}
              index={i}
              pathKey={pathKey}
              agentNames={agentNames}
              selected={selected}
              onSelect={onSelect}
              allowLoop={allowLoop}
              patchAt={patchAt}
              removeAt={removeAt}
              moveAt={moveAt}
            />
          ))}
        </SortableContext>
      </DndContext>
    </div>
  )
}

interface ActionRowProps {
  sortId: string
  action: WorkflowAction
  index: number
  pathKey: string
  agentNames: string[]
  selected: string | null
  onSelect: (key: string | null) => void
  allowLoop: boolean
  patchAt: (i: number, p: Partial<WorkflowAction>) => void
  removeAt: (i: number) => void
  moveAt: (i: number, dir: -1 | 1) => void
}

function ActionRow({
  sortId,
  action: a,
  index: i,
  pathKey,
  agentNames,
  selected,
  onSelect,
  allowLoop,
  patchAt,
  removeAt,
  moveAt,
}: ActionRowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: sortId })
  const key = `${pathKey}/${i}`
  const lanes = actionLanes(a, key)
  const Icon = ICON_BY_KIND[a.kind] ?? WorkflowIcon
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={cn(
        'rounded-md border',
        selected === key ? 'border-primary' : 'border-border',
        isDragging && 'relative z-10 opacity-70 shadow-md',
      )}>
      <div className="flex items-center gap-1 px-2 py-1.5 text-xs">
        <button
          type="button"
          aria-label="Drag to reorder"
          className="cursor-grab touch-none rounded p-0.5 text-muted-foreground hover:bg-accent active:cursor-grabbing"
          {...attributes}
          {...listeners}>
          <GripVertical className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={() => onSelect(selected === key ? null : key)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-muted text-[10px]">
            {i + 1}
          </span>
          <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1">
            <span className="block truncate font-medium">{a.kind}</span>
            <span className="block truncate text-[10px] text-muted-foreground">{actionSummary(a)}</span>
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            aria-label="Move up"
            className="rounded p-0.5 text-muted-foreground hover:bg-accent"
            onClick={() => moveAt(i, -1)}>
            <ArrowUp className="h-3 w-3" />
          </button>
          <button
            type="button"
            aria-label="Move down"
            className="rounded p-0.5 text-muted-foreground hover:bg-accent"
            onClick={() => moveAt(i, 1)}>
            <ArrowDown className="h-3 w-3" />
          </button>
          <button
            type="button"
            aria-label="Remove action"
            className="rounded p-0.5 text-muted-foreground hover:text-destructive"
            onClick={() => removeAt(i)}>
            <X className="h-3 w-3" />
          </button>
        </div>
      </div>
      {selected === key && (
        <div className="space-y-2 border-t p-2">
          <div className="grid grid-cols-2 gap-2">
            <Field label="Action id (must be unique)">
              <input
                className={CONTROL}
                value={str(a.id)}
                onChange={(e) => patchAt(i, { id: e.target.value })}
                placeholder="unique_id"
              />
            </Field>
            {/* Shared across all action kinds (UDR-0105 D7): MAF ignores displayName,
                ChatWalaʻau uses it to label the run-progress node. */}
            <Field label="Display name (optional)">
              <input
                className={CONTROL}
                value={str(a.displayName)}
                onChange={(e) => patchAt(i, { displayName: e.target.value })}
                placeholder="Shown while running"
              />
            </Field>
          </div>
          <ActionForm action={a} agentNames={agentNames} onChange={(p) => patchAt(i, p)} />
        </div>
      )}
      {lanes.length > 0 && (
        <div className="space-y-2 border-t bg-muted/20 p-2">
          {a.kind === 'ConditionGroup' && (
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-medium text-muted-foreground">Branches</span>
              <Button
                variant="ghost"
                size="sm"
                className="h-5 px-1.5 text-[10px]"
                onClick={() => {
                  const conds = a.conditions ?? []
                  patchAt(i, {
                    conditions: [...conds, { condition: '', id: `${a.id ?? key}_c${conds.length + 1}`, actions: [] }],
                  })
                }}>
                <Plus className="mr-0.5 h-3 w-3" /> Branch
              </Button>
            </div>
          )}
          {lanes.map((lane, li) => (
            <div key={lane.key} className="rounded border border-dashed border-border/70 pl-2">
              <div className="flex items-center justify-between py-1 pr-1">
                <span className="flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  <ListTree className="h-3 w-3" /> {lane.label}
                </span>
                {a.kind === 'ConditionGroup' && li < (a.conditions ?? []).length && (
                  <input
                    className="w-32 rounded border border-input bg-background px-1 py-0.5 text-[10px] outline-none"
                    placeholder="=condition"
                    value={str((a.conditions ?? [])[li]?.condition)}
                    onChange={(e) => {
                      const conds = a.conditions ?? []
                      patchAt(i, {
                        conditions: conds.map((c, jj) => (jj === li ? { ...c, condition: e.target.value } : c)),
                      })
                    }}
                  />
                )}
              </div>
              <ActionList
                actions={lane.actions}
                pathKey={lane.key}
                agentNames={agentNames}
                selected={selected}
                onSelect={onSelect}
                allowLoop={lane.loop || allowLoop}
                update={(next) => patchAt(i, lane.patch(next))}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---- per-action forms ------------------------------------------------------
function JailedNote({ kind }: { kind: string }) {
  const note = JAILED_NOTE[kind]
  if (!note) return null
  return <p className="rounded bg-muted/40 px-2 py-1 text-[10px] text-muted-foreground">{note}</p>
}

function ActionForm({
  action,
  agentNames,
  onChange,
}: {
  action: WorkflowAction
  agentNames: string[]
  onChange: (p: Partial<WorkflowAction>) => void
}) {
  const a = action
  switch (a.kind) {
    case 'SetVariable':
      return (
        <div className="space-y-2">
          <Field label="Variable">
            <VariableInput value={str(a.variable)} onChange={(v) => onChange({ variable: v })} />
          </Field>
          <Field label="Value">
            <input className={CONTROL} value={str(a.value)} onChange={(e) => onChange({ value: e.target.value })} />
          </Field>
        </div>
      )
    case 'SetTextVariable':
      return (
        <div className="space-y-2">
          <Field label="Variable">
            <VariableInput value={str(a.variable)} onChange={(v) => onChange({ variable: v })} />
          </Field>
          <Field label="Text">
            <input
              className={CONTROL}
              value={str(a.text)}
              onChange={(e) => onChange({ text: e.target.value })}
              placeholder='=Concat("Hello ", Workflow.Inputs.name)'
            />
          </Field>
        </div>
      )
    case 'SetMultipleVariables':
      return (
        <Field label="Assignments (variable = value)">
          <AssignmentEditor value={a.assignments ?? []} onChange={(v) => onChange({ assignments: v })} />
        </Field>
      )
    case 'ResetVariable':
      return (
        <Field label="Variable">
          <VariableInput value={str(a.variable)} onChange={(v) => onChange({ variable: v })} />
        </Field>
      )
    case 'ClearAllVariables':
      return <p className="text-[11px] text-muted-foreground">Clears every Local.* variable. No fields.</p>
    case 'ParseValue':
      return (
        <div className="space-y-2">
          <Field label="Value (literal or expression)">
            <input
              className={CONTROL}
              value={str(a.value)}
              onChange={(e) => onChange({ value: e.target.value })}
              placeholder="=Workflow.Inputs.customerJson"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Target variable">
              <VariableInput value={str(a.variable)} onChange={(v) => onChange({ variable: v })} />
            </Field>
            <Field label="Value type (optional)">
              <select
                className={CONTROL}
                value={str(a.valueType)}
                onChange={(e) => onChange({ valueType: e.target.value })}>
                <option value="">(auto)</option>
                {PARSE_VALUE_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </div>
      )
    case 'EditTableV2':
      return (
        <div className="space-y-2">
          <Field label="Table (variable)">
            <VariableInput
              value={str(a.table)}
              onChange={(v) => onChange({ table: v })}
              placeholder="Local.customers"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Operation">
              <select
                className={CONTROL}
                value={str(a.operation) || 'add'}
                onChange={(e) => onChange({ operation: e.target.value })}>
                {EDIT_TABLE_OPERATIONS.map((op) => (
                  <option key={op} value={op}>
                    {op}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Key field (optional)">
              <input
                className={CONTROL}
                value={str(a.key)}
                onChange={(e) => onChange({ key: e.target.value })}
                placeholder="id"
              />
            </Field>
          </div>
          <Field label="Item (field = value)">
            <KeyValueEditor
              value={a.item && typeof a.item === 'object' ? (a.item as Record<string, unknown>) : {}}
              onChange={(v) => onChange({ item: v })}
              keyPlaceholder="id"
              valuePlaceholder="100"
            />
          </Field>
          <Field label="Index (optional)">
            <input
              className={CONTROL}
              value={str(a.index)}
              onChange={(e) => onChange({ index: e.target.value })}
              placeholder="0"
            />
          </Field>
        </div>
      )
    case 'If':
      return (
        <Field label="Condition (PowerFx expression)">
          <input
            className={CONTROL}
            value={str(a.condition)}
            onChange={(e) => onChange({ condition: e.target.value })}
            placeholder="=turn.count > 0"
          />
          <p className="mt-1 text-[10px] text-muted-foreground">Edit the then / else branches in the tree below.</p>
        </Field>
      )
    case 'ConditionGroup':
      return (
        <p className="text-[11px] text-muted-foreground">
          Edit each branch condition and its actions in the branch lanes below.
        </p>
      )
    case 'Foreach':
      return (
        <div className="space-y-2">
          <Field label="Source (collection expression)">
            <input
              className={CONTROL}
              value={str(a.source)}
              onChange={(e) => onChange({ source: e.target.value })}
              placeholder="=items"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Item name (optional)">
              <input
                className={CONTROL}
                value={str(a.itemName)}
                onChange={(e) => onChange({ itemName: e.target.value })}
                placeholder="item"
              />
            </Field>
            <Field label="Index name (optional)">
              <input
                className={CONTROL}
                value={str(a.indexName)}
                onChange={(e) => onChange({ indexName: e.target.value })}
                placeholder="index"
              />
            </Field>
          </div>
          <p className="text-[10px] text-muted-foreground">Edit the loop body in the tree below.</p>
        </div>
      )
    case 'BreakLoop':
      return <p className="text-[11px] text-muted-foreground">Breaks the enclosing loop. No fields.</p>
    case 'ContinueLoop':
      return <p className="text-[11px] text-muted-foreground">Continues the enclosing loop. No fields.</p>
    case 'GotoAction':
      return (
        <Field label="Target action id">
          <input
            className={CONTROL}
            value={str(a.actionId)}
            onChange={(e) => onChange({ actionId: e.target.value })}
            placeholder="sendactivity_1"
          />
          <p className="mt-1 text-[10px] text-muted-foreground">
            Jumps to the action with this id (draws a back-edge).
          </p>
        </Field>
      )
    case 'SendActivity':
      return (
        <Field label="Message text">
          <textarea
            className={cn(CONTROL, 'h-16 resize-none text-xs')}
            value={str(a.activity?.text)}
            onChange={(e) => onChange({ activity: { text: e.target.value } })}
          />
        </Field>
      )
    case 'InvokeAzureAgent':
      return (
        <div className="space-y-2">
          <Field label="Agent (declarative Prompt agent)">
            <select
              className={CONTROL}
              value={str(a.agentName)}
              onChange={(e) => onChange({ agentName: e.target.value })}>
              <option value="">Select an agent...</option>
              {agentNames.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Input messages (optional expression)">
            <input
              className={CONTROL}
              value={str(a.input?.messages)}
              onChange={(e) => onChange({ input: { ...(a.input ?? {}), messages: e.target.value } })}
              placeholder="=System.LastMessage"
            />
          </Field>
          <Field label="Input arguments (name = value)">
            <KeyValueEditor
              value={a.input?.arguments ?? {}}
              onChange={(v) => onChange({ input: { ...(a.input ?? {}), arguments: v } })}
              keyPlaceholder="topic"
              valuePlaceholder="=Workflow.Inputs.topic"
            />
          </Field>
          <Field label="External loop condition (optional)">
            <input
              className={CONTROL}
              value={str(a.input?.externalLoop?.when)}
              onChange={(e) => onChange({ input: { ...(a.input ?? {}), externalLoop: { when: e.target.value } } })}
              placeholder="=Local.needsMore"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Output response variable (optional)">
              <VariableInput
                value={str(a.output?.responseObject)}
                onChange={(v) => onChange({ output: { ...(a.output ?? {}), responseObject: v } })}
                placeholder="Local.reply"
              />
            </Field>
            <Field label="Output messages variable (optional)">
              <VariableInput
                value={str(a.output?.messages)}
                onChange={(v) => onChange({ output: { ...(a.output ?? {}), messages: v } })}
                placeholder="Local.replyMessages"
              />
            </Field>
          </div>
          <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={a.output?.autoSend !== false}
              onChange={(e) => onChange({ output: { ...(a.output ?? {}), autoSend: e.target.checked } })}
            />
            Send the agent response as workflow output
          </label>
        </div>
      )
    case 'InvokeFunctionTool':
      return (
        <div className="space-y-2">
          <JailedNote kind={a.kind} />
          <Field label="Function name">
            <input
              className={CONTROL}
              value={str(a.functionName)}
              onChange={(e) => onChange({ functionName: e.target.value })}
              placeholder="get_weather"
            />
          </Field>
          <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={Boolean(a.requireApproval)}
              onChange={(e) => onChange({ requireApproval: e.target.checked })}
            />
            Require approval
          </label>
          <Field label="Arguments (name = value)">
            <KeyValueEditor
              value={a.arguments ?? {}}
              onChange={(v) => onChange({ arguments: v })}
              keyPlaceholder="city"
              valuePlaceholder="=Local.city"
            />
          </Field>
          <Field label="Output result variable (optional)">
            <VariableInput
              value={str(a.output?.result)}
              onChange={(v) => onChange({ output: { ...(a.output ?? {}), result: v } })}
              placeholder="Local.result"
            />
          </Field>
        </div>
      )
    case 'InvokeMcpTool':
      return (
        <div className="space-y-2">
          <JailedNote kind={a.kind} />
          <div className="grid grid-cols-2 gap-2">
            <Field label="Server label">
              <input
                className={CONTROL}
                value={str(a.serverLabel)}
                onChange={(e) => onChange({ serverLabel: e.target.value })}
                placeholder="configured-server"
              />
            </Field>
            <Field label="Tool name">
              <input
                className={CONTROL}
                value={str(a.toolName)}
                onChange={(e) => onChange({ toolName: e.target.value })}
                placeholder="search"
              />
            </Field>
          </div>
          <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={Boolean(a.requireApproval)}
              onChange={(e) => onChange({ requireApproval: e.target.checked })}
            />
            Require approval
          </label>
          <Field label="Arguments (name = value)">
            <KeyValueEditor
              value={a.arguments ?? {}}
              onChange={(v) => onChange({ arguments: v })}
              keyPlaceholder="query"
              valuePlaceholder="=Local.query"
            />
          </Field>
          <Field label="Output result variable (optional)">
            <VariableInput
              value={str(a.output?.result)}
              onChange={(v) => onChange({ output: { ...(a.output ?? {}), result: v } })}
              placeholder="Local.result"
            />
          </Field>
        </div>
      )
    case 'HttpRequestAction':
      return (
        <div className="space-y-2">
          <JailedNote kind={a.kind} />
          <div className="grid grid-cols-[80px_1fr] gap-2">
            <Field label="Method">
              <select
                className={CONTROL}
                value={str(a.method) || 'GET'}
                onChange={(e) => onChange({ method: e.target.value })}>
                {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="URL">
              <input
                className={CONTROL}
                value={str(a.url)}
                onChange={(e) => onChange({ url: e.target.value })}
                placeholder="https://api.example.com/x"
              />
            </Field>
          </div>
          <Field label="Headers (name = value)">
            <KeyValueEditor
              value={a.headers ?? {}}
              onChange={(v) => onChange({ headers: v })}
              keyPlaceholder="Accept"
              valuePlaceholder="application/json"
            />
          </Field>
          <Field label="Query parameters (name = value)">
            <KeyValueEditor
              value={a.queryParameters ?? {}}
              onChange={(v) => onChange({ queryParameters: v })}
              keyPlaceholder="q"
              valuePlaceholder="=Local.q"
            />
          </Field>
          <Field label="Body (optional)">
            <textarea
              className={cn(CONTROL, 'h-14 resize-none text-xs')}
              value={str(a.body)}
              onChange={(e) => onChange({ body: e.target.value })}
              placeholder='{"key": "value"}'
            />
          </Field>
          <Field label="Request timeout ms (optional)">
            <input
              className={CONTROL}
              value={a.requestTimeoutInMilliseconds == null ? '' : String(a.requestTimeoutInMilliseconds)}
              onChange={(e) =>
                onChange({
                  requestTimeoutInMilliseconds: e.target.value ? Number(e.target.value) : undefined,
                })
              }
              placeholder="10000"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Response variable (optional)">
              <VariableInput
                value={str(a.response)}
                onChange={(v) => onChange({ response: v })}
                placeholder="Local.response"
              />
            </Field>
            <Field label="Response headers var (optional)">
              <VariableInput
                value={str(a.responseHeaders)}
                onChange={(v) => onChange({ responseHeaders: v })}
                placeholder="Local.responseHeaders"
              />
            </Field>
          </div>
        </div>
      )
    case 'Question':
      return (
        <div className="space-y-2">
          <Field label="Question text">
            <textarea
              className={cn(CONTROL, 'h-14 resize-none text-xs')}
              value={str(a.question?.text)}
              onChange={(e) => onChange({ question: { text: e.target.value } })}
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Answer variable">
              <VariableInput
                value={str(a.variable)}
                onChange={(v) => onChange({ variable: v })}
                placeholder="Local.answer"
              />
            </Field>
            <Field label="Default (optional)">
              <input
                className={CONTROL}
                value={str(a.default)}
                onChange={(e) => onChange({ default: e.target.value })}
              />
            </Field>
          </div>
          <Field label="Choices (value / label, optional)">
            <ChoiceEditor value={a.choices ?? []} onChange={(v) => onChange({ choices: v })} />
          </Field>
          <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={a.allowFreeText !== false}
              onChange={(e) => onChange({ allowFreeText: e.target.checked })}
            />
            Allow free text
          </label>
        </div>
      )
    case 'RequestExternalInput':
      return (
        <div className="space-y-2">
          <Field label="Prompt text">
            <textarea
              className={cn(CONTROL, 'h-14 resize-none text-xs')}
              value={str(a.prompt?.text)}
              onChange={(e) => onChange({ prompt: { text: e.target.value } })}
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Result variable">
              <VariableInput
                value={str(a.variable)}
                onChange={(v) => onChange({ variable: v })}
                placeholder="Local.input"
              />
            </Field>
            <Field label="Default (optional)">
              <input
                className={CONTROL}
                value={str(a.default)}
                onChange={(e) => onChange({ default: e.target.value })}
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Request type (optional)">
              <input
                className={CONTROL}
                value={str(a.requestType)}
                onChange={(e) => onChange({ requestType: e.target.value })}
                placeholder="approval"
              />
            </Field>
            <Field label="Timeout seconds (optional)">
              <input
                className={CONTROL}
                value={a.timeout == null ? '' : String(a.timeout)}
                onChange={(e) => onChange({ timeout: e.target.value ? Number(e.target.value) : undefined })}
                placeholder="3600"
              />
            </Field>
          </div>
          <Field label="Required fields (comma separated, optional)">
            <CsvInput
              value={a.requiredFields ?? []}
              onChange={(requiredFields) => onChange({ requiredFields })}
              placeholder="approved, approver"
            />
          </Field>
          <Field label="Metadata (name = value, optional)">
            <KeyValueEditor
              value={a.metadata ?? {}}
              onChange={(v) => onChange({ metadata: v })}
              keyPlaceholder="department"
              valuePlaceholder="finance"
            />
          </Field>
          <p className="rounded bg-muted/40 px-2 py-1 text-[10px] text-muted-foreground">
            Never request secrets or credentials through a human-in-the-loop action (UDR-0104 D4).
          </p>
        </div>
      )
    case 'EndWorkflow':
      return <p className="text-[11px] text-muted-foreground">Ends the workflow. No fields.</p>
    case 'EndConversation':
      return <p className="text-[11px] text-muted-foreground">Ends the conversation. No fields.</p>
    case 'CreateConversation':
      return (
        <Field label="Conversation id (variable)">
          <VariableInput
            value={str(a.conversationId)}
            onChange={(v) => onChange({ conversationId: v })}
            placeholder="Local.conversationId"
          />
        </Field>
      )
    default:
      return <p className="text-[11px] text-muted-foreground">This action has no configurable fields.</p>
  }
}

// ---- key/value map editor (variables, arguments, headers, query params) ----
interface KVRow {
  uid: string
  key: string
  value: string
}
let kvSeq = 0
const nextUid = () => `kv${kvSeq++}`

function KeyValueEditor({
  value,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
}: {
  value: Record<string, unknown>
  onChange: (v: Record<string, unknown>) => void
  keyPlaceholder?: string
  valuePlaceholder?: string
}) {
  const [rows, setRows] = useState<KVRow[]>(() =>
    Object.entries(value).map(([k, v]) => ({ uid: nextUid(), key: k, value: str(v) })),
  )
  const commit = (next: KVRow[]) => {
    setRows(next)
    const obj: Record<string, unknown> = {}
    for (const r of next) if (r.key.trim()) obj[r.key] = r.value
    onChange(obj)
  }
  return (
    <div className="space-y-1">
      {rows.map((r, i) => (
        <div key={r.uid} className="flex items-center gap-1">
          <input
            className={cn(CONTROL, 'text-xs')}
            value={r.key}
            placeholder={keyPlaceholder}
            onChange={(e) => commit(rows.map((rr, ii) => (ii === i ? { ...rr, key: e.target.value } : rr)))}
          />
          <input
            className={cn(CONTROL, 'text-xs')}
            value={r.value}
            placeholder={valuePlaceholder}
            onChange={(e) => commit(rows.map((rr, ii) => (ii === i ? { ...rr, value: e.target.value } : rr)))}
          />
          <button
            type="button"
            aria-label="Remove entry"
            className="rounded p-1 text-muted-foreground hover:text-destructive"
            onClick={() => commit(rows.filter((_, ii) => ii !== i))}>
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-1.5 text-[10px]"
        onClick={() => commit([...rows, { uid: nextUid(), key: '', value: '' }])}>
        <Plus className="mr-0.5 h-3 w-3" /> Entry
      </Button>
    </div>
  )
}

// ---- SetMultipleVariables assignment editor (CTR-0184 v3) -------------------
// The executor reads an `assignments` LIST of {variable, value}; the previous map
// editor emitted a `variables` map the executor never read (PRP-0122 FACT 2).
interface AssignRow {
  uid: string
  variable: string
  value: string
}

function AssignmentEditor({
  value,
  onChange,
}: {
  value: Array<{ variable?: string; value?: unknown }>
  onChange: (v: Array<{ variable?: string; value?: unknown }>) => void
}) {
  const [rows, setRows] = useState<AssignRow[]>(() =>
    value.map((a) => ({ uid: nextUid(), variable: str(a.variable), value: str(a.value) })),
  )
  const commit = (next: AssignRow[]) => {
    setRows(next)
    onChange(next.filter((r) => r.variable.trim()).map((r) => ({ variable: r.variable, value: r.value })))
  }
  return (
    <div className="space-y-1">
      {rows.map((r, i) => (
        <div key={r.uid} className="flex items-center gap-1">
          <VariableInput
            value={r.variable}
            onChange={(v) => commit(rows.map((rr, ii) => (ii === i ? { ...rr, variable: v } : rr)))}
          />
          <input
            className={cn(CONTROL, 'text-xs')}
            value={r.value}
            placeholder="=value"
            onChange={(e) => commit(rows.map((rr, ii) => (ii === i ? { ...rr, value: e.target.value } : rr)))}
          />
          <button
            type="button"
            aria-label="Remove assignment"
            className="rounded p-1 text-muted-foreground hover:text-destructive"
            onClick={() => commit(rows.filter((_, ii) => ii !== i))}>
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-1.5 text-[10px]"
        onClick={() => commit([...rows, { uid: nextUid(), variable: '', value: '' }])}>
        <Plus className="mr-0.5 h-3 w-3" /> Assignment
      </Button>
    </div>
  )
}

// ---- Question choice editor (CTR-0184 v3) ----------------------------------
interface ChoiceRow {
  uid: string
  value: string
  label: string
}

function ChoiceEditor({
  value,
  onChange,
}: {
  value: Array<{ value?: string; label?: string }>
  onChange: (v: Array<{ value?: string; label?: string }>) => void
}) {
  const [rows, setRows] = useState<ChoiceRow[]>(() =>
    value.map((c) => ({ uid: nextUid(), value: str(c.value), label: str(c.label) })),
  )
  const commit = (next: ChoiceRow[]) => {
    setRows(next)
    onChange(next.filter((r) => r.value.trim()).map((r) => ({ value: r.value, label: r.label || r.value })))
  }
  return (
    <div className="space-y-1">
      {rows.map((r, i) => (
        <div key={r.uid} className="flex items-center gap-1">
          <input
            className={cn(CONTROL, 'text-xs')}
            value={r.value}
            placeholder="high"
            onChange={(e) => commit(rows.map((rr, ii) => (ii === i ? { ...rr, value: e.target.value } : rr)))}
          />
          <input
            className={cn(CONTROL, 'text-xs')}
            value={r.label}
            placeholder="High"
            onChange={(e) => commit(rows.map((rr, ii) => (ii === i ? { ...rr, label: e.target.value } : rr)))}
          />
          <button
            type="button"
            aria-label="Remove choice"
            className="rounded p-1 text-muted-foreground hover:text-destructive"
            onClick={() => commit(rows.filter((_, ii) => ii !== i))}>
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-1.5 text-[10px]"
        onClick={() => commit([...rows, { uid: nextUid(), value: '', label: '' }])}>
        <Plus className="mr-0.5 h-3 w-3" /> Choice
      </Button>
    </div>
  )
}

// ---- inputs / outputs map editor -------------------------------------------
interface IORow {
  uid: string
  name: string
  type: string
  description: string
}

function IOEditor({
  title,
  value,
  onChange,
  withDescription,
  seedKey,
}: {
  title: string
  value: Record<string, { type?: string; description?: string }>
  onChange: (v: Record<string, { type?: string; description?: string }>) => void
  withDescription?: boolean
  seedKey: string
}) {
  const toRows = (v: Record<string, { type?: string; description?: string }>): IORow[] =>
    Object.entries(v).map(([name, def]) => ({
      uid: nextUid(),
      name,
      type: str(def?.type),
      description: str(def?.description),
    }))
  const [rows, setRows] = useState<IORow[]>(() => toRows(value))
  const lastSeed = useRef(seedKey)
  if (lastSeed.current !== seedKey) {
    lastSeed.current = seedKey
    setRows(toRows(value))
  }
  const commit = (next: IORow[]) => {
    setRows(next)
    const obj: Record<string, { type?: string; description?: string }> = {}
    for (const r of next) {
      if (!r.name.trim()) continue
      const def: { type?: string; description?: string } = {}
      if (r.type.trim()) def.type = r.type
      if (withDescription && r.description.trim()) def.description = r.description
      obj[r.name] = def
    }
    onChange(obj)
  }
  const set = (i: number, p: Partial<IORow>) => commit(rows.map((r, ii) => (ii === i ? { ...r, ...p } : r)))
  return (
    <div className="space-y-1">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70">{title}</div>
      {rows.map((r, i) => (
        <div key={r.uid} className="space-y-1 rounded border border-border/60 p-1">
          <div className="flex items-center gap-1">
            <input
              className={cn(CONTROL, 'text-xs')}
              value={r.name}
              placeholder="name"
              onChange={(e) => set(i, { name: e.target.value })}
            />
            <input
              className={cn(CONTROL, 'w-24 text-xs')}
              value={r.type}
              placeholder="type"
              onChange={(e) => set(i, { type: e.target.value })}
            />
            <button
              type="button"
              aria-label="Remove entry"
              className="rounded p-1 text-muted-foreground hover:text-destructive"
              onClick={() => commit(rows.filter((_, ii) => ii !== i))}>
              <X className="h-3 w-3" />
            </button>
          </div>
          {withDescription && (
            <input
              className={cn(CONTROL, 'text-xs')}
              value={r.description}
              placeholder="description"
              onChange={(e) => set(i, { description: e.target.value })}
            />
          )}
        </div>
      ))}
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-1.5 text-[10px]"
        onClick={() => commit([...rows, { uid: nextUid(), name: '', type: '', description: '' }])}>
        <Plus className="mr-0.5 h-3 w-3" /> {title === 'Inputs' ? 'Input' : 'Output'}
      </Button>
    </div>
  )
}
