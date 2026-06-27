import { Braces, Check } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { ACTIVE_AGENT_CHANGED_EVENT } from '@/components/DeclarativeAgentManager'
import { cn } from '@/lib/utils'

/**
 * Structured Output control (CTR-0118, PRP-0082, UDR-0058).
 *
 * A compact per-message toggle that constrains the assistant's next answer to JSON.
 * Two modes (UDR-0058 D3):
 *   - "json_object": generic JSON (no schema).
 *   - "json_schema": an explicit JSON Schema typed into the editor (used verbatim;
 *     an empty / invalid editor falls back to generic on the backend).
 *
 * The per-session selection (format + schema text) persists in localStorage, the
 * model-options pattern (CTR-0071). Per-model capability is advertised by
 * GET /api/model `structured_output`; when the selected model reports
 * `supported=false` the control is hidden. A non-native (`native=false`) model is
 * still offered but annotated best-effort (UDR-0058 D6). Reports the resolved
 * selection up to ChatPanel, which sends it as AG-UI state.output_schema /
 * state.output_format and the OpenAI Responses API `text.format`.
 */

export type OutputFormat = 'none' | 'json_object' | 'json_schema'

export interface StructuredSelection {
  format: OutputFormat
  schema: Record<string, unknown> | null
}

interface StructuredCapability {
  supported: boolean
  native: boolean
  fallback: string
}

interface ModelInfo {
  models: string[]
  default_model: string
  structured_output?: Record<string, StructuredCapability>
  /** Active declarative agent's structured-output default (CTR-0144, PRP-0094). */
  active_agent?: { output_format?: string; output_schema?: Record<string, unknown> | null }
}

interface StructuredOutputControlProps {
  threadId: string
  selectedModel: string
  onChange: (selection: StructuredSelection) => void
}

const STORAGE_PREFIX = 'chatwalaau-structured-'

function fmtKey(threadId: string): string {
  return `${STORAGE_PREFIX}${threadId}-format`
}
function schemaKey(threadId: string): string {
  return `${STORAGE_PREFIX}${threadId}-schema`
}

/** Parse the schema text; returns null when empty or invalid (-> generic fallback). */
function parseSchema(text: string): Record<string, unknown> | null {
  const t = text.trim()
  if (!t) return null
  try {
    const parsed = JSON.parse(t)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null
  } catch {
    return null
  }
}

function capabilityFor(info: ModelInfo | null, model: string): StructuredCapability | undefined {
  const map = info?.structured_output
  if (!map) return undefined
  return map[model] ?? map[info?.default_model ?? '']
}

export function StructuredOutputControl({ threadId, selectedModel, onChange }: StructuredOutputControlProps) {
  const [info, setInfo] = useState<ModelInfo | null>(null)
  const [format, setFormat] = useState<OutputFormat>('none')
  const [schemaText, setSchemaText] = useState('')
  const [editorOpen, setEditorOpen] = useState(false)

  const loadInfo = useCallback(() => {
    fetch('/api/model')
      .then((res) => res.json())
      .then((data: ModelInfo) => setInfo(data))
      .catch(() => {})
  }, [])

  useEffect(() => {
    loadInfo()
  }, [loadInfo])

  // Re-read /api/model when the active declarative agent changes so the control
  // reflects the new agent's structured-output default (CTR-0144, PRP-0094).
  useEffect(() => {
    const handler = () => loadInfo()
    window.addEventListener(ACTIVE_AGENT_CHANGED_EVENT, handler)
    return () => window.removeEventListener(ACTIVE_AGENT_CHANGED_EVENT, handler)
  }, [loadInfo])

  // Seed the selection: a per-session localStorage choice wins; otherwise the active
  // declarative agent's structured-output default (its JSON Schema is shown in the
  // editor); otherwise off. Re-runs when info refreshes (agent switch) or the thread
  // changes.
  useEffect(() => {
    const storedFmt = localStorage.getItem(fmtKey(threadId))
    if (storedFmt === 'json_object' || storedFmt === 'json_schema') {
      setFormat(storedFmt as OutputFormat)
      setSchemaText(localStorage.getItem(schemaKey(threadId)) ?? '')
      return
    }
    const agent = info?.active_agent
    if (agent?.output_format && agent.output_format !== 'none') {
      const useSchema = agent.output_format === 'json_schema' && agent.output_schema
      setFormat(useSchema ? 'json_schema' : 'json_object')
      setSchemaText(agent.output_schema ? JSON.stringify(agent.output_schema, null, 2) : '')
    } else {
      setFormat('none')
      setSchemaText('')
    }
  }, [info, threadId])

  // Report the resolved selection whenever it changes.
  useEffect(() => {
    onChange({ format, schema: format === 'json_schema' ? parseSchema(schemaText) : null })
  }, [format, schemaText, onChange])

  const setFormatPersisted = useCallback(
    (next: OutputFormat) => {
      setFormat(next)
      localStorage.setItem(fmtKey(threadId), next)
    },
    [threadId],
  )

  const handleSchemaChange = useCallback(
    (text: string) => {
      setSchemaText(text)
      localStorage.setItem(schemaKey(threadId), text)
    },
    [threadId],
  )

  const cap = capabilityFor(info, selectedModel)
  // Hide the control entirely when the model does not support structured output.
  // Default to supported when the map has no entry yet (keeps it visible before
  // /api/model resolves and on unknown models).
  if (info && cap && !cap.supported) return null

  const active = format !== 'none'
  const schemaInvalid = format === 'json_schema' && schemaText.trim().length > 0 && parseSchema(schemaText) === null
  const bestEffort = cap ? !cap.native : false

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => {
          if (active) {
            setFormatPersisted('none')
            setEditorOpen(false)
          } else {
            setFormatPersisted('json_object')
          }
        }}
        onContextMenu={(e) => {
          // Right-click toggles the schema editor without changing on/off state.
          e.preventDefault()
          setEditorOpen((p) => !p)
        }}
        title={
          active
            ? 'Structured output ON (click to turn off; use the menu for a JSON Schema)'
            : 'Constrain the answer to JSON (structured output)'
        }
        className={cn(
          'flex items-center gap-0.5 rounded-md border px-1.5 h-6 text-xs transition-colors',
          active
            ? 'border-primary/40 bg-primary/10 text-primary'
            : 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
        )}>
        <Braces className="h-3 w-3 shrink-0" />
        <span className="hidden sm:inline">{active ? (format === 'json_schema' ? 'Schema' : 'JSON') : 'JSON'}</span>
      </button>

      {active && (
        <button
          type="button"
          onClick={() => setEditorOpen((p) => !p)}
          title="Edit JSON Schema"
          className="ml-0.5 inline-flex h-6 items-center rounded-md border border-transparent px-1 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground">
          {format === 'json_schema' ? 'edit' : '+schema'}
        </button>
      )}

      {editorOpen && (
        <>
          <button
            type="button"
            tabIndex={-1}
            className="fixed inset-0 z-40 cursor-default border-none bg-transparent"
            onClick={() => setEditorOpen(false)}
            aria-label="Close schema editor"
          />
          <div className="absolute bottom-full left-0 z-50 mb-1 w-[320px] rounded-md border bg-popover p-2 shadow-md">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-xs font-medium">JSON Schema (optional)</span>
              <button
                type="button"
                onClick={() => {
                  setFormatPersisted(parseSchema(schemaText) ? 'json_schema' : 'json_object')
                  setEditorOpen(false)
                }}
                className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[11px] text-primary hover:bg-accent">
                <Check className="h-3 w-3" /> Apply
              </button>
            </div>
            <textarea
              value={schemaText}
              onChange={(e) => handleSchemaChange(e.target.value)}
              spellCheck={false}
              placeholder={
                '{\n  "type": "object",\n  "properties": { "answer": { "type": "string" } },\n  "required": ["answer"],\n  "additionalProperties": false\n}'
              }
              className="h-40 w-full resize-none rounded-sm border bg-background p-2 font-mono text-[11px] leading-snug outline-none focus:ring-1 focus:ring-ring"
            />
            <p className="mt-1 text-[10px] text-muted-foreground">
              {schemaInvalid
                ? 'Invalid JSON — generic JSON object will be used.'
                : 'Empty schema = generic JSON object. Strict-compatible schemas validate best.'}
              {bestEffort && ' This model uses a best-effort fallback.'}
            </p>
          </div>
        </>
      )}
    </div>
  )
}
