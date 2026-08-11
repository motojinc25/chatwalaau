/**
 * Run-target store (PRP-0118 follow-up, UDR-0101 D5 amended by operator feedback;
 * PRP-0135 adds the Harness run-target, UDR-0119 D3).
 *
 * The chat run-target is decided from the unified Declarative Agents modal, not a
 * composer picker: a Prompt agent is activated server-side (the existing flow), OR a
 * Workflow / Harness agent is selected here as the run-target. When a workflow
 * run-target is set the composer runs that workflow (AG-UI state.workflow_id); when a
 * harness run-target is set the composer runs that harness agent (state.harness_id).
 * AT MOST ONE run-target is effective (UDR-0119 D3): setting one clears the other,
 * and activating a Prompt agent clears both. Persisted in localStorage so the
 * decision survives a reload (SPA-only, per browser). A stale / deleted entity simply
 * errors at run time and the operator re-selects.
 */

export const RUN_TARGET_CHANGED_EVENT = 'chatwalaau:run-target-changed'

const KEY = 'chatwalaau:workflow-run-target'
const HARNESS_KEY = 'chatwalaau:harness-run-target'

export interface WorkflowRunTarget {
  id: string
  name: string
}

export interface HarnessRunTarget {
  id: string
  name: string
}

function readTarget<T extends { id: string }>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as T
    return parsed && typeof parsed.id === 'string' ? parsed : null
  } catch {
    return null
  }
}

export function getWorkflowRunTarget(): WorkflowRunTarget | null {
  return readTarget<WorkflowRunTarget>(KEY)
}

export function setWorkflowRunTarget(target: WorkflowRunTarget | null): void {
  try {
    if (target) {
      localStorage.setItem(KEY, JSON.stringify(target))
      // One effective run-target axis (UDR-0119 D3): a workflow displaces a harness.
      localStorage.removeItem(HARNESS_KEY)
    } else localStorage.removeItem(KEY)
  } catch {
    // localStorage may be unavailable; the in-session event still fires.
  }
  window.dispatchEvent(new Event(RUN_TARGET_CHANGED_EVENT))
}

export function getHarnessRunTarget(): HarnessRunTarget | null {
  return readTarget<HarnessRunTarget>(HARNESS_KEY)
}

export function setHarnessRunTarget(target: HarnessRunTarget | null): void {
  try {
    if (target) {
      localStorage.setItem(HARNESS_KEY, JSON.stringify(target))
      // One effective run-target axis (UDR-0119 D3): a harness displaces a workflow.
      localStorage.removeItem(KEY)
    } else localStorage.removeItem(HARNESS_KEY)
  } catch {
    // localStorage may be unavailable; the in-session event still fires.
  }
  window.dispatchEvent(new Event(RUN_TARGET_CHANGED_EVENT))
}
