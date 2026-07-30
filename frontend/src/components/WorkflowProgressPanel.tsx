import { CircleAlert, CircleCheck, Loader2, Maximize2, SkipForward, Workflow as WorkflowIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * Workflow progress panel (CTR-0185, FEAT-0062, PRP-0118, UDR-0101 D8).
 *
 * Renders the live state of a running workflow's graph from the additive CTR-0009
 * CUSTOM workflow_* events the parent collects (workflow_started /
 * workflow_node_started / workflow_node_completed / workflow_completed). Agent-node
 * TEXT output flows through the normal message stream; this panel shows WHICH node
 * is running. Absent an active workflow run the panel renders nothing.
 */

export interface WorkflowNodeState {
  node: string
  /**
   * Human label for the node: the action's `displayName` when the author supplied one,
   * else the action id (PRP-0122, UDR-0105 D7). Optional so a run streamed by an older
   * backend still renders.
   */
  label?: string
  /**
   * `skipped` (PRP-0123, UDR-0106 D3) is a node MAF BYPASSED -- a conditional branch that
   * was not taken. It is not a success and is not counted as a completed step.
   */
  status: 'running' | 'done' | 'skipped' | 'failed'
  index: number
}

export interface WorkflowRunState {
  active: boolean
  completed: boolean
  /** The run ended in an error (v0.116.1); mutually exclusive with `completed`. */
  failed?: boolean
  /** Operator-facing failure text from the workflow_failed / workflow_node_failed event. */
  error?: string
  nodes: WorkflowNodeState[]
  /** completed-node count reported by the workflow_completed event (v0.115.1). */
  steps?: number
}

/** Fold a workflow_* CUSTOM event into the run state (pure; used by the parent). */
export function reduceWorkflowEvent(
  state: WorkflowRunState,
  name: string | undefined,
  value: Record<string, unknown> | undefined,
): WorkflowRunState {
  switch (name) {
    case 'workflow_started': {
      // A RESUMED turn continues the same run (v0.117.1): keep every step already shown.
      // Resetting here is what made earlier actions vanish each time a workflow asked a
      // human-in-the-loop question -- one run must read as one list, not one per answer.
      if (value?.resumed === true) return { ...state, active: true, completed: false, failed: false }
      return { active: true, completed: false, nodes: [] }
    }
    case 'workflow_node_started': {
      const node = String(value?.node ?? 'step')
      const label = value?.label ? String(value.label) : undefined
      if (state.nodes.some((n) => n.node === node && n.status === 'running')) return state
      // On a resume the step may already be listed (it was awaiting input); revive it in
      // place rather than appending a duplicate.
      if (state.nodes.some((n) => n.node === node))
        return { ...state, nodes: state.nodes.map((n) => (n.node === node ? { ...n, status: 'running' } : n)) }
      return { ...state, nodes: [...state.nodes, { node, label, status: 'running', index: state.nodes.length }] }
    }
    case 'workflow_node_completed': {
      const node = String(value?.node ?? '')
      // UDR-0106 D3: a bypassed node reports `skipped: true` and must not read as a success.
      const settled = value?.skipped === true ? ('skipped' as const) : ('done' as const)
      return {
        ...state,
        nodes: state.nodes.map((n) => (n.node === node && n.status === 'running' ? { ...n, status: settled } : n)),
      }
    }
    case 'workflow_node_failed': {
      const node = String(value?.node ?? '')
      const message = value?.message ? String(value.message) : undefined
      return {
        ...state,
        ...(message ? { error: message } : {}),
        nodes: state.nodes.map((n) => (n.node === node && n.status === 'running' ? { ...n, status: 'failed' } : n)),
      }
    }
    case 'workflow_failed': {
      const steps = typeof value?.steps === 'number' ? (value.steps as number) : undefined
      const message = value?.message ? String(value.message) : undefined
      return {
        ...state,
        active: false,
        completed: false,
        failed: true,
        ...(message ? { error: message } : {}),
        ...(steps !== undefined ? { steps } : {}),
        // A step still marked running when the run died did not finish.
        nodes: state.nodes.map((n) => (n.status === 'running' ? { ...n, status: 'failed' } : n)),
      }
    }
    case 'workflow_completed': {
      const steps = typeof value?.steps === 'number' ? (value.steps as number) : undefined
      return { ...state, active: false, completed: true, ...(steps !== undefined ? { steps } : {}) }
    }
    default:
      return state
  }
}

export const EMPTY_WORKFLOW_RUN: WorkflowRunState = { active: false, completed: false, nodes: [] }

/**
 * Embeddable workflow run indicator. Rendered INSIDE the assistant message
 * (ChatMessageItem) -- both live during a run and, on reload, from the persisted
 * completion marker (v0.115.1). There is no longer a standalone panel above the
 * composer; `className` lets the host place it in the message flow.
 */
export function WorkflowProgressPanel({
  state,
  className,
  onOpenCanvas,
}: {
  state: WorkflowRunState
  className?: string
  /** Opens the detached run canvas (CTR-0187). Absent = no control is rendered. */
  onOpenCanvas?: () => void
}) {
  if (!state.active && !state.completed && !state.failed) return null
  return (
    <div
      className={cn(
        'rounded-md border p-2 text-xs',
        state.failed ? 'border-destructive/40 bg-destructive/10' : 'bg-muted/40',
        className,
      )}>
      <div
        className={cn(
          'flex items-center gap-1.5 font-medium',
          state.failed ? 'text-destructive' : 'text-muted-foreground',
          (state.nodes.length > 0 || state.error) && 'mb-1',
        )}>
        {state.failed ? (
          <CircleAlert className="h-3.5 w-3.5" />
        ) : state.completed ? (
          <CircleCheck className="h-3.5 w-3.5 text-primary" />
        ) : (
          <WorkflowIcon className="h-3.5 w-3.5" />
        )}
        {state.failed
          ? `Workflow failed${state.steps ? ` (after ${state.steps} step${state.steps === 1 ? '' : 's'})` : ''}`
          : state.completed
            ? `Workflow complete${state.steps ? ` (${state.steps} step${state.steps === 1 ? '' : 's'})` : ''}`
            : 'Workflow running'}
        {onOpenCanvas && (
          <button
            type="button"
            onClick={onOpenCanvas}
            title="Open the workflow diagram"
            className="ml-auto inline-flex items-center gap-1 rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground">
            <Maximize2 className="h-3 w-3" />
            Diagram
          </button>
        )}
      </div>
      {state.failed && state.error && <p className="mb-1 text-[11px] text-destructive">{state.error}</p>}
      {state.nodes.length > 0 && (
        <ol className="space-y-0.5">
          {state.nodes.map((n) => (
            <li key={`${n.node}-${n.index}`} className="flex items-center gap-1.5">
              {n.status === 'failed' ? (
                <CircleAlert className="h-3 w-3 text-destructive" />
              ) : n.status === 'skipped' ? (
                <SkipForward className="h-3 w-3 text-muted-foreground/70" />
              ) : n.status === 'done' ? (
                <CircleCheck className="h-3 w-3 text-primary" />
              ) : (
                <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
              )}
              <span
                className={cn(
                  'truncate',
                  n.status === 'done' && 'text-muted-foreground',
                  n.status === 'skipped' && 'text-muted-foreground/70 line-through',
                  n.status === 'failed' && 'text-destructive',
                )}>
                {n.label || n.node}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
