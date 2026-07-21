/**
 * Run-target store (PRP-0118 follow-up, UDR-0101 D5 amended by operator feedback).
 *
 * The chat run-target is decided from the unified Declarative Agents modal, not a
 * composer picker: a Prompt agent is activated server-side (the existing flow), OR a
 * Workflow is selected here as the run-target. When a workflow run-target is set the
 * composer runs that workflow (AG-UI state.workflow_id) and the assistant message is
 * labeled with the workflow name; clearing it (or activating a Prompt agent) returns
 * to the active agent. Persisted in localStorage so the decision survives a reload
 * (SPA-only, per browser). A stale / deleted workflow simply errors at run time and
 * the operator re-selects.
 */

export const RUN_TARGET_CHANGED_EVENT = 'chatwalaau:run-target-changed'

const KEY = 'chatwalaau:workflow-run-target'

export interface WorkflowRunTarget {
  id: string
  name: string
}

export function getWorkflowRunTarget(): WorkflowRunTarget | null {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as WorkflowRunTarget
    return parsed && typeof parsed.id === 'string' ? parsed : null
  } catch {
    return null
  }
}

export function setWorkflowRunTarget(target: WorkflowRunTarget | null): void {
  try {
    if (target) localStorage.setItem(KEY, JSON.stringify(target))
    else localStorage.removeItem(KEY)
  } catch {
    // localStorage may be unavailable; the in-session event still fires.
  }
  window.dispatchEvent(new Event(RUN_TARGET_CHANGED_EVENT))
}
