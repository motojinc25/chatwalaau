import { Check, ChevronDown, Gauge, MessageSquareText, SlidersHorizontal } from 'lucide-react'
import type { ComponentType } from 'react'
import { useCallback, useEffect, useState } from 'react'
import { ACTIVE_AGENT_CHANGED_EVENT } from '@/components/DeclarativeAgentManager'
import { cn } from '@/lib/utils'

/**
 * One generation-option descriptor advertised by the backend (CTR-0102 v4,
 * PRP-0081). v1 renders `enum` options (effort, verbosity); the `number` kind
 * (temperature / top_p) is reserved for a future non-reasoning model and is not
 * rendered here yet (UDR-0057 D3) -- no current model advertises it.
 */
interface OptionDescriptor {
  key: string
  kind: 'enum' | 'number'
  allowed?: string[]
  default: string
  min?: number
  max?: number
  step?: number
}

interface ModelOptionCatalog {
  options: OptionDescriptor[]
}

interface LegacyReasoningCatalog {
  allowed: string[]
  default: string
}

interface ModelInfo {
  models: string[]
  default_model: string
  /** Generalized per-model option catalog (CTR-0069 v4, PRP-0081). */
  model_options?: Record<string, ModelOptionCatalog>
  /** Legacy reasoning-only catalog (CTR-0069 v3); used as a fallback. */
  reasoning_options?: Record<string, LegacyReasoningCatalog>
  /** Active declarative agent option defaults (CTR-0144, PRP-0094). */
  active_agent?: { model_options?: Record<string, string> }
}

interface ModelOptionsSelectorProps {
  threadId: string
  /** The currently selected model; its catalog drives the advertised options. */
  selectedModel: string
  /** Reports the full per-option selection (e.g. {effort, verbosity}). */
  onOptionsChange: (selected: Record<string, string>) => void
}

const STORAGE_PREFIX = 'chatwalaau-modelopt-'

// Per-option presentation. Unknown keys fall back to a generic icon/label.
const OPTION_META: Record<string, { icon: ComponentType<{ className?: string }>; title: string }> = {
  effort: { icon: Gauge, title: 'Reasoning effort' },
  verbosity: { icon: MessageSquareText, title: 'Verbosity' },
}

function metaFor(key: string) {
  return OPTION_META[key] ?? { icon: SlidersHorizontal, title: key }
}

function storageKey(threadId: string, optionKey: string): string {
  return `${STORAGE_PREFIX}${threadId}-${optionKey}`
}

/** Normalize either the generalized catalog or the legacy reasoning catalog. */
function catalogFor(data: ModelInfo, model: string): ModelOptionCatalog | undefined {
  const map = data.model_options
  if (map) return map[model] ?? map[data.default_model]
  const legacy = data.reasoning_options
  if (legacy) {
    const cat = legacy[model] ?? legacy[data.default_model]
    if (cat) return { options: [{ key: 'effort', kind: 'enum', allowed: cat.allowed, default: cat.default }] }
  }
  return undefined
}

/**
 * Catalog-driven model generation-options panel (CTR-0071, PRP-0081, UDR-0057).
 * Sits next to the model selector and renders one compact dropdown per option
 * the selected model advertises (reasoning effort, and for gpt-5.x text
 * verbosity). The allowed values and defaults follow the backend-served catalog
 * (GET /api/model `model_options`); the frontend hardcodes nothing (UDR-0057
 * D2). An option with zero or one choice is not rendered, but every advertised
 * enum option is still reported so the backend receives the resolved selection.
 */
export function ModelOptionsSelector({ threadId, selectedModel, onOptionsChange }: ModelOptionsSelectorProps) {
  const [info, setInfo] = useState<ModelInfo | null>(null)
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [openKey, setOpenKey] = useState<string | null>(null)

  const loadInfo = useCallback(() => {
    fetch('/api/model')
      .then((res) => res.json())
      .then((data: ModelInfo) => setInfo(data))
      .catch(() => {})
  }, [])

  useEffect(() => {
    loadInfo()
  }, [loadInfo])

  // Re-read /api/model when the active declarative agent changes so the panel
  // reflects the new agent's option defaults immediately (CTR-0144, PRP-0094).
  useEffect(() => {
    const handler = () => loadInfo()
    window.addEventListener(ACTIVE_AGENT_CHANGED_EVENT, handler)
    return () => window.removeEventListener(ACTIVE_AGENT_CHANGED_EVENT, handler)
  }, [loadInfo])

  const catalog = info ? catalogFor(info, selectedModel) : undefined

  // When the model (and therefore the catalog) changes, derive each option's
  // value: keep the stored per-session choice if still valid; else the active
  // declarative agent's default for that option (CTR-0144); else the catalog
  // default. Report the full selection so the backend resolves it.
  useEffect(() => {
    if (!catalog) return
    const agentDefaults = info?.active_agent?.model_options ?? {}
    const next: Record<string, string> = {}
    for (const opt of catalog.options) {
      if (opt.kind !== 'enum') continue // number kind reserved (UDR-0057 D3)
      const allowed = opt.allowed ?? []
      const stored = localStorage.getItem(storageKey(threadId, opt.key))
      const agentDefault = agentDefaults[opt.key]
      next[opt.key] =
        stored && allowed.includes(stored)
          ? stored
          : agentDefault && allowed.includes(agentDefault)
            ? agentDefault
            : opt.default
    }
    setSelections(next)
    onOptionsChange(next)
    // `catalog` changes when selectedModel changes or info refreshes (agent switch),
    // so the selection re-derives without listing selectedModel.
  }, [catalog, info, threadId, onOptionsChange])

  const handleSelect = useCallback(
    (optionKey: string, value: string) => {
      setOpenKey(null)
      localStorage.setItem(storageKey(threadId, optionKey), value)
      setSelections((prev) => {
        const next = { ...prev, [optionKey]: value }
        onOptionsChange(next)
        return next
      })
    },
    [threadId, onOptionsChange],
  )

  if (!catalog) return null

  // Only enum options with a real choice are rendered (selection is still
  // reported for all advertised enum options via the effect above).
  const visible = catalog.options.filter((o) => o.kind === 'enum' && (o.allowed?.length ?? 0) > 1)
  if (visible.length === 0) return null

  return (
    <>
      {visible.map((opt) => {
        const meta = metaFor(opt.key)
        const Icon = meta.icon
        const selected = selections[opt.key] ?? opt.default
        const isOpen = openKey === opt.key
        return (
          <div key={opt.key} className="relative">
            <button
              type="button"
              onClick={() => setOpenKey((prev) => (prev === opt.key ? null : opt.key))}
              title={meta.title}
              className="flex items-center gap-0.5 rounded-md border border-transparent px-1.5 h-6 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors">
              <Icon className="h-3 w-3 shrink-0" />
              <span className="max-w-[80px] truncate capitalize">{selected}</span>
              <ChevronDown className="h-3 w-3 shrink-0" />
            </button>

            {isOpen && (
              <>
                <button
                  type="button"
                  tabIndex={-1}
                  className="fixed inset-0 z-40 cursor-default bg-transparent border-none"
                  onClick={() => setOpenKey(null)}
                  aria-label={`Close ${meta.title} menu`}
                />
                <div className="absolute bottom-full mb-1 left-0 z-50 min-w-[140px] rounded-md border bg-popover p-1 shadow-md">
                  {(opt.allowed ?? []).map((value) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => handleSelect(opt.key, value)}
                      className={cn(
                        'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-xs hover:bg-accent hover:text-accent-foreground',
                        value === selected && 'bg-accent/50',
                      )}>
                      <Check className={cn('h-3 w-3', value === selected ? 'opacity-100' : 'opacity-0')} />
                      <span className="capitalize">{value}</span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        )
      })}
    </>
  )
}
