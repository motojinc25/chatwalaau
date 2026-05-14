import { Check, ChevronDown } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { cn } from '@/lib/utils'

interface ModelInfo {
  models: string[]
  default_model: string
  max_context_tokens: number
  max_context_tokens_map: Record<string, number>
}

interface ModelSelectorProps {
  threadId: string
  onModelChange: (model: string, maxTokens: number) => void
}

const MODEL_STORAGE_PREFIX = 'chatwalaau-model-'

/**
 * Compact model selector dropdown (CTR-0071, PRP-0035).
 * Hidden when only one model is configured.
 */
export function ModelSelector({ threadId, onModelChange }: ModelSelectorProps) {
  const [modelInfo, setModelInfo] = useState<ModelInfo | null>(null)
  const [selectedModel, setSelectedModel] = useState<string>('')
  const [isOpen, setIsOpen] = useState(false)

  // Fetch available models from backend
  useEffect(() => {
    fetch('/api/model')
      .then((res) => res.json())
      .then((data: ModelInfo) => {
        setModelInfo(data)
        // Restore per-session model from localStorage, or use default
        const stored = localStorage.getItem(`${MODEL_STORAGE_PREFIX}${threadId}`)
        const initial = stored && data.models.includes(stored) ? stored : data.default_model
        setSelectedModel(initial)
        onModelChange(initial, data.max_context_tokens_map[initial] ?? data.max_context_tokens)
      })
      .catch(() => {})
  }, [threadId, onModelChange])

  const handleSelect = useCallback(
    (model: string) => {
      setSelectedModel(model)
      setIsOpen(false)
      localStorage.setItem(`${MODEL_STORAGE_PREFIX}${threadId}`, model)
      if (modelInfo) {
        onModelChange(model, modelInfo.max_context_tokens_map[model] ?? modelInfo.max_context_tokens)
      }
    },
    [threadId, modelInfo, onModelChange],
  )

  // Hide when single model or no data
  if (!modelInfo || modelInfo.models.length <= 1) return null

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        className="flex items-center gap-0.5 rounded-md border border-transparent px-1.5 h-6 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors">
        <span className="max-w-[120px] truncate">{selectedModel}</span>
        <ChevronDown className="h-3 w-3 shrink-0" />
      </button>

      {isOpen && (
        <>
          <button
            type="button"
            tabIndex={-1}
            className="fixed inset-0 z-40 cursor-default bg-transparent border-none"
            onClick={() => setIsOpen(false)}
            aria-label="Close model menu"
          />
          <div className="absolute bottom-full mb-1 left-0 z-50 min-w-[160px] rounded-md border bg-popover p-1 shadow-md">
            {modelInfo.models.map((model) => (
              <button
                key={model}
                type="button"
                onClick={() => handleSelect(model)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-xs hover:bg-accent hover:text-accent-foreground',
                  model === selectedModel && 'bg-accent/50',
                )}>
                <Check className={cn('h-3 w-3', model === selectedModel ? 'opacity-100' : 'opacity-0')} />
                <span>{model}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
