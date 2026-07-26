import Editor from '@monaco-editor/react'
import {
  Background,
  type Edge,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import ELK from 'elkjs/lib/elk.bundled.js'
import {
  ArrowDown,
  ArrowUp,
  Braces,
  CornerDownRight,
  Eraser,
  GitBranch,
  Globe,
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
import { type ComponentType, memo, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
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

function newAction(kind: string, index: number): WorkflowAction {
  const id = `${kind.toLowerCase()}_${index}`
  switch (kind) {
    // Variable
    case 'SetVariable':
      return { kind, id, variable: '', value: '' }
    case 'SetMultipleVariables':
      return { kind, id, variables: {} }
    case 'SetTextVariable':
      return { kind, id, variable: '', value: '' }
    case 'ResetVariable':
      return { kind, id, variable: '' }
    case 'ClearAllVariables':
      return { kind, id }
    case 'ParseValue':
      return { kind, id, source: '', variable: '' }
    case 'EditTableV2':
      return { kind, id, table: '', operation: 'add', row: { key: '', value: '' } }
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
      return { kind, id, question: { text: '' }, variable: '' }
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

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

function actionSummary(a: WorkflowAction): string {
  switch (a.kind) {
    case 'SetVariable':
    case 'SetTextVariable':
      return a.variable ? `${str(a.variable)} = ${str(a.value).slice(0, 24)}` : 'variable'
    case 'SetMultipleVariables':
      return `${Object.keys(a.variables ?? {}).length} variable(s)`
    case 'ResetVariable':
      return a.variable ? str(a.variable) : 'variable'
    case 'ClearAllVariables':
      return 'all variables'
    case 'ParseValue':
      return a.variable ? `-> ${str(a.variable)}` : str(a.source) || 'parse'
    case 'EditTableV2':
      return `${str(a.operation) || 'edit'} ${str(a.table)}`.trim()
    case 'If':
      return a.condition ? str(a.condition).slice(0, 40) : 'condition'
    case 'ConditionGroup':
      return `${(a.conditions ?? []).length} branch(es)`
    case 'Foreach':
      return a.source ? str(a.source) : 'source'
    case 'BreakLoop':
      return 'break'
    case 'ContinueLoop':
      return 'continue'
    case 'GotoAction':
      return a.actionId ? `-> ${str(a.actionId)}` : 'target'
    case 'SendActivity':
      return a.activity?.text ? str(a.activity.text).slice(0, 40) : 'message'
    case 'InvokeAzureAgent':
      return a.agentName ? str(a.agentName) : str(a.agent?.name) || 'no agent'
    case 'InvokeFunctionTool':
      return a.functionName ? str(a.functionName) : 'function'
    case 'InvokeMcpTool':
      return a.serverLabel || a.toolName ? `${str(a.serverLabel)}/${str(a.toolName)}` : 'mcp tool'
    case 'HttpRequestAction':
      return `${str(a.method) || 'GET'} ${str(a.url).slice(0, 32)}`.trim()
    case 'Question':
      return a.question?.text ? str(a.question.text).slice(0, 40) : 'question'
    case 'RequestExternalInput':
      return a.prompt?.text ? str(a.prompt.text).slice(0, 40) : 'prompt'
    case 'EndWorkflow':
      return 'end workflow'
    case 'EndConversation':
      return 'end conversation'
    case 'CreateConversation':
      return a.conversationId ? str(a.conversationId) : 'conversation'
    default:
      return a.id ? str(a.id) : ''
  }
}

// ---- lane model: shared by the LEFT tree, the canvas and the edge builder ----
interface Lane {
  key: string
  label: string
  actions: WorkflowAction[]
  /** true when actions inside this lane run inside a loop (Break/Continue allowed). */
  loop: boolean
  /** immutable patch producing the parent action's partial update for a new lane array. */
  patch: (next: WorkflowAction[]) => Partial<WorkflowAction>
}

/** The editable child-action lanes of a control-flow action (empty for leaf kinds). */
function actionLanes(a: WorkflowAction, id: string): Lane[] {
  switch (a.kind) {
    case 'If':
      return [
        // biome-ignore lint/suspicious/noThenProperty: MAF If action uses a `then` branch key, not a thenable
        { key: `${id}/then`, label: 'then', actions: a.then ?? [], loop: false, patch: (n) => ({ then: n }) },
        { key: `${id}/else`, label: 'else', actions: a.else ?? [], loop: false, patch: (n) => ({ else: n }) },
      ]
    case 'Foreach':
      return [
        { key: `${id}/body`, label: 'body', actions: a.actions ?? [], loop: true, patch: (n) => ({ actions: n }) },
      ]
    case 'ConditionGroup': {
      const conditions = a.conditions ?? []
      const lanes: Lane[] = conditions.map((c, j) => ({
        key: `${id}/cond${j}`,
        label: c.condition ? `when: ${str(c.condition).slice(0, 20)}` : `branch ${j + 1}`,
        actions: c.actions ?? [],
        loop: false,
        patch: (n) => ({ conditions: conditions.map((cc, jj) => (jj === j ? { ...cc, actions: n } : cc)) }),
      }))
      lanes.push({
        key: `${id}/elseActions`,
        label: 'else',
        actions: a.elseActions ?? [],
        loop: false,
        patch: (n) => ({ elseActions: n }),
      })
      return lanes
    }
    default:
      return []
  }
}

// ===========================================================================
// React Flow nodes
// ===========================================================================
const NODE_W = 190
const NODE_H = 52
const LANE_MIN_W = 170
const LANE_MIN_H = 60

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

// ---- elk hierarchical layout ----------------------------------------------
const elk = new ELK()
type ElkGraphNode = {
  id: string
  width?: number
  height?: number
  layoutOptions?: Record<string, string>
  children?: ElkGraphNode[]
  edges?: Array<{ id: string; sources: string[]; targets: string[] }>
  x?: number
  y?: number
}
interface NodeMeta {
  type: 'step' | 'container' | 'lane'
  label: string
  sub?: string
}

const ROOT_OPTS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.layered.spacing.nodeNodeBetweenLayers': '30',
  'elk.spacing.nodeNode': '24',
}
const CONTAINER_OPTS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.padding': '[top=30.0,left=12.0,bottom=12.0,right=12.0]',
  'elk.spacing.nodeNode': '18',
}
const LANE_OPTS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.padding': '[top=22.0,left=10.0,bottom=10.0,right=10.0]',
  'elk.layered.spacing.nodeNodeBetweenLayers': '22',
  'elk.spacing.nodeNode': '16',
}

/** Build the elk graph + a meta map (elkjs drops unknown props, so meta is kept aside). */
function buildElkLevel(actions: WorkflowAction[], pathKey: string, meta: Map<string, NodeMeta>) {
  const children: ElkGraphNode[] = []
  const edges: Array<{ id: string; sources: string[]; targets: string[] }> = []
  actions.forEach((a, i) => {
    const id = `${pathKey}/${i}`
    const lanes = actionLanes(a, id)
    if (lanes.length === 0) {
      children.push({ id, width: NODE_W, height: NODE_H })
      meta.set(id, { type: 'step', label: a.kind, sub: actionSummary(a) })
    } else {
      const laneNodes: ElkGraphNode[] = lanes.map((l) => {
        const sub = buildElkLevel(l.actions, l.key, meta)
        meta.set(l.key, { type: 'lane', label: l.label })
        return sub.children.length
          ? { id: l.key, layoutOptions: LANE_OPTS, children: sub.children, edges: sub.edges }
          : { id: l.key, width: LANE_MIN_W, height: LANE_MIN_H }
      })
      children.push({ id, layoutOptions: CONTAINER_OPTS, children: laneNodes })
      meta.set(id, { type: 'container', label: a.kind, sub: actionSummary(a) })
    }
    if (i < actions.length - 1) {
      edges.push({ id: `e/${id}`, sources: [id], targets: [`${pathKey}/${i + 1}`] })
    }
  })
  return { children, edges }
}

/** Flatten an elk-laid graph into React Flow nodes (parents before children). */
function elkToNodes(elkNodes: ElkGraphNode[], parentId: string | null, meta: Map<string, NodeMeta>, out: Node[]) {
  for (const n of elkNodes) {
    const m = meta.get(n.id)
    if (!m) continue
    out.push({
      id: n.id,
      type: m.type,
      position: { x: n.x ?? 0, y: n.y ?? 0 },
      data: { label: m.label, sub: m.sub, selected: false, onSelect: () => {} },
      draggable: false,
      selectable: m.type !== 'lane',
      style: { width: n.width ?? NODE_W, height: n.height ?? NODE_H },
      ...(parentId ? { parentId, extent: 'parent' as const } : {}),
    })
    if (n.children?.length) elkToNodes(n.children, n.id, meta, out)
  }
}

/** Sequential (top-to-bottom) edges + GotoAction back-edges, matching the lane keys. */
function buildEdges(actions: WorkflowAction[]): Edge[] {
  const edges: Edge[] = []
  const idMap = new Map<string, string>()
  const gotos: Array<{ from: string; to: string }> = []
  const walk = (list: WorkflowAction[], pathKey: string) => {
    list.forEach((a, i) => {
      const id = `${pathKey}/${i}`
      if (a.id) idMap.set(str(a.id), id)
      if (a.kind === 'GotoAction' && a.actionId) gotos.push({ from: id, to: str(a.actionId) })
      if (i < list.length - 1) {
        edges.push({
          id: `seq/${id}`,
          source: id,
          target: `${pathKey}/${i + 1}`,
          markerEnd: { type: MarkerType.ArrowClosed },
        })
      }
      for (const l of actionLanes(a, id)) walk(l.actions, l.key)
    })
  }
  walk(actions, 'a')
  for (const g of gotos) {
    const target = idMap.get(g.to)
    if (target) {
      edges.push({
        id: `goto/${g.from}`,
        source: g.from,
        target,
        label: 'goto',
        animated: true,
        style: { stroke: 'hsl(var(--primary))', strokeDasharray: '4 3' },
        markerEnd: { type: MarkerType.ArrowClosed, color: 'hsl(var(--primary))' },
      })
    }
  }
  return edges
}

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

  // ---- initial load: Prompt agent names + source for edit ----
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setDirty(false)
    setRawMode(false)
    setSelected(null)
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
    const meta = new Map<string, NodeMeta>()
    const level = buildElkLevel(doc.actions, 'a', meta)
    const graph: ElkGraphNode = { id: 'root', layoutOptions: ROOT_OPTS, children: level.children, edges: level.edges }
    elk
      .layout(graph as never)
      .then((res) => {
        if (cancelled) return
        const out: Node[] = []
        elkToNodes((res.children ?? []) as ElkGraphNode[], null, meta, out)
        setLayoutNodes(out)
      })
      .catch(() => {
        if (!cancelled) setLayoutNodes([])
      })
    return () => {
      cancelled = true
    }
  }, [doc.actions, open])

  const nodes = useMemo(
    () =>
      layoutNodes.map((n) =>
        n.type === 'lane'
          ? n
          : { ...n, data: { ...n.data, selected: selected === n.id, onSelect: () => setSelected(n.id) } },
      ),
    [layoutNodes, selected],
  )
  const edges = useMemo(() => buildEdges(doc.actions), [doc.actions])

  // ---- save / close ----
  const handleSave = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      const body = rawMode ? { yaml: rawYaml } : { document: doc, name: doc.name }
      const result = await api.save(body, editId)
      setDirty(false)
      onSaved(result.id ?? editId ?? undefined)
      onOpenChange(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save workflow')
    } finally {
      setSaving(false)
    }
  }, [rawMode, rawYaml, doc, editId, api, onSaved, onOpenChange])

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
    <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(true) : requestClose())}>
      <DialogContent className="flex h-screen w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-background p-0 sm:rounded-none [&>button]:hidden">
        <DialogTitle className="sr-only">
          {editId ? 'Edit declarative workflow' : 'Create declarative workflow'}
        </DialogTitle>
        <DialogDescription className="sr-only">
          Compose a declarative workflow graph: sequential actions, control flow and agent invocations.
        </DialogDescription>
        <div className="flex shrink-0 items-center justify-between border-b px-5 py-3">
          <div className="flex items-center gap-2">
            <WorkflowIcon className="h-5 w-5 text-primary" />
            <div>
              <div className="text-sm font-semibold">{editId ? 'Edit workflow' : 'Create workflow'}</div>
              <div className="text-[11px] text-muted-foreground">
                A workflow orchestrates declarative Prompt agents. Credentials and sampling are resolved by ChatWalaʻau.
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
              {editId ? 'Save' : 'Create'}
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
              <div className={cn('h-full space-y-3 overflow-y-auto p-4', rawMode && 'pointer-events-none opacity-50')}>
                <Field label="Name (identifier)">
                  <input
                    className={CONTROL}
                    value={doc.name}
                    onChange={(e) => patch({ name: e.target.value })}
                    placeholder="TriageFlow"
                  />
                </Field>
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
                    nodesConnectable={false}
                    nodesDraggable={false}
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

        {error && <div className="shrink-0 border-t bg-destructive/10 px-4 py-2 text-xs text-destructive">{error}</div>}

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
    onSelect(`${pathKey}/${actions.length}`)
    update([...actions, newAction(kind, actions.length + 1)])
  }

  return (
    <div className="space-y-1">
      <div className="flex justify-end">
        <AddActionMenu onAdd={addKind} allowLoop={allowLoop} />
      </div>
      {actions.length === 0 && <p className="text-[11px] text-muted-foreground">No actions. Add a step.</p>}
      {actions.map((a, i) => {
        const key = `${pathKey}/${i}`
        const lanes = actionLanes(a, key)
        const Icon = ICON_BY_KIND[a.kind] ?? WorkflowIcon
        return (
          <div
            key={a.id ?? key}
            className={cn('rounded-md border', selected === key ? 'border-primary' : 'border-border')}>
            <div className="flex items-center gap-2 px-2 py-1.5 text-xs">
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
                          conditions: [
                            ...conds,
                            { condition: '', id: `${a.id ?? key}_c${conds.length + 1}`, actions: [] },
                          ],
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
      })}
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
    case 'SetTextVariable':
      return (
        <div className="space-y-2">
          <Field label="Variable">
            <input
              className={CONTROL}
              value={str(a.variable)}
              onChange={(e) => onChange({ variable: e.target.value })}
              placeholder="topic.name"
            />
          </Field>
          <Field label="Value">
            <input className={CONTROL} value={str(a.value)} onChange={(e) => onChange({ value: e.target.value })} />
          </Field>
        </div>
      )
    case 'SetMultipleVariables':
      return (
        <Field label="Variables (path = value)">
          <KeyValueEditor
            value={a.variables ?? {}}
            onChange={(v) => onChange({ variables: v })}
            keyPlaceholder="topic.x"
            valuePlaceholder="=value"
          />
        </Field>
      )
    case 'ResetVariable':
      return (
        <Field label="Variable">
          <input
            className={CONTROL}
            value={str(a.variable)}
            onChange={(e) => onChange({ variable: e.target.value })}
            placeholder="topic.name"
          />
        </Field>
      )
    case 'ClearAllVariables':
      return <p className="text-[11px] text-muted-foreground">Clears all workflow variables. No fields.</p>
    case 'ParseValue':
      return (
        <div className="space-y-2">
          <Field label="Source (expression)">
            <input
              className={CONTROL}
              value={str(a.source)}
              onChange={(e) => onChange({ source: e.target.value })}
              placeholder="=lastMessage.text"
            />
          </Field>
          <Field label="Target variable">
            <input
              className={CONTROL}
              value={str(a.variable)}
              onChange={(e) => onChange({ variable: e.target.value })}
              placeholder="topic.parsed"
            />
          </Field>
        </div>
      )
    case 'EditTableV2':
      return (
        <div className="space-y-2">
          <Field label="Table (variable)">
            <input
              className={CONTROL}
              value={str(a.table)}
              onChange={(e) => onChange({ table: e.target.value })}
              placeholder="topic.rows"
            />
          </Field>
          <Field label="Operation">
            <select
              className={CONTROL}
              value={str(a.operation) || 'add'}
              onChange={(e) => onChange({ operation: e.target.value })}>
              <option value="add">add</option>
              <option value="update">update</option>
              <option value="remove">remove</option>
              <option value="clear">clear</option>
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Row key">
              <input
                className={CONTROL}
                value={str(a.row?.key)}
                onChange={(e) => onChange({ row: { ...(a.row ?? {}), key: e.target.value } })}
              />
            </Field>
            <Field label="Row value">
              <input
                className={CONTROL}
                value={str(a.row?.value)}
                onChange={(e) => onChange({ row: { ...(a.row ?? {}), value: e.target.value } })}
              />
            </Field>
          </div>
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
              placeholder="=conversation.messages"
            />
          </Field>
          <Field label="Output response variable (optional)">
            <input
              className={CONTROL}
              value={str(a.output?.responseObject)}
              onChange={(e) => onChange({ output: { ...(a.output ?? {}), responseObject: e.target.value } })}
              placeholder="topic.reply"
            />
          </Field>
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
              valuePlaceholder="=topic.city"
            />
          </Field>
          <Field label="Output result variable (optional)">
            <input
              className={CONTROL}
              value={str(a.output?.result)}
              onChange={(e) => onChange({ output: { ...(a.output ?? {}), result: e.target.value } })}
              placeholder="topic.result"
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
              valuePlaceholder="=topic.query"
            />
          </Field>
          <Field label="Output result variable (optional)">
            <input
              className={CONTROL}
              value={str(a.output?.result)}
              onChange={(e) => onChange({ output: { ...(a.output ?? {}), result: e.target.value } })}
              placeholder="topic.result"
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
              valuePlaceholder="=topic.q"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Response variable (optional)">
              <input
                className={CONTROL}
                value={str(a.response)}
                onChange={(e) => onChange({ response: e.target.value })}
                placeholder="topic.response"
              />
            </Field>
            <Field label="Response headers var (optional)">
              <input
                className={CONTROL}
                value={str(a.responseHeaders)}
                onChange={(e) => onChange({ responseHeaders: e.target.value })}
                placeholder="topic.responseHeaders"
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
              <input
                className={CONTROL}
                value={str(a.variable)}
                onChange={(e) => onChange({ variable: e.target.value })}
                placeholder="topic.answer"
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
              <input
                className={CONTROL}
                value={str(a.variable)}
                onChange={(e) => onChange({ variable: e.target.value })}
                placeholder="topic.input"
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
        </div>
      )
    case 'EndWorkflow':
      return <p className="text-[11px] text-muted-foreground">Ends the workflow. No fields.</p>
    case 'EndConversation':
      return <p className="text-[11px] text-muted-foreground">Ends the conversation. No fields.</p>
    case 'CreateConversation':
      return (
        <Field label="Conversation id (variable)">
          <input
            className={CONTROL}
            value={str(a.conversationId)}
            onChange={(e) => onChange({ conversationId: e.target.value })}
            placeholder="topic.conversationId"
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
