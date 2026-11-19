import { useCallback, useMemo } from 'react'

/**
 * Declarative Agent authoring API client (CTR-0177 / CTR-0178, PRP-0117, UDR-0100).
 *
 * Thin wrappers over the backend authoring endpoints. The backend is the single
 * validation + serialization source of truth (UDR-0100 D6/D8): the editor sends a
 * structured `document` (or raw `yaml`) and the backend returns the canonical YAML
 * and any warnings. Auth rides the session cookie via the global fetch interceptor,
 * so these are plain `fetch` calls like the rest of the SPA.
 */

export interface AgentDocument {
  name: string
  displayName?: string
  description?: string
  instructions: string
  model: { id?: string; options?: { effort?: string; verbosity?: string } }
  tools: ToolEntry[]
  outputSchema?: { properties: Record<string, { type: string; description?: string; required?: boolean }> } | null
  /** Knowledge scope (PRP-0256, UDR-0237 D3): written under metadata.chatwalaau.knowledge. */
  knowledge?: KnowledgeScope | null
}

export interface KnowledgeScope {
  rag_collections: string[]
  ontologies: string[]
  grounding: 'off' | 'strict'
  max_distance: number | null
  web_search: boolean
  /** Decision routing (PRP-0258, UDR-0239): /v1/responses route "auto" narrows to one route. */
  routing?: KnowledgeRouting | null
}

/** One route: a business category and the subset of the agent's sources it searches. */
export interface KnowledgeRoute {
  id: string
  description: string
  rag_collections: string[]
  ontologies: string[]
}

export interface KnowledgeRouting {
  instructions: string
  /** null = the App Setting responses_route_min_probability_percent (default 60 %). */
  min_probability: number | null
  low_confidence: 'all' | 'clarify'
  on_error: 'all' | 'fail'
  routes: KnowledgeRoute[]
}

/** Names a knowledge scope can use; null = that source is not configured on this server. */
export interface KnowledgeSources {
  rag_collections: string[] | null
  ontologies: Array<{ id: string; name: string }> | null
}

export interface ToolEntry {
  kind: 'function' | 'mcp' | 'skill'
  name: string
  allowedTools?: string[]
}

export interface ValidationResult {
  valid: boolean
  error: string | null
  warnings: string[]
  yaml: string | null
  summary?: {
    name: string
    description: string
    model_filter: string[] | null
    model_options: Record<string, string> | null
    tool_allowlist: string[] | null
    structured_output: boolean
  }
}

export interface ToolInventory {
  function_tools: Array<{
    identifier: string
    name: string
    category: string
    description: string
    available: boolean
  }>
  mcp_servers: Array<{
    identifier: string
    name: string
    loaded: boolean
    available: boolean
    tools: Array<{ identifier: string; name: string; description: string; available: boolean }>
  }>
  skills: Array<{ identifier: string; name: string; group: string; description: string; available: boolean }>
}

export interface ModelInfo {
  models: string[]
  default_model: string
  model_options?: Record<string, { options: Array<{ key: string; kind: string; allowed?: string[]; default: string }> }>
  /**
   * Per-model structured-output capability (CTR-0069 v5, UDR-0058 D6).
   * PRP-0185 step 2: read by the editor so a Custom agent's output schema can say
   * that the model it pins withholds structured output (UDR-0167 D17).
   */
  structured_output?: Record<string, { supported: boolean; native: boolean }>
  /** Per-model tool capability states (CTR-0069, PRP-0185, UDR-0167). */
  model_capabilities?: Record<string, Record<string, boolean>>
}

function detailMessage(detail: unknown, fallback: string): string {
  const d = detail as { detail?: { message?: string; error?: string } } | null
  return d?.detail?.message || d?.detail?.error || fallback
}

export function useAgentAuthoring() {
  const validate = useCallback(async (body: { document?: AgentDocument; yaml?: string }): Promise<ValidationResult> => {
    const res = await fetch('/api/agents/authoring/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      const detail = await res.json().catch(() => null)
      throw new Error(detailMessage(detail, 'Validation failed'))
    }
    return (await res.json()) as ValidationResult
  }, [])

  const loadInventory = useCallback(async (): Promise<ToolInventory> => {
    const res = await fetch('/api/agents/tool-inventory')
    if (!res.ok) throw new Error('Failed to load tool inventory')
    return (await res.json()) as ToolInventory
  }, [])

  const loadKnowledgeSources = useCallback(async (): Promise<KnowledgeSources> => {
    const res = await fetch('/api/agents/authoring/knowledge-sources')
    if (!res.ok) return { rag_collections: null, ontologies: null }
    return (await res.json()) as KnowledgeSources
  }, [])

  const loadModels = useCallback(async (): Promise<ModelInfo> => {
    const res = await fetch('/api/model')
    if (!res.ok) throw new Error('Failed to load models')
    return (await res.json()) as ModelInfo
  }, [])

  const loadSource = useCallback(async (id: string): Promise<{ yaml: string; document: AgentDocument }> => {
    const res = await fetch(`/api/agents/authoring/${encodeURI(id)}/source`)
    if (!res.ok) throw new Error('Failed to load agent source')
    return (await res.json()) as { yaml: string; document: AgentDocument }
  }, [])

  const save = useCallback(
    async (
      body: { document?: AgentDocument; yaml?: string; name?: string },
      id: string | null,
    ): Promise<{ id?: string }> => {
      const url = id ? `/api/agents/authoring/${encodeURI(id)}` : '/api/agents/authoring'
      const res = await fetch(url, {
        method: id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detailMessage(detail, 'Failed to save agent'))
      }
      return (await res.json()) as { id?: string }
    },
    [],
  )

  const remove = useCallback(async (id: string): Promise<void> => {
    const res = await fetch(`/api/agents/authoring/${encodeURI(id)}`, { method: 'DELETE' })
    if (!res.ok) {
      const detail = await res.json().catch(() => null)
      throw new Error(detailMessage(detail, 'Failed to delete agent'))
    }
  }, [])

  // Memoize the returned object so it is referentially STABLE across renders. Each
  // method is already useCallback([])-stable, but a fresh object literal would change
  // identity every render and re-trigger any effect that depends on the hook value
  // (the initial-load effect would loop and never settle -- PRP-0117 fix).
  return useMemo(
    () => ({ validate, loadInventory, loadKnowledgeSources, loadModels, loadSource, save, remove }),
    [validate, loadInventory, loadKnowledgeSources, loadModels, loadSource, save, remove],
  )
}
