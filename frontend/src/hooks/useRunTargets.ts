import { useCallback, useMemo } from 'react'
import { type HarnessEntry, useHarnessAuthoring } from '@/hooks/useHarnessAuthoring'
import type { WorkflowEntry } from '@/hooks/useWorkflowAuthoring'
import { getHarnessRunTarget, getWorkflowRunTarget, setHarnessRunTarget, setWorkflowRunTarget } from '@/lib/runTarget'

/**
 * Run-target switch semantics (CTR-0217, PRP-0176, UDR-0158 D2/D3/D4/D6).
 *
 * THE one place that knows what can run and how a choice is applied. Both switching
 * surfaces -- the wide Declarative Agents & Workflows modal (CTR-0144) and the narrow
 * run-target picker (CTR-0216) -- consume it, so their semantics cannot drift. A
 * surface MUST NOT call the activation API, write the run-target store, or dispatch
 * the change events itself (UDR-0158 D2).
 *
 * The three axes and their reach (UDR-0072 D7, UDR-0119 D3):
 *
 *   prompt   -> PUT /api/agents/active, AgentRegistry rebuild. SERVER-WIDE: it changes
 *               the agent for every browser, chat, Teams and the OpenAI-compatible API.
 *               A surface MUST confirm it first (UDR-0158 D3).
 *   workflow -> validated, then stored in localStorage as the chat run-target.
 *   harness  -> validated, then stored in localStorage as the chat run-target.
 *
 * At most ONE is effective: the store clears the other axis, and a prompt activation
 * clears both. Order is fixed: validate, then set, then notify.
 */

/** Dispatched after the active declarative agent changes (re-exported seam, CTR-0144). */
export const ACTIVE_AGENT_CHANGED_EVENT = 'chatwalaau:active-agent-changed'

export type RunTargetKind = 'prompt' | 'workflow' | 'harness'

export interface PromptAgentEntry {
  id: string
  name: string
  display_name?: string
  description?: string
  group_path: string[]
  source: 'core' | 'custom'
  active: boolean
  loaded: boolean
  error?: string | null
  warnings?: string[]
  editable?: boolean
  tool_allowlist?: string[] | null
}

export interface RunTargetInventory {
  prompts: PromptAgentEntry[]
  workflows: WorkflowEntry[]
  harnesses: HarnessEntry[]
  /** The active Prompt agent id from the server store ("core" when none is set). */
  activeId: string
}

export interface RunTargetChoice {
  kind: RunTargetKind
  id: string
  /** Display name, stored with a workflow / harness target so the composer can label it. */
  name?: string
}

export interface ApplyResult {
  /** Present for a prompt switch: the inventory the activation endpoint returned. */
  agents?: PromptAgentEntry[]
  activeId?: string
}

export interface Selectability {
  selectable: boolean
  /** One short line, shown next to an unselectable row (UDR-0158 D6). */
  reason?: string
}

/** A prompt activation reaches every client, so a surface must confirm it first (D3). */
export function isServerWide(kind: RunTargetKind): boolean {
  return kind === 'prompt'
}

/**
 * Can this entity become the run-target? The judgement lives here, not in a surface
 * (UDR-0158 D6), so both the modal and the picker refuse the same rows for the same
 * reasons the API would.
 */
export function runTargetSelectability(
  kind: RunTargetKind,
  entry: Pick<HarnessEntry, 'loaded' | 'error' | 'warnings'> & { runnable?: boolean },
): Selectability {
  if (entry.loaded === false) return { selectable: false, reason: entry.error || 'This file could not be loaded.' }
  if (entry.error) return { selectable: false, reason: entry.error }
  if (entry.warnings?.length) return { selectable: false, reason: `Resolve its warnings first: ${entry.warnings[0]}` }
  if (kind === 'harness' && entry.runnable === false) {
    return { selectable: false, reason: 'This harness agent is not runnable (demo mode).' }
  }
  return { selectable: true }
}

function detailMessage(detail: unknown, fallback: string): string {
  const d = detail as { detail?: { message?: string; error?: string } } | null
  return d?.detail?.message || d?.detail?.error || fallback
}

export function useRunTargets() {
  const hApi = useHarnessAuthoring()

  /**
   * The three inventories, in parallel. A non-OK response yields an empty list for that
   * kind, so a feature that is disabled in this deployment simply contributes nothing
   * (today's behaviour in the modal).
   */
  const loadInventory = useCallback(async (): Promise<RunTargetInventory> => {
    const [aRes, wRes, harnesses] = await Promise.all([
      fetch('/api/agents')
        .then((r) => (r.ok ? r.json() : { agents: [] }))
        .catch(() => ({ agents: [] })),
      fetch('/api/workflows')
        .then((r) => (r.ok ? r.json() : { workflows: [] }))
        .catch(() => ({ workflows: [] })),
      hApi.list().catch(() => [] as HarnessEntry[]),
    ])
    return {
      prompts: (aRes.agents ?? []) as PromptAgentEntry[],
      workflows: (wRes.workflows ?? []) as WorkflowEntry[],
      harnesses,
      activeId: (aRes.active as string | undefined) ?? 'core',
    }
  }, [hApi])

  /**
   * Apply a choice. Throws with the server's message on failure; the caller renders it.
   * Never catches: a surface decides what a failure looks like on its own screen.
   */
  const applyChoice = useCallback(
    async (choice: RunTargetChoice): Promise<ApplyResult> => {
      if (choice.kind === 'prompt') {
        const res = await fetch('/api/agents/active', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: choice.id }),
        })
        if (!res.ok) {
          const d = await res.json().catch(() => null)
          throw new Error(detailMessage(d, 'Failed to activate agent'))
        }
        const data = (await res.json()) as { active?: string; agents?: PromptAgentEntry[] }
        // The activated agent IS the run-target: clear both client-side axes
        // (UDR-0101 D5, UDR-0119 D3), then tell the surfaces that read /api/model.
        setWorkflowRunTarget(null)
        setHarnessRunTarget(null)
        window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
        return { agents: (data.agents ?? []) as PromptAgentEntry[], activeId: data.active ?? 'core' }
      }

      if (choice.kind === 'harness') {
        // Re-validate the STORED spec (map + factory preflight) before it becomes the
        // run-target, so a non-runnable spec can never be selected (UDR-0119 D8).
        const result = await hApi.validateStored(choice.id)
        if (!result.valid) throw new Error(result.error || 'Harness agent failed validation')
        if (result.warnings.length) throw new Error(`Resolve the warnings first: ${result.warnings[0]}`)
        // The store clears any workflow run-target (one effective axis, UDR-0119 D3).
        setHarnessRunTarget({ id: choice.id, name: choice.name ?? choice.id })
        return {}
      }

      // Compile/validate the STORED workflow (its "build" step) before it goes live.
      const res = await fetch(`/api/workflows/${encodeURI(choice.id)}/validate`, { method: 'POST' })
      if (!res.ok) {
        const d = await res.json().catch(() => null)
        throw new Error(detailMessage(d, 'Workflow failed to compile'))
      }
      const result = (await res.json()) as { valid: boolean; error: string | null; warnings: string[] }
      if (!result.valid) throw new Error(result.error || 'Workflow failed to compile')
      if (result.warnings.length) throw new Error(`Resolve the warnings first: ${result.warnings[0]}`)
      setWorkflowRunTarget({ id: choice.id, name: choice.name ?? choice.id })
      return {}
    },
    [hApi],
  )

  /**
   * Forget a client-side run-target (the entity behind it was deleted). Clearing is a
   * store write, so it belongs here too (UDR-0158 D2); it never touches the server.
   */
  const clearRunTarget = useCallback((kind: 'workflow' | 'harness'): void => {
    if (kind === 'workflow') setWorkflowRunTarget(null)
    else setHarnessRunTarget(null)
  }, [])

  /** Which choice answers the next message right now. */
  const effectiveChoice = useCallback((activeId: string): RunTargetChoice => {
    const wf = getWorkflowRunTarget()
    if (wf) return { kind: 'workflow', id: wf.id, name: wf.name }
    const h = getHarnessRunTarget()
    if (h) return { kind: 'harness', id: h.id, name: h.name }
    return { kind: 'prompt', id: activeId || 'core' }
  }, [])

  return useMemo(
    () => ({ loadInventory, applyChoice, clearRunTarget, effectiveChoice }),
    [loadInventory, applyChoice, clearRunTarget, effectiveChoice],
  )
}
