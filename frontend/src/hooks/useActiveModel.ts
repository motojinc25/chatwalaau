import { useCallback, useEffect, useState } from 'react'
import { ACTIVE_AGENT_CHANGED_EVENT } from '@/components/DeclarativeAgentManager'

/** GET /api/model, the fields this hook needs (CTR-0069). */
interface ModelInfo {
  models: string[]
  default_model: string
  max_context_tokens: number
  max_context_tokens_map: Record<string, number>
}

export interface ActiveModel {
  /** The model that will answer: the active run-target's own (CTR-0069, UDR-0166 D1). */
  model: string
  /** Its context window, for the CTR-0092 context-window indicator. */
  maxContextTokens: number
}

const FALLBACK_CONTEXT_TOKENS = 128000

/**
 * Report which chat model will answer, without offering a choice (PRP-0184).
 *
 * Before PRP-0184 the chat input's `ModelSelector` both CHOSE the model and told
 * the panel its context window. The choice moved to the run-target (UDR-0166 D1),
 * but the panel still needs the second half -- the indicator has to size itself to
 * the window of whichever model actually answers -- so this hook reads it from
 * `/api/model` and nothing else.
 *
 * `default_model` is the right field to read: the registry default IS the active
 * run-target's model, because the Built-in agent's persisted `core_agent_model`
 * and a custom agent's `model.id` both reach the registry through
 * `spec.model_filter`. Re-read on an agent switch (CTR-0144), which is exactly
 * when that model can change.
 */
export function useActiveModel(): ActiveModel {
  const [active, setActive] = useState<ActiveModel>({
    model: '',
    maxContextTokens: FALLBACK_CONTEXT_TOKENS,
  })

  const load = useCallback(() => {
    fetch('/api/model')
      .then((res) => (res.ok ? res.json() : null))
      .then((data: ModelInfo | null) => {
        if (!data) return
        const model = data.default_model || ''
        setActive({
          model,
          maxContextTokens: data.max_context_tokens_map?.[model] ?? data.max_context_tokens ?? FALLBACK_CONTEXT_TOKENS,
        })
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    const handler = () => load()
    window.addEventListener(ACTIVE_AGENT_CHANGED_EVENT, handler)
    return () => window.removeEventListener(ACTIVE_AGENT_CHANGED_EVENT, handler)
  }, [load])

  return active
}
