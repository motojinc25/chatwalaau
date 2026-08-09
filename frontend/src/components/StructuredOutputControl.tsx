import { AlertTriangle, Braces, Check } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ACTIVE_AGENT_CHANGED_EVENT } from '@/components/DeclarativeAgentManager'
import { parseSchemaText, validateStrictSchema } from '@/lib/structuredSchema'
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
  /**
   * PRP-0131 / UDR-0058 D9: the schema used when structured output is on and no
   * explicit schema was typed. It is a PROVIDER property: OpenAI-family models get an
   * open object ("any JSON object"), while Anthropic -- which cannot express one
   * (`additionalProperties` must be false and cannot be omitted) -- gets a valid
   * CLOSED default. That is a real difference in what "no schema" produces, so the
   * editor SHOWS it rather than leaving the operator to discover it in the answer.
   */
  default_schema?: Record<string, unknown>
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
  return parseSchemaText(text).schema
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

  // Strict-mode problems in the typed schema (CTR-0118 v2, v0.117.5). An explicit
  // schema is sent to the provider verbatim with strict:true, so a schema that is
  // valid JSON but invalid under strict mode used to be accepted here and rejected
  // there, failing the turn with an HTTP 400 the user never saw.
  //
  // v0.117.5: the PARSE result is kept too. Text that does not parse used to be
  // indistinguishable from an empty editor and silently degraded to the generic
  // "any JSON object" mode, so a stray trailing comma dropped the schema and the
  // answer came back in an unrelated shape with nothing said about it.
  const parsed = useMemo(() => parseSchemaText(schemaText), [schemaText])
  const parsedSchema = parsed.schema
  const parseError = parsed.error
  const schemaProblems = useMemo(() => (parsedSchema ? validateStrictSchema(parsedSchema) : []), [parsedSchema])
  // A schema was typed but is NOT the one being sent -- the turn would silently run
  // unconstrained. Surfaced on the toggle itself, not only inside the editor.
  const schemaDropped = format === 'json_schema' && schemaText.trim().length > 0 && parsedSchema === null

  const cap = capabilityFor(info, selectedModel)
  // Hide the control entirely when the model does not support structured output.
  // Default to supported when the map has no entry yet (keeps it visible before
  // /api/model resolves and on unknown models).
  if (info && cap && !cap.supported) return null

  const active = format !== 'none'
  const bestEffort = cap ? !cap.native : false
  // UDR-0058 D9: a CLOSED default means "no schema" does not mean "any JSON object"
  // on this model, so the editor says what it does mean.
  const defaultSchema = cap?.default_schema
  const closedDefault = defaultSchema !== undefined && defaultSchema.additionalProperties !== true

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
          title={
            schemaDropped
              ? 'The typed schema is not valid JSON and is NOT being used — this turn would run unconstrained. Click to fix it.'
              : 'Edit JSON Schema'
          }
          className={cn(
            'ml-0.5 inline-flex h-6 items-center gap-0.5 rounded-md border border-transparent px-1 text-[11px]',
            schemaDropped
              ? 'text-destructive hover:bg-destructive/10'
              : 'text-muted-foreground hover:bg-muted hover:text-foreground',
          )}>
          {/* v0.117.5: a schema that was typed but cannot be used is surfaced HERE,
              not only inside the editor, so it cannot be missed before sending. */}
          {schemaDropped && <AlertTriangle className="h-3 w-3 shrink-0" />}
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
              {/* v0.117.5: a PARSE error blocks Apply -- the text is not JSON at all,
                  so there is nothing to apply and the old behaviour (silently switching
                  to generic JSON) is what hid the problem. Strict-rule problems stay
                  overridable ("Apply anyway"): those rules track a provider's current
                  behaviour and the operator may know better. */}
              <button
                type="button"
                disabled={parseError !== null}
                onClick={() => {
                  if (parseError !== null) return
                  setFormatPersisted(parsedSchema ? 'json_schema' : 'json_object')
                  setEditorOpen(false)
                }}
                title={
                  parseError !== null
                    ? 'Fix the JSON syntax first — an unparseable schema cannot be applied.'
                    : schemaProblems.length > 0
                      ? 'This schema does not meet the provider’s strict-mode rules and will fail the turn.'
                      : undefined
                }
                className={cn(
                  'inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[11px] hover:bg-accent',
                  parseError !== null
                    ? 'cursor-not-allowed text-muted-foreground opacity-50 hover:bg-transparent'
                    : schemaProblems.length > 0
                      ? 'text-amber-600 dark:text-amber-500'
                      : 'text-primary',
                )}>
                <Check className="h-3 w-3" /> {schemaProblems.length > 0 ? 'Apply anyway' : 'Apply'}
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
            {/* JSON syntax error (CTR-0118 v2, v0.117.5). Reported FIRST and loudly:
                unparseable text used to be treated exactly like an empty editor and
                dropped the schema without a word, so the answer came back in an
                unrelated shape. The parser's message points at the offending
                character (e.g. a trailing comma). */}
            {parseError !== null && (
              <div className="mt-1 rounded-sm border border-destructive/50 bg-destructive/10 p-1.5">
                <p className="flex items-center gap-1 text-[10px] font-medium text-destructive">
                  <AlertTriangle className="h-3 w-3 shrink-0" />
                  This is not valid JSON, so the schema is NOT being used:
                </p>
                <p className="mt-1 pl-4 font-mono text-[10px] leading-snug text-muted-foreground">{parseError}</p>
                <p className="mt-1 pl-4 text-[10px] leading-snug text-muted-foreground">
                  A trailing comma before a closing <code className="font-mono">{'}'}</code> or{' '}
                  <code className="font-mono">]</code> is the most common cause.
                </p>
              </div>
            )}

            {/* Strict-mode problems (CTR-0118 v2, v0.117.5). Listed with the path of
                the offending sub-schema so the fix is obvious; the turn would
                otherwise fail at the provider with a 400 the user never sees. */}
            {schemaProblems.length > 0 && (
              <div className="mt-1 rounded-sm border border-amber-500/40 bg-amber-500/10 p-1.5">
                <p className="flex items-center gap-1 text-[10px] font-medium text-amber-700 dark:text-amber-500">
                  <AlertTriangle className="h-3 w-3 shrink-0" />
                  The provider will reject this schema ({schemaProblems.length}
                  {schemaProblems.length === 1 ? ' problem' : ' problems'}):
                </p>
                <ul className="mt-1 max-h-24 list-disc space-y-0.5 overflow-y-auto pl-4 text-[10px] leading-snug text-muted-foreground">
                  {schemaProblems.map((problem) => (
                    <li key={`${problem.path}:${problem.message}`}>
                      <code className="font-mono">{problem.path}</code> — {problem.message}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {parseError === null && (
              <p className="mt-1 text-[10px] text-muted-foreground">
                {closedDefault
                  ? `Empty schema on this model = ${JSON.stringify(defaultSchema)} — it cannot express "any JSON object", so this is the default shape. `
                  : 'Empty schema = generic JSON object. '}
                An explicit schema is sent in strict mode: every array needs "items", every object needs
                "additionalProperties": false and must list every property in "required".
                {bestEffort && ' This model uses a best-effort fallback.'}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
