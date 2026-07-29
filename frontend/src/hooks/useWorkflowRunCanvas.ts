import { useCallback, useRef, useState } from 'react'
import type { WorkflowStreamEvent } from '@/hooks/useChat'
import type { WorkflowAction } from '@/hooks/useWorkflowAuthoring'
import { emptyRun, reduceRun, type WorkflowRunModel } from '@/lib/workflowRunState'

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
  const autoOpenRef = useRef(false)

  /** Start a new run canvas and fetch its document graph (UDR-0106 D10). */
  const beginRun = useCallback((workflowId: string, workflowName: string) => {
    const runId = crypto.randomUUID()
    currentRunId.current = runId
    setInstances((prev) => {
      // A new run starts HIDDEN: an ordinary workflow send must not spawn a window
      // unasked. The operator opens it from the indicator's "Diagram" control, and a run
      // that needs them (input required, or failed) surfaces itself in `ingest`.
      const next = [...prev, { run: emptyRun(runId, workflowId, workflowName), actions: null, open: false }]
      return next.slice(-MAX_INSTANCES)
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
   * Hide the canvas. The run's state is RETAINED so the indicator's "Diagram" control can
   * bring it back -- closing a viewer must not discard what it was viewing.
   */
  const close = useCallback((runId: string) => setOpen(runId, false), [setOpen])

  /** Open (or re-open) the canvas for the most recent run. */
  const openCurrent = useCallback(() => {
    const runId = currentRunId.current
    if (runId) setOpen(runId, true)
  }, [setOpen])

  /** True when the most recent run still has a canvas that can be shown. */
  const hasCurrent = useCallback(() => instances.some((i) => i.run.runId === currentRunId.current), [instances])

  /** When true, a new run's canvas pops open on its first event. Default OFF. */
  const setAutoOpen = useCallback((on: boolean) => {
    autoOpenRef.current = on
  }, [])

  return { instances, beginRun, ingest, setOpen, close, openCurrent, hasCurrent, setAutoOpen, currentRunId }
}
