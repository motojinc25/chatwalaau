import { useCallback, useMemo } from 'react'

/**
 * Harness Agent authoring + management API client (CTR-0194 / CTR-0195, PRP-0135,
 * UDR-0119).
 *
 * Thin wrappers over the backend harness endpoints. The backend is the single
 * validation + serialization source of truth (the UDR-0100 D6/D8 pattern): the
 * editor sends a structured `document` (or raw `yaml`) and the backend returns
 * the canonical YAML and any warnings. Auth rides the session cookie via the
 * global fetch interceptor, so these are plain `fetch` calls like the rest of
 * the SPA.
 */

export interface HarnessDocument {
  name: string
  displayName?: string
  description?: string
  model: { id?: string }
  instructions: { harness?: string; agent?: string }
  /** Flat CTR-0178 identifiers: function:<name> / mcp:<server> (whole servers only). */
  tools: string[]
  compaction: { disabled?: boolean; maxContextWindowTokens?: number | null; maxOutputTokens?: number | null }
  todo: { disabled?: boolean }
  mode: { disabled?: boolean; initial?: string | null }
  fileMemory: { disabled?: boolean }
  fileAccess: { disableWriteTools?: boolean; disableWriteToolApproval?: boolean }
  webSearch: { disabled?: boolean }
  loop: { maxIterations?: number | null }
}

export interface HarnessPolicy {
  model: string
  web_search: 'enabled' | 'disabled' | 'withheld'
  file_memory: boolean
  file_access: boolean
  shell: boolean
  skills: boolean
  todo: boolean
  mode: boolean
  mode_initial: string | null
  /**
   * PRP-0144 / UDR-0125 D4 -- EFFECTIVE compaction, not declared intent. Was a
   * bare boolean, which reported `true` for an agent the framework had built no
   * compaction strategy for. The resolved budgets are present only when enabled.
   */
  compaction: {
    enabled: boolean
    max_context_window_tokens?: number
    max_output_tokens?: number
    source?: 'catalog' | 'yaml' | 'mixed'
  }
  loop_max_iterations: number
  write_tool_approval: boolean
}

export interface HarnessValidationResult {
  valid: boolean
  error: string | null
  warnings: string[]
  yaml: string | null
  summary?: {
    name: string
    description: string
    model: string
    tools: string[]
    policy: HarnessPolicy
    resolved_tools: string[]
  }
}

export interface HarnessEntry {
  id: string
  name: string
  display_name?: string
  description?: string
  group_path: string[]
  kind: 'Harness'
  loaded: boolean
  error?: string | null
  warnings?: string[]
  runnable: boolean
  editable?: boolean
  policy?: HarnessPolicy | null
}

function detailMessage(detail: unknown, fallback: string): string {
  const d = detail as { detail?: { message?: string; error?: string } } | null
  return d?.detail?.message || d?.detail?.error || fallback
}

export function useHarnessAuthoring() {
  const list = useCallback(async (): Promise<HarnessEntry[]> => {
    const res = await fetch('/api/harness-agents')
    if (!res.ok) return []
    const d = (await res.json()) as { agents?: HarnessEntry[] }
    return d.agents ?? []
  }, [])

  const authoringStatus = useCallback(async (): Promise<{ available: boolean; writable: boolean }> => {
    try {
      const res = await fetch('/api/harness-agents/authoring/status')
      if (!res.ok) return { available: false, writable: false }
      const d = (await res.json()) as { available?: boolean; writable?: boolean }
      return { available: Boolean(d.available), writable: Boolean(d.writable) }
    } catch {
      return { available: false, writable: false }
    }
  }, [])

  const validate = useCallback(
    async (body: { document?: HarnessDocument; yaml?: string }): Promise<HarnessValidationResult> => {
      const res = await fetch('/api/harness-agents/authoring/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detailMessage(detail, 'Validation failed'))
      }
      return (await res.json()) as HarnessValidationResult
    },
    [],
  )

  const validateStored = useCallback(async (id: string): Promise<HarnessValidationResult> => {
    const res = await fetch(`/api/harness-agents/${encodeURI(id)}/validate`, { method: 'POST' })
    if (!res.ok) {
      const detail = await res.json().catch(() => null)
      throw new Error(detailMessage(detail, 'Validation failed'))
    }
    return (await res.json()) as HarnessValidationResult
  }, [])

  const loadSource = useCallback(async (id: string): Promise<{ yaml: string; document: HarnessDocument }> => {
    const res = await fetch(`/api/harness-agents/authoring/${encodeURI(id)}/source`)
    if (!res.ok) throw new Error('Failed to load harness agent source')
    return (await res.json()) as { yaml: string; document: HarnessDocument }
  }, [])

  const save = useCallback(
    async (
      body: { document?: HarnessDocument; yaml?: string; name?: string },
      id: string | null,
    ): Promise<{ id?: string }> => {
      const url = id ? `/api/harness-agents/authoring/${encodeURI(id)}` : '/api/harness-agents/authoring'
      const res = await fetch(url, {
        method: id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detailMessage(detail, 'Failed to save harness agent'))
      }
      return (await res.json()) as { id?: string }
    },
    [],
  )

  const remove = useCallback(async (id: string): Promise<void> => {
    const res = await fetch(`/api/harness-agents/authoring/${encodeURI(id)}`, { method: 'DELETE' })
    if (!res.ok) {
      const detail = await res.json().catch(() => null)
      throw new Error(detailMessage(detail, 'Failed to delete harness agent'))
    }
  }, [])

  // Referentially stable (the useAgentAuthoring precedent, PRP-0117 fix).
  return useMemo(
    () => ({ list, authoringStatus, validate, validateStored, loadSource, save, remove }),
    [list, authoringStatus, validate, validateStored, loadSource, save, remove],
  )
}
