import type { HarnessProgress, HarnessRunState, HarnessTodo } from '@/types/chat'

/**
 * Harness run progress helpers (PRP-0181, CTR-0197 v2, CTR-0219).
 *
 * The `harness_progress` CUSTOM event is always a FULL snapshot, so the reducer is
 * "replace": a lost event is healed by the next one and a replay is idempotent.
 */

const STATES: readonly HarnessRunState[] = ['running', 'completed', 'waiting', 'cap_reached', 'stopped']

/** Validate a `harness_progress` value (or a persisted `usage.harness_run`). */
export function parseHarnessProgress(value: unknown): HarnessProgress | null {
  if (!value || typeof value !== 'object') return null
  const v = value as Record<string, unknown>
  const state = STATES.includes(v.state as HarnessRunState) ? (v.state as HarnessRunState) : 'running'
  const todos: HarnessTodo[] = Array.isArray(v.todos)
    ? (v.todos as unknown[])
        .filter((t): t is Record<string, unknown> => !!t && typeof t === 'object')
        .map((t) => ({ id: Number(t.id ?? 0), title: String(t.title ?? ''), done: t.done === true }))
    : []
  const iteration = typeof v.iteration === 'number' ? v.iteration : 1
  const maxIterations = typeof v.max_iterations === 'number' ? v.max_iterations : iteration
  return {
    run_id: String(v.run_id ?? ''),
    harness_id: String(v.harness_id ?? ''),
    mode: typeof v.mode === 'string' ? v.mode : null,
    iteration,
    max_iterations: maxIterations,
    state,
    todos,
    todos_truncated: v.todos_truncated === true,
  }
}

/** A turn that ended without its final snapshot (Stop / dropped stream) is `stopped`. */
export function finalizeHarnessProgress(progress: HarnessProgress): HarnessProgress {
  return progress.state === 'running' ? { ...progress, state: 'stopped' } : progress
}

export function harnessDoneCount(progress: HarnessProgress): number {
  return progress.todos.filter((t) => t.done).length
}

/** One explicit sentence for the end state (the indicator and the dialog share it). */
export function harnessStateText(progress: HarnessProgress): string {
  const open = progress.todos.length - harnessDoneCount(progress)
  const tasks = `${open} task${open === 1 ? '' : 's'} open`
  switch (progress.state) {
    case 'running':
      return 'Working'
    case 'completed':
      return 'All tasks done'
    case 'waiting':
      return progress.mode === 'plan' ? 'Waiting for your answer (plan mode)' : `Waiting for you -- ${tasks}`
    case 'cap_reached':
      return `Stopped at the ${progress.max_iterations}-iteration limit with ${tasks}`
    case 'stopped':
      return `Stopped -- ${tasks}`
  }
}
