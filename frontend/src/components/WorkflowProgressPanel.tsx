import { CircleCheck, Loader2, Workflow as WorkflowIcon } from 'lucide-react'
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
  status: 'running' | 'done'
  index: number
}

export interface WorkflowRunState {
  active: boolean
  completed: boolean
  nodes: WorkflowNodeState[]
}

/** Fold a workflow_* CUSTOM event into the run state (pure; used by the parent). */
export function reduceWorkflowEvent(
  state: WorkflowRunState,
  name: string | undefined,
  value: Record<string, unknown> | undefined,
): WorkflowRunState {
  switch (name) {
    case 'workflow_started':
      return { active: true, completed: false, nodes: [] }
    case 'workflow_node_started': {
      const node = String(value?.node ?? 'step')
      if (state.nodes.some((n) => n.node === node && n.status === 'running')) return state
      return { ...state, nodes: [...state.nodes, { node, status: 'running', index: state.nodes.length }] }
    }
    case 'workflow_node_completed': {
      const node = String(value?.node ?? '')
      return {
        ...state,
        nodes: state.nodes.map((n) => (n.node === node && n.status === 'running' ? { ...n, status: 'done' } : n)),
      }
    }
    case 'workflow_completed':
      return { ...state, active: false, completed: true }
    default:
      return state
  }
}

export const EMPTY_WORKFLOW_RUN: WorkflowRunState = { active: false, completed: false, nodes: [] }

export function WorkflowProgressPanel({ state }: { state: WorkflowRunState }) {
  if (!state.active && !state.completed) return null
  if (state.nodes.length === 0 && !state.active) return null
  return (
    <div className="mx-4 mb-2 rounded-md border bg-muted/40 p-2 text-xs">
      <div className="mb-1 flex items-center gap-1.5 font-medium text-muted-foreground">
        <WorkflowIcon className="h-3.5 w-3.5" />
        {state.completed ? 'Workflow complete' : 'Workflow running'}
      </div>
      <ol className="space-y-0.5">
        {state.nodes.map((n) => (
          <li key={`${n.node}-${n.index}`} className="flex items-center gap-1.5">
            {n.status === 'done' ? (
              <CircleCheck className="h-3 w-3 text-primary" />
            ) : (
              <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
            )}
            <span className={cn('truncate', n.status === 'done' && 'text-muted-foreground')}>{n.node}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}
