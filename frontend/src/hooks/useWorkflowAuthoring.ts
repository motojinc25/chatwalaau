import { useCallback, useMemo } from 'react'

/**
 * Declarative Workflow API client (CTR-0182 / CTR-0183, PRP-0118, UDR-0101).
 *
 * Thin wrappers over the backend workflow management + authoring endpoints. The
 * backend is the single validation + serialization source of truth (UDR-0101 D9):
 * the editor sends a structured `document` (or raw `yaml`) and the backend returns
 * the canonical YAML and any warnings. Auth rides the session cookie via the global
 * fetch interceptor, so these are plain `fetch` calls like the rest of the SPA.
 */

/** One declarative workflow action (mapped subset; free-form per kind). */
export interface WorkflowAction {
  kind: string
  id?: string
  // SendActivity
  activity?: { text?: string }
  // InvokeAzureAgent
  agentName?: string
  // SetValue / SetVariable
  path?: string
  value?: unknown
  // If
  condition?: string
  // Foreach
  source?: string
  [key: string]: unknown
}

export interface WorkflowDocument {
  name: string
  description?: string
  maxTurns?: number | null
  actions: WorkflowAction[]
}

export interface WorkflowValidationResult {
  valid: boolean
  error: string | null
  warnings: string[]
  yaml?: string | null
  summary?: {
    name: string
    description: string
    referenced_agents: string[]
    action_kinds: string[]
  }
}

export interface WorkflowEntry {
  id: string
  name: string
  description?: string
  group_path: string[]
  source: 'custom'
  loaded: boolean
  error?: string | null
  warnings?: string[]
  referenced_agents?: string[]
  action_kinds?: string[]
  editable?: boolean
}

export interface WorkflowInventory {
  workflows_dir: string
  workflows: WorkflowEntry[]
}

function detailMessage(detail: unknown, fallback: string): string {
  const d = detail as { detail?: { message?: string; error?: string } } | null
  return d?.detail?.message || d?.detail?.error || fallback
}

export function useWorkflowAuthoring() {
  const listWorkflows = useCallback(async (): Promise<WorkflowInventory> => {
    const res = await fetch('/api/workflows')
    if (!res.ok) throw new Error('Failed to load workflows')
    return (await res.json()) as WorkflowInventory
  }, [])

  const validate = useCallback(
    async (body: { document?: WorkflowDocument; yaml?: string }): Promise<WorkflowValidationResult> => {
      const res = await fetch('/api/workflows/authoring/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detailMessage(detail, 'Validation failed'))
      }
      return (await res.json()) as WorkflowValidationResult
    },
    [],
  )

  const loadSource = useCallback(async (id: string): Promise<{ yaml: string; document: WorkflowDocument }> => {
    const res = await fetch(`/api/workflows/authoring/${encodeURI(id)}/source`)
    if (!res.ok) throw new Error('Failed to load workflow source')
    return (await res.json()) as { yaml: string; document: WorkflowDocument }
  }, [])

  const save = useCallback(
    async (
      body: { document?: WorkflowDocument; yaml?: string; name?: string },
      id: string | null,
    ): Promise<{ id?: string }> => {
      const url = id ? `/api/workflows/authoring/${encodeURI(id)}` : '/api/workflows/authoring'
      const res = await fetch(url, {
        method: id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detailMessage(detail, 'Failed to save workflow'))
      }
      return (await res.json()) as { id?: string }
    },
    [],
  )

  const remove = useCallback(async (id: string): Promise<void> => {
    const res = await fetch(`/api/workflows/authoring/${encodeURI(id)}`, { method: 'DELETE' })
    if (!res.ok) {
      const detail = await res.json().catch(() => null)
      throw new Error(detailMessage(detail, 'Failed to delete workflow'))
    }
  }, [])

  const authoringStatus = useCallback(async (): Promise<{ available: boolean; writable: boolean }> => {
    const res = await fetch('/api/workflows/authoring/status')
    if (!res.ok) return { available: false, writable: false }
    const data = (await res.json()) as { available?: boolean; writable?: boolean }
    return { available: Boolean(data.available), writable: Boolean(data.writable) }
  }, [])

  return useMemo(
    () => ({ listWorkflows, validate, loadSource, save, remove, authoringStatus }),
    [listWorkflows, validate, loadSource, save, remove, authoringStatus],
  )
}
