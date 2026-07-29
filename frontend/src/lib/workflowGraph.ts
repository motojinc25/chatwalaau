import { type Edge, MarkerType, type Node } from '@xyflow/react'
import ELK from 'elkjs/lib/elk.bundled.js'
import type { WorkflowAction } from '@/hooks/useWorkflowAuthoring'

/**
 * Workflow Graph Model (CTR-0188, FEAT-0062, PRP-0123, UDR-0106 D11).
 *
 * The ONE derivation from a declarative workflow document to a laid-out React Flow
 * graph -- action tree walk, lane construction, elk hierarchical layout, edge building --
 * shared by the authoring editor (CTR-0184) and the run canvas (CTR-0187) so the two
 * surfaces can never disagree about a workflow's shape.
 *
 * Extracted verbatim from `DeclarativeWorkflowEditor.tsx` (v0.116.2); behavior-preserving
 * for the editor. This module produces DATA only: node/edge components, icons, selection,
 * and run status belong to the consuming contract.
 *
 * Node ids are POSITIONAL path keys (`a/0`, `a/1/then/0`) because lane keys, React Flow
 * `parentId`, and the editor's selection model are all derived from them. An action's own
 * `id` (UDR-0105 D7 guarantees it is explicit and unique) is indexed separately as
 * `byActionId`, so a run event carrying `node: <action id>` still addresses exactly one
 * graph node.
 */

const elk = new ELK()

export const NODE_W = 190
export const NODE_H = 52
export const LANE_MIN_W = 170
export const LANE_MIN_H = 60

export const ROOT_OPTS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.layered.spacing.nodeNodeBetweenLayers': '30',
  'elk.spacing.nodeNode': '24',
}
export const CONTAINER_OPTS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.padding': '[top=30.0,left=12.0,bottom=12.0,right=12.0]',
  'elk.spacing.nodeNode': '18',
}
export const LANE_OPTS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.padding': '[top=22.0,left=10.0,bottom=10.0,right=10.0]',
  'elk.layered.spacing.nodeNodeBetweenLayers': '22',
  'elk.spacing.nodeNode': '16',
}

export type ElkGraphNode = {
  id: string
  width?: number
  height?: number
  layoutOptions?: Record<string, string>
  children?: ElkGraphNode[]
  edges?: Array<{ id: string; sources: string[]; targets: string[] }>
  x?: number
  y?: number
}

export interface NodeMeta {
  type: 'step' | 'container' | 'lane'
  /** The action kind (the consumer maps it to an icon), or the lane label for lanes. */
  label: string
  sub?: string
  /** The author's action id, when the action carries one (absent for lanes). */
  actionId?: string
}

/** The editable child-action lanes of a control-flow action (empty for leaf kinds). */
export interface Lane {
  key: string
  label: string
  actions: WorkflowAction[]
  /** true when actions inside this lane run inside a loop (Break/Continue allowed). */
  loop: boolean
  /** immutable patch producing the parent action's partial update for a new lane array. */
  patch: (next: WorkflowAction[]) => Partial<WorkflowAction>
}

export function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

export function actionSummary(a: WorkflowAction): string {
  switch (a.kind) {
    case 'SetVariable':
      return a.variable ? `${str(a.variable)} = ${str(a.value).slice(0, 24)}` : 'variable'
    case 'SetTextVariable':
      return a.variable ? `${str(a.variable)} = ${str(a.text).slice(0, 24)}` : 'variable'
    case 'SetMultipleVariables':
      return `${(a.assignments ?? []).length} assignment(s)`
    case 'ResetVariable':
      return a.variable ? str(a.variable) : 'variable'
    case 'ClearAllVariables':
      return 'all variables'
    case 'ParseValue':
      return a.variable ? `-> ${str(a.variable)}` : str(a.value) || 'parse'
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

export function actionLanes(a: WorkflowAction, id: string): Lane[] {
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

/** Build the elk graph + a meta map (elkjs drops unknown props, so meta is kept aside). */
export function buildElkLevel(actions: WorkflowAction[], pathKey: string, meta: Map<string, NodeMeta>) {
  const children: ElkGraphNode[] = []
  const edges: Array<{ id: string; sources: string[]; targets: string[] }> = []
  actions.forEach((a, i) => {
    const id = `${pathKey}/${i}`
    const lanes = actionLanes(a, id)
    const actionId = a.id ? str(a.id) : undefined
    if (lanes.length === 0) {
      children.push({ id, width: NODE_W, height: NODE_H })
      meta.set(id, { type: 'step', label: a.kind, sub: actionSummary(a), actionId })
    } else {
      const laneNodes: ElkGraphNode[] = lanes.map((l) => {
        const sub = buildElkLevel(l.actions, l.key, meta)
        meta.set(l.key, { type: 'lane', label: l.label })
        return sub.children.length
          ? { id: l.key, layoutOptions: LANE_OPTS, children: sub.children, edges: sub.edges }
          : { id: l.key, width: LANE_MIN_W, height: LANE_MIN_H }
      })
      // Layout-only edges between consecutive lanes force the elk RIGHT layout to place
      // them side-by-side in order: If -> then (left) / else (right); ConditionGroup ->
      // branch1..n (left to right) / else (rightmost). These are NOT rendered as React
      // Flow edges (only buildEdges() produces those), so they only constrain layout.
      const laneOrderEdges = lanes.slice(0, -1).map((l, li) => ({
        id: `lane-order/${id}/${li}`,
        sources: [l.key],
        targets: [lanes[li + 1].key],
      }))
      children.push({ id, layoutOptions: CONTAINER_OPTS, children: laneNodes, edges: laneOrderEdges })
      meta.set(id, { type: 'container', label: a.kind, sub: actionSummary(a), actionId })
    }
    if (i < actions.length - 1) {
      edges.push({ id: `e/${id}`, sources: [id], targets: [`${pathKey}/${i + 1}`] })
    }
  })
  return { children, edges }
}

/** Flatten an elk-laid graph into React Flow nodes (parents before children). */
export function elkToNodes(
  elkNodes: ElkGraphNode[],
  parentId: string | null,
  meta: Map<string, NodeMeta>,
  out: Node[],
) {
  for (const n of elkNodes) {
    const m = meta.get(n.id)
    if (!m) continue
    out.push({
      id: n.id,
      type: m.type,
      position: { x: n.x ?? 0, y: n.y ?? 0 },
      data: { label: m.label, sub: m.sub, actionId: m.actionId, selected: false, onSelect: () => {} },
      draggable: m.type !== 'lane',
      selectable: m.type !== 'lane',
      style: { width: n.width ?? NODE_W, height: n.height ?? NODE_H },
      ...(parentId ? { parentId, extent: 'parent' as const } : {}),
    })
    if (n.children?.length) elkToNodes(n.children, n.id, meta, out)
  }
}

/** Sequential (top-to-bottom) edges + GotoAction back-edges, matching the lane keys. */
export function buildEdges(actions: WorkflowAction[]): Edge[] {
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
        // A GotoAction jump is an editable smoothstep (step) back-edge whose bend bows off
        // the node column and can be dragged off any node it overlaps; animated so it is
        // visually distinct from the sequential flow (v0.115.2).
        type: 'smoothstep',
        animated: true,
        style: { stroke: 'hsl(var(--primary))' },
        markerEnd: { type: MarkerType.ArrowClosed, color: 'hsl(var(--primary))' },
      })
    }
  }
  return edges
}

export interface WorkflowGraph {
  nodes: Node[]
  edges: Edge[]
  meta: Map<string, NodeMeta>
  /** action id -> React Flow node id, so a run event can address exactly one node. */
  byActionId: Map<string, string>
}

/**
 * Lay out a workflow document. Async because the elk layout is.
 *
 * Never throws: a layout failure yields an empty node list (the consumer keeps its
 * previous graph or renders the degraded view) rather than blanking the surface with an
 * exception (CTR-0188 failure semantics).
 */
export async function buildWorkflowGraph(actions: WorkflowAction[]): Promise<WorkflowGraph> {
  const meta = new Map<string, NodeMeta>()
  const level = buildElkLevel(actions, 'a', meta)
  const graph: ElkGraphNode = { id: 'root', layoutOptions: ROOT_OPTS, children: level.children, edges: level.edges }
  const edges = buildEdges(actions)
  const byActionId = new Map<string, string>()
  for (const [nodeId, m] of meta) if (m.actionId) byActionId.set(m.actionId, nodeId)
  try {
    const res = await elk.layout(graph as never)
    const nodes: Node[] = []
    elkToNodes((res.children ?? []) as ElkGraphNode[], null, meta, nodes)
    return { nodes, edges, meta, byActionId }
  } catch {
    return { nodes: [], edges, meta, byActionId }
  }
}
