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

/**
 * One declarative workflow action. Permissive on purpose: the editor supports the
 * full Microsoft Agent Framework action surface (23 kinds, PRP-0121) and the backend
 * is the single validator/serializer (UDR-0101 D9). Fields below are the ones the
 * editor reads/writes by kind; the index signature keeps every other MAF field
 * (nested branches, arguments maps, etc.) typechecking and round-tripping.
 */
export interface WorkflowAction {
  kind: string
  id?: string
  // SendActivity
  activity?: { text?: string }
  // InvokeAzureAgent
  agentName?: string
  agent?: { name?: string }
  input?: { messages?: unknown; arguments?: Record<string, unknown> }
  output?: { responseObject?: unknown; messages?: unknown; autoSend?: boolean; result?: unknown }
  // SetValue / SetVariable / SetTextVariable / ResetVariable / ParseValue
  path?: string
  value?: unknown
  variable?: string
  variables?: Record<string, unknown>
  // ParseValue / Foreach
  source?: string
  // Foreach
  itemName?: string
  indexName?: string
  // EditTableV2
  table?: string
  operation?: string
  row?: { key?: string; value?: unknown }
  // If
  condition?: string
  then?: WorkflowAction[]
  else?: WorkflowAction[]
  // ConditionGroup
  conditions?: Array<{ condition?: string; id?: string; actions?: WorkflowAction[] }>
  elseActions?: WorkflowAction[]
  // Foreach / control-flow bodies
  actions?: WorkflowAction[]
  // GotoAction
  actionId?: string
  // InvokeFunctionTool
  functionName?: string
  requireApproval?: boolean
  arguments?: Record<string, unknown>
  // InvokeMcpTool
  serverLabel?: string
  toolName?: string
  // HttpRequestAction
  method?: string
  url?: string
  headers?: Record<string, unknown>
  queryParameters?: Record<string, unknown>
  response?: unknown
  responseHeaders?: unknown
  // Question / RequestExternalInput
  question?: { text?: string }
  prompt?: { text?: string }
  default?: unknown
  // CreateConversation
  conversationId?: string
  [key: string]: unknown
}

/** A top-level workflow input declaration: `inputs: { name: { type, description } }`. */
export interface WorkflowInput {
  type?: string
  description?: string
}

/** A top-level workflow output declaration: `outputs: { name: { type } }`. */
export interface WorkflowOutput {
  type?: string
}

export interface WorkflowDocument {
  name: string
  /** Optional friendly label (YAML `displayName`), shown in the management UI. */
  displayName?: string
  description?: string
  maxTurns?: number | null
  /** Optional declared inputs, keyed by name. */
  inputs?: Record<string, WorkflowInput>
  /** Optional declared outputs, keyed by name. */
  outputs?: Record<string, WorkflowOutput>
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
  display_name?: string
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
