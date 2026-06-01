import { Check, ChevronDown, Gauge } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { cn } from '@/lib/utils'

interface ReasoningCatalog {
  allowed: string[]
  default: string
}

interface ModelInfo {
  models: string[]
  default_model: string
  reasoning_options?: Record<string, ReasoningCatalog>
}

interface ReasoningSelectorProps {
  threadId: string
  /** The currently selected model; its catalog drives the allowed list + default. */
  selectedModel: string
  onReasoningChange: (effort: string) => void
}

const REASONING_STORAGE_PREFIX = 'chatwalaau-reasoning-'

/**
 * Compact reasoning-effort selector dropdown (CTR-0071, PRP-0071, UDR-0047).
 * Sits next to the model selector. The allowed values and default follow the
 * selected model's backend-served catalog (GET /api/model reasoning_options);
 * the frontend hardcodes nothing (UDR-0047 D2). Hidden when the model exposes
 * no choice (zero or one allowed level).
 */
export function ReasoningSelector({ threadId, selectedModel, onReasoningChange }: ReasoningSelectorProps) {
  const [optionsMap, setOptionsMap] = useState<Record<string, ReasoningCatalog> | null>(null)
  const [defaultModel, setDefaultModel] = useState('')
  const [selected, setSelected] = useState('')
  const [isOpen, setIsOpen] = useState(false)

  useEffect(() => {
    fetch('/api/model')
      .then((res) => res.json())
      .then((data: ModelInfo) => {
        setOptionsMap(data.reasoning_options ?? {})
        setDefaultModel(data.default_model ?? '')
      })
      .catch(() => {})
  }, [])

  // Resolve the catalog for the active model (fall back to the default model).
  const catalog = optionsMap ? (optionsMap[selectedModel] ?? optionsMap[defaultModel]) : undefined

  // When the model (and therefore the catalog) changes, keep the stored choice
  // if it is still valid for this model; otherwise reflect the model default.
  useEffect(() => {
    if (!catalog) return
    const stored = localStorage.getItem(`${REASONING_STORAGE_PREFIX}${threadId}`)
    const next = stored && catalog.allowed.includes(stored) ? stored : catalog.default
    setSelected(next)
    onReasoningChange(next)
    // `catalog` changes when selectedModel changes (per-model entry in the map),
    // so the effort re-derives on model switch without listing selectedModel.
  }, [catalog, threadId, onReasoningChange])

  const handleSelect = useCallback(
    (effort: string) => {
      setSelected(effort)
      setIsOpen(false)
      localStorage.setItem(`${REASONING_STORAGE_PREFIX}${threadId}`, effort)
      onReasoningChange(effort)
    },
    [threadId, onReasoningChange],
  )

  // Hide when there is no catalog or no real choice to make.
  if (!catalog || catalog.allowed.length <= 1) return null

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        title="Reasoning effort"
        className="flex items-center gap-0.5 rounded-md border border-transparent px-1.5 h-6 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors">
        <Gauge className="h-3 w-3 shrink-0" />
        <span className="max-w-[80px] truncate capitalize">{selected}</span>
        <ChevronDown className="h-3 w-3 shrink-0" />
      </button>

      {isOpen && (
        <>
          <button
            type="button"
            tabIndex={-1}
            className="fixed inset-0 z-40 cursor-default bg-transparent border-none"
            onClick={() => setIsOpen(false)}
            aria-label="Close reasoning menu"
          />
          <div className="absolute bottom-full mb-1 left-0 z-50 min-w-[140px] rounded-md border bg-popover p-1 shadow-md">
            {catalog.allowed.map((effort) => (
              <button
                key={effort}
                type="button"
                onClick={() => handleSelect(effort)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-xs hover:bg-accent hover:text-accent-foreground',
                  effort === selected && 'bg-accent/50',
                )}>
                <Check className={cn('h-3 w-3', effort === selected ? 'opacity-100' : 'opacity-0')} />
                <span className="capitalize">{effort}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
