import {
  Background,
  type Edge,
  Handle,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  CircleAlert,
  CircleCheck,
  CircleDashed,
  Loader2,
  MessageCircleQuestion,
  SkipForward,
  Variable,
  Workflow as WorkflowIcon,
  X,
} from 'lucide-react'
import { memo, type PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { Button } from '@/components/ui/button'
import type { WorkflowAction } from '@/hooks/useWorkflowAuthoring'
import { cn } from '@/lib/utils'
import { buildWorkflowGraph } from '@/lib/workflowGraph'
import {
  completedSteps,
  skippedSteps,
  type WorkflowNodeRunState,
  type WorkflowNodeStatus,
  type WorkflowRunModel,
} from '@/lib/workflowRunState'

/**
 * Workflow Run Canvas (CTR-0187, FEAT-0062, PRP-0123, UDR-0106 D10/D12/D13).
 *
 * A detached, movable, resizable, per-run overlay rendering a declarative workflow's
 * execution graph live. The graph is derived from the workflow DOCUMENT (CTR-0182) via
 * the shared graph model (CTR-0188) BEFORE the first node event arrives, so an untaken
 * branch is visible as `skipped` rather than absent. Run events change node STATE only.
 *
 * Read-only with respect to the document; it does not author (CTR-0184), define the
 * event grammar (CTR-0009), or run the workflow (CTR-0181). Canvas state is ephemeral:
 * not persisted, not restored on reload, not part of session export.
 */

const STATUS_STYLE: Record<WorkflowNodeStatus, { ring: string; text: string }> = {
  idle: { ring: 'border-border bg-background', text: 'text-muted-foreground' },
  running: { ring: 'border-primary bg-primary/10', text: 'text-foreground' },
  completed: { ring: 'border-primary/40 bg-primary/5', text: 'text-foreground' },
  skipped: { ring: 'border-dashed border-muted-foreground/40 bg-muted/30', text: 'text-muted-foreground' },
  failed: { ring: 'border-destructive/60 bg-destructive/10', text: 'text-destructive' },
  awaiting_input: { ring: 'border-amber-500/60 bg-amber-500/10', text: 'text-foreground' },
}

function StatusIcon({ status, className }: { status: WorkflowNodeStatus; className?: string }) {
  switch (status) {
    case 'running':
      return <Loader2 className={cn('animate-spin text-primary', className)} />
    case 'completed':
      return <CircleCheck className={cn('text-primary', className)} />
    case 'skipped':
      return <SkipForward className={cn('text-muted-foreground', className)} />
    case 'failed':
      return <CircleAlert className={cn('text-destructive', className)} />
    case 'awaiting_input':
      return <MessageCircleQuestion className={cn('text-amber-500', className)} />
    default:
      return <CircleDashed className={cn('text-muted-foreground/60', className)} />
  }
}

interface RunNodeData extends Record<string, unknown> {
  label: string
  sub?: string
  status: WorkflowNodeStatus
  selected: boolean
  onSelect: () => void
}

const RunStepNode = memo(({ data }: NodeProps<Node<RunNodeData>>) => {
  const style = STATUS_STYLE[data.status]
  return (
    <button
      type="button"
      onClick={data.onSelect}
      className={cn(
        'flex h-full w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-xs shadow-sm',
        style.ring,
        data.selected && 'ring-2 ring-ring',
      )}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <StatusIcon status={data.status} className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0">
        <span className={cn('block max-w-[140px] truncate font-medium', style.text)}>{data.label}</span>
        {data.sub && <span className="block max-w-[140px] truncate text-[10px] text-muted-foreground">{data.sub}</span>}
      </span>
    </button>
  )
})
RunStepNode.displayName = 'RunStepNode'

const RunContainerNode = memo(({ data }: NodeProps<Node<RunNodeData>>) => {
  const style = STATUS_STYLE[data.status]
  return (
    <div className={cn('h-full w-full rounded-md border', style.ring)}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <button
        type="button"
        onClick={data.onSelect}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-[11px] font-medium">
        <StatusIcon status={data.status} className="h-3 w-3 shrink-0" />
        <span className={cn('truncate', style.text)}>{data.label}</span>
      </button>
    </div>
  )
})
RunContainerNode.displayName = 'RunContainerNode'

const RunLaneNode = memo(({ data }: NodeProps<Node<{ label: string }>>) => (
  <div className="h-full w-full rounded border border-dashed border-muted-foreground/30 bg-muted/20">
    <span className="block px-1.5 py-0.5 text-[10px] text-muted-foreground">{data.label}</span>
  </div>
))
RunLaneNode.displayName = 'RunLaneNode'

const nodeTypes = { step: RunStepNode, container: RunContainerNode, lane: RunLaneNode }

export interface WorkflowRunCanvasProps {
  run: WorkflowRunModel
  /** The workflow document (CTR-0182 detail). Null = degraded, event-only rendering. */
  actions: WorkflowAction[] | null
  /**
   * Hide the window. The run's state is retained by the host so the in-message
   * indicator's "Diagram" control can bring it back (CTR-0185 / UDR-0106 D13); closing a
   * viewer never discards what it was viewing, and never affects the run itself.
   */
  onClose: () => void
  /** Submit the collected human-in-the-loop answers (UDR-0106 D5). */
  onSubmitInput: (answers: Record<string, { user_input: string; value: unknown }>) => void
  /** Initial offset so simultaneously open canvases do not stack exactly. */
  index: number
}

const MIN_W = 420
const MIN_H = 280

export function WorkflowRunCanvas({ run, actions, onClose, onSubmitInput, index }: WorkflowRunCanvasProps) {
  const [pos, setPos] = useState({ x: 80 + index * 28, y: 80 + index * 28 })
  const [size, setSize] = useState({ w: 720, h: 460 })
  const [selected, setSelected] = useState<string | null>(null)
  const [graph, setGraph] = useState<{ nodes: Node[]; edges: Edge[]; byActionId: Map<string, string> } | null>(null)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const drag = useRef<{ mode: 'move' | 'resize'; x: number; y: number; ox: number; oy: number } | null>(null)

  // UDR-0106 D10: the graph comes from the DOCUMENT, laid out before the first event.
  useEffect(() => {
    let cancelled = false
    if (!actions) {
      setGraph(null)
      return
    }
    buildWorkflowGraph(actions).then((g) => {
      if (!cancelled) setGraph({ nodes: g.nodes, edges: g.edges, byActionId: g.byActionId })
    })
    return () => {
      cancelled = true
    }
  }, [actions])

  const onPointerDown = useCallback(
    (mode: 'move' | 'resize') => (e: ReactPointerEvent) => {
      e.preventDefault()
      ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
      drag.current = {
        mode,
        x: e.clientX,
        y: e.clientY,
        ox: mode === 'move' ? pos.x : size.w,
        oy: mode === 'move' ? pos.y : size.h,
      }
    },
    [pos, size],
  )

  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current
      if (!d) return
      const dx = e.clientX - d.x
      const dy = e.clientY - d.y
      if (d.mode === 'move') setPos({ x: Math.max(0, d.ox + dx), y: Math.max(0, d.oy + dy) })
      else setSize({ w: Math.max(MIN_W, d.ox + dx), h: Math.max(MIN_H, d.oy + dy) })
    }
    const up = () => {
      drag.current = null
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [])

  // Paint node STATE onto the document graph. An event whose node is absent from the
  // document (hand-edited YAML, a MAF-generated executor) is surfaced separately rather
  // than dropped, so progress is never silently lost (CTR-0187 failure semantics).
  const { nodes, unmapped } = useMemo(() => {
    if (!graph) return { nodes: [] as Node[], unmapped: Object.values(run.nodes) }
    const painted = graph.nodes.map((n) => {
      const actionId = (n.data as { actionId?: string }).actionId
      const state = actionId ? run.nodes[actionId] : undefined
      if (n.type === 'lane') return n
      return {
        ...n,
        data: {
          ...n.data,
          status: state?.status ?? 'idle',
          selected: selected === n.id,
          onSelect: () => setSelected(n.id),
        },
      }
    })
    // v0.117.1: "outside the diagram" means the BACKEND could not attribute the executor to
    // an authored action (`origin: unmapped`) -- hand-written YAML whose action carries no
    // `id`, so MAF named it. Framework plumbing (`_workflow_entry`) never reaches the client
    // any more, and an `If`'s condition evaluator is reported under the `If` itself, so this
    // list is empty for every workflow authored in the editor. It used to be a permanent,
    // meaningless "_workflow_entry" row.
    const known = new Set(graph.byActionId.keys())
    return {
      nodes: painted,
      unmapped: Object.values(run.nodes).filter((s) => s.origin === 'unmapped' && !known.has(s.node)),
    }
  }, [graph, run.nodes, selected])

  const selectedState: WorkflowNodeRunState | undefined = useMemo(() => {
    if (!selected || !graph) return undefined
    const node = graph.nodes.find((n) => n.id === selected)
    const actionId = (node?.data as { actionId?: string } | undefined)?.actionId
    return actionId ? run.nodes[actionId] : undefined
  }, [selected, graph, run.nodes])

  const done = completedSteps(run)
  const skipped = skippedSteps(run)

  return (
    <div
      className="pointer-events-auto fixed z-40 flex flex-col overflow-hidden rounded-lg border bg-background shadow-2xl"
      style={{ left: pos.x, top: pos.y, width: size.w, height: size.h }}>
      {/* title bar (drag to move) */}
      <div
        onPointerDown={onPointerDown('move')}
        className="flex cursor-move items-center gap-2 border-b bg-muted/40 px-3 py-1.5 text-xs">
        <WorkflowIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate font-medium">{run.workflowName}</span>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {done} step{done === 1 ? '' : 's'}
          {skipped > 0 && ` / ${skipped} skipped`}
        </span>
        <Button variant="ghost" size="icon" className="h-6 w-6" onClick={onClose} aria-label="Close diagram">
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      <PanelGroup direction="horizontal" className="flex min-h-0 flex-1">
        {/* graph */}
        <Panel defaultSize={68} minSize={25} className="min-w-0">
          {graph ? (
            <ReactFlowProvider>
              <ReactFlow
                nodes={nodes}
                edges={graph.edges}
                nodeTypes={nodeTypes}
                fitView
                proOptions={{ hideAttribution: true }}
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable>
                <Background />
              </ReactFlow>
            </ReactFlowProvider>
          ) : (
            <div className="h-full overflow-auto p-3 text-xs">
              <p className="mb-2 text-muted-foreground">
                The workflow diagram is unavailable, so only the steps that reported progress are shown.
              </p>
              <ol className="space-y-1">
                {Object.values(run.nodes)
                  .sort((a, b) => a.order - b.order)
                  .map((n) => (
                    <li key={n.node} className="flex items-center gap-1.5">
                      <StatusIcon status={n.status} className="h-3 w-3" />
                      <span className="truncate">{n.label}</span>
                    </li>
                  ))}
              </ol>
            </div>
          )}
        </Panel>

        {/* draggable pane boundary (v0.117.1) */}
        <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/60 data-[resize-handle-state=drag]:bg-primary" />

        {/* detail + variables */}
        <Panel defaultSize={32} minSize={15} className="flex flex-col overflow-hidden">
          <div className="min-h-0 flex-1 overflow-auto p-2 text-xs">
            {selectedState ? (
              <>
                <div className="mb-1 flex items-center gap-1.5 font-medium">
                  <StatusIcon status={selectedState.status} className="h-3.5 w-3.5" />
                  <span className="truncate">{selectedState.label}</span>
                </div>
                {selectedState.message && <p className="mb-1 text-[11px] text-destructive">{selectedState.message}</p>}
                <ul className="space-y-1">
                  {selectedState.log.map((entry) => (
                    <li key={entry.seq} className="rounded border bg-muted/30 p-1.5">
                      <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                        <span className="font-medium">{entry.status}</span>
                        {entry.iteration != null && <span>iter {entry.iteration}</span>}
                        {entry.truncated && <span>(truncated)</span>}
                      </div>
                      {entry.payload !== undefined && entry.payload !== null && (
                        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all text-[10px]">
                          {typeof entry.payload === 'string' ? entry.payload : JSON.stringify(entry.payload, null, 2)}
                        </pre>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="text-muted-foreground">Select a step to see its log.</p>
            )}
            {selectedState && selectedState.log.length === 0 && (
              <p className="text-[11px] text-muted-foreground">No log was recorded for this step.</p>
            )}

            {unmapped.length > 0 && (
              <div className="mt-3 border-t pt-2">
                <p className="mb-1 text-[10px] font-medium text-muted-foreground">Steps outside the diagram</p>
                <ul className="space-y-0.5">
                  {unmapped
                    .sort((a, b) => a.order - b.order)
                    .map((n) => (
                      <li key={n.node} className="flex items-center gap-1.5 text-[11px]">
                        <StatusIcon status={n.status} className="h-3 w-3" />
                        <span className="truncate">{n.label}</span>
                      </li>
                    ))}
                </ul>
              </div>
            )}
          </div>

          {/* Variable inspector -- present only when the backend emits snapshots, i.e.
              WORKFLOW_STATE_INSPECTOR is on (UDR-0106 D7). Absent, not empty, by default. */}
          {run.snapshot && (
            <div className="max-h-[45%] shrink-0 overflow-auto border-t p-2 text-xs">
              <div className="mb-1 flex items-center gap-1.5 font-medium">
                <Variable className="h-3.5 w-3.5 text-muted-foreground" />
                <span>Variables</span>
                <span className="text-[10px] text-muted-foreground">superstep {run.snapshot.iteration ?? '-'}</span>
              </div>
              {Object.entries(run.snapshot.namespaces).map(([ns, values]) => (
                <div key={ns} className="mb-1.5">
                  <p className="text-[10px] font-medium text-muted-foreground">{ns}</p>
                  <ul className="space-y-0.5">
                    {Object.entries(values).map(([k, v]) => (
                      <li key={k} className="flex gap-1 break-all text-[11px]">
                        <span className="shrink-0 text-muted-foreground">{k}</span>
                        <span className="min-w-0">{typeof v === 'string' ? v : JSON.stringify(v)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              {run.snapshot.redactedKeys ? (
                <p className="text-[10px] text-muted-foreground">{run.snapshot.redactedKeys} value(s) redacted</p>
              ) : null}
            </div>
          )}
        </Panel>
      </PanelGroup>

      {/* Human-in-the-loop input (UDR-0106 D5) */}
      {run.interrupts.length > 0 && (
        <div className="shrink-0 border-t bg-amber-500/5 p-2 text-xs">
          {run.interrupts.map((it) => {
            const meta = it.metadata ?? {}
            const choices = meta.choices ?? []
            const value = answers[it.id] ?? String(meta.default ?? '')
            return (
              <div key={it.id} className="mb-1.5 last:mb-0">
                <p className="mb-1 font-medium">{it.message || meta.prompt || 'Input required'}</p>
                {choices.length > 0 && (
                  <div className="mb-1 flex flex-wrap gap-1">
                    {choices.map((c) => (
                      <Button
                        key={c.value}
                        type="button"
                        size="sm"
                        variant={value === c.value ? 'default' : 'outline'}
                        className="h-6 px-2 text-[11px]"
                        onClick={() => setAnswers((a) => ({ ...a, [it.id]: c.value }))}>
                        {c.label}
                      </Button>
                    ))}
                  </div>
                )}
                {(choices.length === 0 || meta.allow_free_text !== false) && (
                  <input
                    value={value}
                    onChange={(e) => setAnswers((a) => ({ ...a, [it.id]: e.target.value }))}
                    placeholder={meta.variable ? `-> ${meta.variable}` : 'Your answer'}
                    className="w-full rounded-md border border-input bg-background px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-ring"
                  />
                )}
              </div>
            )
          })}
          <div className="mt-1 flex justify-end">
            <Button
              type="button"
              size="sm"
              className="h-6 px-3 text-[11px]"
              disabled={run.interrupts.some((it) => !(answers[it.id] ?? String(it.metadata?.default ?? '')).trim())}
              onClick={() => {
                const collected = Object.fromEntries(
                  run.interrupts.map((it) => {
                    const answer = answers[it.id] ?? String(it.metadata?.default ?? '')
                    return [it.id, { user_input: answer, value: answer }]
                  }),
                )
                // Drop the local drafts with the form; the next question starts blank.
                setAnswers({})
                onSubmitInput(collected)
              }}>
              Submit
            </Button>
          </div>
        </div>
      )}

      {run.status === 'failed' && run.error && (
        <div className="shrink-0 border-t bg-destructive/10 px-3 py-1.5 text-[11px] text-destructive">{run.error}</div>
      )}

      {/* resize handle (pointer-only affordance; hidden from assistive tech) */}
      <div
        onPointerDown={onPointerDown('resize')}
        className="absolute bottom-0 right-0 h-3 w-3 cursor-nwse-resize"
        aria-hidden="true"
      />
    </div>
  )
}
