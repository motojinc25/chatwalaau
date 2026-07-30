import { useCallback, useRef, useState } from 'react'
import type { WorkflowStreamEvent } from '@/hooks/useChat'
import type { WorkflowAction } from '@/hooks/useWorkflowAuthoring'
import { emptyRun, reduceRun, type WorkflowLogEntry, type WorkflowRunModel } from '@/lib/workflowRunState'
import type { PersistedWorkflowLogEntry, PersistedWorkflowRun } from '@/types/chat'

/**
 * Run-canvas instances (CTR-0187, PRP-0123, UDR-0106 D12).
 *
 * One canvas per workflow RUN, never re-targeted, so a failing run can be compared
 * against the previous one. All state is ephemeral -- nothing is persisted or restored
 * on reload.
 *
 * Closing a canvas HIDES it; the run's node states, logs, and pending input requests are
 * retained so the "Diagram" control on the in-message indicator (CTR-0185) can re-open it.
 * A run whose canvas was closed is not a run whose progress was thrown away. There is no
 * minimize / dock: closing IS the collapse, and the indicator is the way back.
 */

/** Simultaneously visible canvases. Past this the oldest is hidden (its state is kept). */
export const MAX_OPEN_CANVASES = 3
/** Total retained instances (visible + hidden); the oldest is dropped past this. */
const MAX_INSTANCES = 8
/** Per-step log entries kept when a run is SAVED with its message (v0.117.1). */
const MAX_PERSISTED_LOG_ENTRIES = 8
/** Total characters of step logs a saved run may carry; a session file is not a log store. */
const MAX_PERSISTED_LOG_CHARS = 60_000

export interface WorkflowCanvasInstance {
  run: WorkflowRunModel
  /** Document actions for the graph; null while loading or when unavailable. */
  actions: WorkflowAction[] | null
  /** Whether the window is on screen. A hidden instance keeps all of its run state. */
  open: boolean
}

export function useWorkflowRunCanvas() {
  const [instances, setInstances] = useState<WorkflowCanvasInstance[]>([])
  const currentRunId = useRef<string | null>(null)
  // v0.117.1: a workflow run opens its canvas by itself. Watching the diagram IS how you
  // watch a workflow, so requiring a click first made the default experience the poorer one.
  const autoOpenRef = useRef(true)

  /** Start a new run canvas and fetch its document graph (UDR-0106 D10). */
  const beginRun = useCallback((workflowId: string, workflowName: string) => {
    const runId = crypto.randomUUID()
    currentRunId.current = runId
    setInstances((prev) => {
      const next = [
        ...prev,
        { run: emptyRun(runId, workflowId, workflowName), actions: null, open: autoOpenRef.current },
      ]
      const trimmed = next.slice(-MAX_INSTANCES)
      let excess = trimmed.filter((i) => i.open).length - MAX_OPEN_CANVASES
      if (excess <= 0) return trimmed
      return trimmed.map((i) => {
        if (i.open && i.run.runId !== runId && excess > 0) {
          excess -= 1
          return { ...i, open: false }
        }
        return i
      })
    })
    // CTR-0182 v-next: the detail response carries the document so the canvas lays the
    // graph out before the first node event. An unreadable source degrades to null.
    fetch(`/api/workflows/${encodeURIComponent(workflowId)}`)
      .then((r) => (r.ok ? (r.json() as Promise<{ document?: { actions?: WorkflowAction[] } | null }>) : null))
      .then((detail) => {
        const actions = detail?.document?.actions ?? null
        setInstances((prev) => prev.map((i) => (i.run.runId === runId ? { ...i, actions } : i)))
      })
      .catch(() => {})
    return runId
  }, [])

  /** Show one canvas, hiding the oldest if that would exceed the visible cap. */
  const setOpen = useCallback((runId: string, open: boolean) => {
    setInstances((prev) => {
      const next = prev.map((i) => (i.run.runId === runId ? { ...i, open } : i))
      if (!open) return next
      // Bound the visible windows: hide the oldest rather than dropping its state.
      let excess = next.filter((i) => i.open).length - MAX_OPEN_CANVASES
      if (excess <= 0) return next
      return next.map((i) => {
        if (i.open && i.run.runId !== runId && excess > 0) {
          excess -= 1
          return { ...i, open: false }
        }
        return i
      })
    })
  }, [])

  /** Fold one standard AG-UI workflow event into the current run. */
  const ingest = useCallback(
    (event: WorkflowStreamEvent) => {
      const runId = currentRunId.current
      if (!runId) return
      let surface = false
      setInstances((prev) =>
        prev.map((i) => {
          if (i.run.runId !== runId) return i
          const run = reduceRun(i.run, event)
          // A run that needs the operator (paused for input, or failed) shows itself:
          // the answer cannot be given from a hidden window.
          if (!i.open && (run.status === 'awaiting_input' || run.status === 'failed' || autoOpenRef.current)) {
            surface = true
          }
          return { ...i, run }
        }),
      )
      if (surface) setOpen(runId, true)
    },
    [setOpen],
  )

  /**
   * The operator answered: the run is no longer waiting on them (v0.117.1).
   *
   * Called BEFORE the resume request is sent, so the input form disappears the moment
   * Submit is pressed rather than lingering for the whole resumed run -- the answer has
   * been given, and a form still on screen invites a second answer to a question that is
   * already closed. The awaiting step goes back to `running`; the resume stream then
   * reports its real state.
   */
  const submitInput = useCallback((runId: string) => {
    setInstances((prev) =>
      prev.map((i) => {
        if (i.run.runId !== runId) return i
        const nodes = Object.fromEntries(
          Object.entries(i.run.nodes).map(([k, n]) =>
            n.status === 'awaiting_input' ? [k, { ...n, status: 'running' as const }] : [k, n],
          ),
        )
        return { ...i, run: { ...i.run, status: 'running', interrupts: [], nodes } }
      }),
    )
  }, [])

  /**
   * Hide the canvas. The run's state is RETAINED so the indicator's "Diagram" control can
   * bring it back -- closing a viewer must not discard what it was viewing.
   */
  const close = useCallback((runId: string) => setOpen(runId, false), [setOpen])

  /** Open (or re-open) the canvas for the most recent run. */
  const openCurrent = useCallback(() => {
    const runId = currentRunId.current
    if (runId) setOpen(runId, true)
  }, [setOpen])

  /**
   * Snapshot the current run for persistence (v0.117.1).
   *
   * Includes the per-step processing logs and the last variable snapshot, so a reloaded
   * chat is the same view as the live one rather than a list of step names. Both are
   * BOUNDED here: a payload is already clipped per action by the backend, but a long or
   * loop-heavy run has many of them, and a session file is not the place for unbounded
   * diagnostics. The most recent entries are kept, since they are the ones being looked at.
   */
  const snapshotCurrent = useCallback((): PersistedWorkflowRun | null => {
    const instance = instances.find((i) => i.run.runId === currentRunId.current)
    if (!instance) return null
    const run = instance.run
    const nodes = Object.values(run.nodes)
    if (nodes.length === 0) return null

    let budget = MAX_PERSISTED_LOG_CHARS
    let logsTruncated = false
    const persistLog = (entries: WorkflowLogEntry[]): PersistedWorkflowLogEntry[] | undefined => {
      const recent = entries.slice(-MAX_PERSISTED_LOG_ENTRIES)
      if (recent.length < entries.length) logsTruncated = true
      const out: PersistedWorkflowLogEntry[] = []
      for (const entry of recent) {
        const rendered: PersistedWorkflowLogEntry = {
          seq: entry.seq,
          status: entry.status,
          ...(entry.message ? { message: entry.message } : {}),
          ...(entry.payload !== undefined && entry.payload !== null ? { payload: entry.payload } : {}),
          ...(entry.truncated ? { truncated: true } : {}),
          ...(entry.iteration != null ? { iteration: entry.iteration } : {}),
        }
        const cost = JSON.stringify(rendered).length
        if (cost > budget) {
          logsTruncated = true
          break
        }
        budget -= cost
        out.push(rendered)
      }
      return out.length > 0 ? out : undefined
    }

    const persistedNodes = nodes
      .sort((a, b) => a.order - b.order)
      .map((n) => {
        // Called ONCE per node: persistLog consumes the shared budget, so evaluating it
        // twice would charge each node's log against it twice.
        const log = persistLog(n.log)
        return {
          node: n.node,
          label: n.label,
          status: n.status,
          origin: n.origin,
          ...(n.iteration != null ? { iteration: n.iteration } : {}),
          ...(n.message ? { message: n.message } : {}),
          ...(log ? { log } : {}),
        }
      })

    return {
      workflow_id: run.workflowId,
      workflow_name: run.workflowName,
      status: run.status,
      steps: nodes.filter((n) => n.status === 'completed').length,
      skipped: nodes.filter((n) => n.status === 'skipped').length,
      ...(run.error ? { error: run.error } : {}),
      // Already redacted and bounded by the backend inspector (CTR-0189).
      ...(run.snapshot ? { snapshot: run.snapshot } : {}),
      ...(logsTruncated ? { logs_truncated: true } : {}),
      nodes: persistedNodes,
    }
  }, [instances])

  /**
   * Re-open a run restored from history (v0.117.1). The graph comes from the document as
   * always (UDR-0106 D10); the persisted node states repaint it. Per-action logs are absent
   * because they were never saved, so the detail pane shows the step's final state only.
   */
  const openRestored = useCallback((persisted: PersistedWorkflowRun) => {
    const runId = `restored:${persisted.workflow_id}:${persisted.nodes.length}:${persisted.steps}`
    currentRunId.current = runId
    const run = emptyRun(runId, persisted.workflow_id, persisted.workflow_name)
    run.status = persisted.status
    if (persisted.error) run.error = persisted.error
    // v0.117.1: the variables and the per-step logs come back too, so a restored run is the
    // same view as the live one instead of a list of step names with empty detail panes.
    if (persisted.snapshot) run.snapshot = persisted.snapshot
    persisted.nodes.forEach((n, order) => {
      run.nodes[n.node] = {
        node: n.node,
        label: n.label,
        status: n.status,
        origin: n.origin ?? 'action',
        iteration: n.iteration ?? null,
        ...(n.message ? { message: n.message } : {}),
        log: (n.log ?? []).map((entry) => ({
          seq: entry.seq,
          status: entry.status,
          message: entry.message,
          payload: entry.payload,
          truncated: entry.truncated,
          iteration: entry.iteration,
        })),
        order,
      }
    })
    setInstances((prev) => {
      if (prev.some((i) => i.run.runId === runId)) {
        return prev.map((i) => (i.run.runId === runId ? { ...i, open: true } : i))
      }
      return [...prev, { run, actions: null, open: true }].slice(-MAX_INSTANCES)
    })
    fetch(`/api/workflows/${encodeURIComponent(persisted.workflow_id)}`)
      .then((r) => (r.ok ? (r.json() as Promise<{ document?: { actions?: WorkflowAction[] } | null }>) : null))
      .then((detail) => {
        const actions = detail?.document?.actions ?? null
        setInstances((prev) => prev.map((i) => (i.run.runId === runId ? { ...i, actions } : i)))
      })
      .catch(() => {})
  }, [])

  /** True when the most recent run still has a canvas that can be shown. */
  const hasCurrent = useCallback(() => instances.some((i) => i.run.runId === currentRunId.current), [instances])

  /** When true (the default), a run opens its canvas itself. */
  const setAutoOpen = useCallback((on: boolean) => {
    autoOpenRef.current = on
  }, [])

  return {
    instances,
    beginRun,
    ingest,
    setOpen,
    close,
    submitInput,
    openCurrent,
    openRestored,
    snapshotCurrent,
    hasCurrent,
    setAutoOpen,
    currentRunId,
  }
}
