import type { WorkflowInterrupt, WorkflowNodeActivity, WorkflowStreamEvent } from '@/hooks/useChat'

/**
 * Per-run state folded from the standard AG-UI workflow events (CTR-0187, PRP-0123,
 * UDR-0106 D2/D3). Pure reducers so the run canvas and the in-message indicator can
 * share one interpretation of a run without duplicating SSE parsing.
 */

export type WorkflowNodeStatus = 'idle' | 'running' | 'completed' | 'skipped' | 'failed' | 'awaiting_input'

export interface WorkflowLogEntry {
  /** Monotonic sequence so entries keep arrival order without a clock. */
  seq: number
  status: WorkflowNodeStatus
  message?: string
  payload?: unknown
  truncated?: boolean
  iteration?: number | null
}

export type WorkflowNodeOrigin = 'action' | 'derived' | 'unmapped' | 'internal'

export interface WorkflowNodeRunState {
  node: string
  label: string
  status: WorkflowNodeStatus
  /** Where the executor came from; `unmapped` is the only one shown outside the diagram. */
  origin: WorkflowNodeOrigin
  iteration?: number | null
  message?: string
  log: WorkflowLogEntry[]
  /** Arrival order, used to render an ordered fallback list when no graph is available. */
  order: number
}

export interface WorkflowStateSnapshot {
  iteration: number | null
  namespaces: Record<string, Record<string, unknown>>
  truncated?: boolean
  redactedKeys?: number
}

export interface WorkflowRunModel {
  runId: string
  workflowId: string
  workflowName: string
  status: 'running' | 'completed' | 'failed' | 'awaiting_input'
  /** node id (the author's action id) -> state */
  nodes: Record<string, WorkflowNodeRunState>
  /** Latest variable snapshot; absent unless WORKFLOW_STATE_INSPECTOR is on (UDR-0106 D7). */
  snapshot?: WorkflowStateSnapshot
  interrupts: WorkflowInterrupt[]
  error?: string
  seq: number
}

export function emptyRun(runId: string, workflowId: string, workflowName: string): WorkflowRunModel {
  return { runId, workflowId, workflowName, status: 'running', nodes: {}, interrupts: [], seq: 0 }
}

/** Completed-step count: `skipped` is NOT a completed step (UDR-0106 D3). */
export function completedSteps(run: WorkflowRunModel): number {
  return Object.values(run.nodes).filter((n) => n.status === 'completed').length
}

export function skippedSteps(run: WorkflowRunModel): number {
  return Object.values(run.nodes).filter((n) => n.status === 'skipped').length
}

function foldNode(run: WorkflowRunModel, activity: WorkflowNodeActivity): WorkflowRunModel {
  const node = String(activity.node ?? '')
  if (!node) return run
  const status = (activity.status ?? 'running') as WorkflowNodeStatus
  const prev = run.nodes[node]
  const seq = run.seq + 1
  const entry: WorkflowLogEntry = {
    seq,
    status,
    message: activity.message ?? activity.prompt,
    payload: activity.data ?? activity.details,
    truncated: activity.truncated,
    iteration: activity.iteration,
  }
  const next: WorkflowNodeRunState = {
    node,
    label: activity.label || prev?.label || node,
    status,
    origin: activity.origin ?? prev?.origin ?? 'action',
    iteration: activity.iteration ?? prev?.iteration,
    message: activity.message ?? prev?.message,
    log: [...(prev?.log ?? []), entry],
    order: prev?.order ?? Object.keys(run.nodes).length,
  }
  return {
    ...run,
    seq,
    nodes: { ...run.nodes, [node]: next },
    status: status === 'awaiting_input' ? 'awaiting_input' : run.status,
  }
}

export function reduceRun(run: WorkflowRunModel, event: WorkflowStreamEvent): WorkflowRunModel {
  switch (event.kind) {
    case 'node':
      return foldNode(run, event.activity)
    case 'state': {
      const s = event.snapshot as {
        iteration?: number | null
        namespaces?: Record<string, Record<string, unknown>>
        truncated?: boolean
        redacted_keys?: number
      }
      return {
        ...run,
        snapshot: {
          iteration: s.iteration ?? null,
          namespaces: s.namespaces ?? {},
          truncated: s.truncated,
          redactedKeys: s.redacted_keys,
        },
      }
    }
    case 'run_finished':
      return event.interrupts.length > 0
        ? { ...run, status: 'awaiting_input', interrupts: event.interrupts }
        : { ...run, status: 'completed', interrupts: [] }
    case 'run_error': {
      // A step still marked running when the run died did not finish.
      const nodes = Object.fromEntries(
        Object.entries(run.nodes).map(([k, n]) => [
          k,
          n.status === 'running' ? { ...n, status: 'failed' as const } : n,
        ]),
      )
      return { ...run, status: 'failed', error: event.message, nodes }
    }
    default:
      // step_started / step_finished carry no information the ACTIVITY_SNAPSHOT does not
      // already provide; they are accepted (and ignored) so the stream stays additive.
      return run
  }
}
