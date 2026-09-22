import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ACTIVE_AGENT_CHANGED_EVENT } from '@/hooks/useRunTargets'
import { parseSchemaText, validateStrictSchema } from '@/lib/structuredSchema'
import { cn } from '@/lib/utils'

/**
 * Built-in (ChatWalaʻau Core) agent options: model, reasoning effort and -- on the
 * wide surface -- structured output (PRP-0184, CTR-0143 / CTR-0144 / CTR-0216,
 * UDR-0166 D8/D9/D10).
 *
 * These three controls used to sit in the chat input as PER-MESSAGE selections.
 * They are the Built-in agent's CONFIGURATION now, so this component writes them to
 * the Application Settings store (CTR-0199 PATCH) and the backend rebuilds every
 * per-model Agent atomically.
 *
 * The change is SERVER-WIDE and the component says so before it is applied
 * (UDR-0166 D9, the UDR-0158 rule for a server-wide switch): the Built-in agent also
 * answers the Teams channel, the CLI channel, the OpenAI-compatible API and every
 * background lane (session titles, memory, meeting minutes, Ontology NL). The
 * operator asked for exactly that, so this states the reach rather than asking
 * permission for it.
 */

interface OptionDescriptor {
  key: string
  allowed?: string[]
  default: string
}

/** Per-model structured-output capability (CTR-0069 v5, UDR-0058 D6/D9). */
interface StructuredCapability {
  supported: boolean
  native: boolean
  default_schema?: Record<string, unknown> | null
}

interface ModelInfo {
  models: string[]
  default_model: string
  model_options?: Record<string, { options: OptionDescriptor[] }>
  structured_output?: Record<string, StructuredCapability>
}

interface CoreAgentOptionsProps {
  /** Include the structured-output controls (wide surface only; UDR-0166 D10). */
  withStructuredOutput?: boolean
  /** Called after a successful save so the caller can refresh its own view. */
  onSaved?: () => void
  className?: string
}

const CONTROL =
  'h-7 rounded-md border bg-background px-2 text-xs focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50'

/** The settings keys this surface owns (UDR-0166 D8). */
interface CoreSettings {
  core_agent_model: string
  core_agent_effort: string
  core_agent_output_format: string
  core_agent_output_schema: string
}

const EMPTY: CoreSettings = {
  core_agent_model: '',
  core_agent_effort: '',
  core_agent_output_format: '',
  core_agent_output_schema: '',
}

export function CoreAgentOptions({ withStructuredOutput = false, onSaved, className }: CoreAgentOptionsProps) {
  const [info, setInfo] = useState<ModelInfo | null>(null)
  const [stored, setStored] = useState<CoreSettings>(EMPTY)
  const [draft, setDraft] = useState<CoreSettings>(EMPTY)
  const [available, setAvailable] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [modelRes, settingsRes] = await Promise.all([fetch('/api/model'), fetch('/api/app-settings')])
      if (modelRes.ok) setInfo((await modelRes.json()) as ModelInfo)
      if (!settingsRes.ok) {
        // No settings store configured -> the selection cannot be persisted, so the
        // controls are shown disabled with the reason rather than silently failing.
        setAvailable(false)
        return
      }
      // CTR-0199 GET returns the live values under `settings` (AppSettingsStatus).
      const status = (await settingsRes.json()) as { settings?: Record<string, unknown> }
      const values = status.settings ?? {}
      const next: CoreSettings = {
        core_agent_model: String(values.core_agent_model ?? ''),
        core_agent_effort: String(values.core_agent_effort ?? ''),
        core_agent_output_format: String(values.core_agent_output_format ?? ''),
        core_agent_output_schema: String(values.core_agent_output_schema ?? ''),
      }
      setStored(next)
      setDraft(next)
    } catch {
      setAvailable(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const effortAllowed =
    info?.model_options?.[draft.core_agent_model || info.default_model]?.options?.find((o) => o.key === 'effort')
      ?.allowed ?? []

  const activeModel = draft.core_agent_model || info?.default_model || ''
  // UDR-0058 D6/D10: the backend decides whether the model can do structured output at
  // all, and the surface OMITS the feature when it cannot -- offering a control whose
  // first use raises is worse than not offering it.
  const structuredCap = info?.structured_output?.[activeModel]
  const structuredSupported = structuredCap ? structuredCap.supported : true
  // UDR-0058 D9: substituting a default schema is permitted only because it is not
  // SILENT. A closed default (Anthropic cannot express an open object) means "no
  // schema" has a different meaning here than on the OpenAI family, so say so.
  const closedDefault =
    structuredCap?.default_schema != null &&
    (structuredCap.default_schema as { additionalProperties?: unknown }).additionalProperties === false

  // Schema validation (CTR-0118). These guarantees came with the control that used
  // to live in the chat input and they move with it, because the failure they prevent
  // is unchanged: text that does not PARSE is indistinguishable from an empty editor
  // unless something says so, and both would mean "no schema" -- so a stray trailing
  // comma would silently drop the schema and the answers would come back in an
  // unrelated shape. A schema that parses but breaks strict mode is reported with the
  // PATH of the offending sub-schema instead of failing later at the provider.
  const parsed = parseSchemaText(draft.core_agent_output_schema)
  const parseError = parsed.error
  const schemaProblems = parsed.schema ? validateStrictSchema(parsed.schema) : []
  const schemaDropped =
    draft.core_agent_output_format === 'json_schema' &&
    draft.core_agent_output_schema.trim() !== '' &&
    parseError !== null

  const dirty = JSON.stringify(draft) !== JSON.stringify(stored)

  const save = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const body = withStructuredOutput
        ? draft
        : {
            core_agent_model: draft.core_agent_model,
            core_agent_effort: draft.core_agent_effort,
          }
      const res = await fetch('/api/app-settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: body }),
      })
      if (!res.ok) {
        const detail = (await res.json().catch(() => null)) as { detail?: { message?: string } } | null
        throw new Error(detail?.detail?.message || 'Failed to apply')
      }
      setStored(draft)
      // The registry was rebuilt server-side; tell every surface that reads
      // /api/model (the context-window indicator, the run-target name) to re-read.
      window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
      onSaved?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply')
    } finally {
      setBusy(false)
    }
  }, [draft, withStructuredOutput, onSaved])

  return (
    <div className={cn('space-y-2', className)}>
      <div className="flex flex-wrap items-center gap-2">
        <select
          className={cn(CONTROL, 'w-44')}
          disabled={!available || busy}
          value={draft.core_agent_model}
          aria-label="Built-in agent model"
          onChange={(e) => setDraft((d) => ({ ...d, core_agent_model: e.target.value }))}>
          <option value="">Default model{info?.default_model ? ` (${info.default_model})` : ''}</option>
          {(info?.models ?? []).map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        {effortAllowed.length > 0 && (
          <select
            className={cn(CONTROL, 'w-36')}
            disabled={!available || busy}
            value={draft.core_agent_effort}
            aria-label="Built-in agent reasoning effort"
            title="Verbosity and the output budget follow the effort."
            onChange={(e) => setDraft((d) => ({ ...d, core_agent_effort: e.target.value }))}>
            <option value="">effort (default)</option>
            {effortAllowed.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        )}
        {withStructuredOutput && structuredSupported && (
          <select
            className={cn(CONTROL, 'w-40')}
            disabled={!available || busy}
            value={draft.core_agent_output_format}
            aria-label="Built-in agent structured output"
            onChange={(e) => setDraft((d) => ({ ...d, core_agent_output_format: e.target.value }))}>
            <option value="">structured output off</option>
            <option value="json_object">JSON object</option>
            <option value="json_schema">JSON schema</option>
          </select>
        )}
        {/* A parse error has NOTHING to apply: applying it would store text the backend
            cannot use and fall back to the provider's default schema, which is the
            silent drop CTR-0118 exists to prevent. A strict-rule problem is reported
            but not blocking -- it may be a rule this validator judges too narrowly. */}
        <Button size="sm" disabled={parseError !== null || !available || busy || !dirty} onClick={() => void save()}>
          {busy ? 'Applying…' : 'Apply'}
        </Button>
      </div>

      {withStructuredOutput && structuredSupported && closedDefault && draft.core_agent_output_format !== '' && (
        <p className="text-[11px] text-muted-foreground">
          This model cannot return a free-form JSON object, so leaving the schema empty applies its closed default (an
          object with one <span className="font-mono">answer</span> string).
        </p>
      )}

      {withStructuredOutput && structuredSupported && draft.core_agent_output_format === 'json_schema' && (
        <textarea
          className="h-24 w-full rounded-md border bg-background p-2 font-mono text-[11px]"
          disabled={!available || busy}
          spellCheck={false}
          placeholder='{"type": "object", "properties": {...}}'
          aria-label="Built-in agent output schema"
          value={draft.core_agent_output_schema}
          onChange={(e) => setDraft((d) => ({ ...d, core_agent_output_schema: e.target.value }))}
        />
      )}

      {schemaDropped && (
        <p className="text-[11px] text-red-600 dark:text-red-400">
          This is not valid JSON, so the schema is NOT being used ({parseError}). A trailing comma is the usual cause.
        </p>
      )}
      {schemaProblems.length > 0 && (
        <ul className="space-y-0.5 text-[11px] text-amber-700 dark:text-amber-400">
          {schemaProblems.map((problem) => (
            <li key={`${problem.path}:${problem.message}`}>
              <span className="font-mono">{problem.path || '(root)'}</span>: {problem.message}
            </li>
          ))}
        </ul>
      )}
      {!available ? (
        <p className="text-[11px] text-amber-700 dark:text-amber-400">
          APP_SETTINGS_FILE is unset, so the Built-in agent's model and effort cannot be stored.
        </p>
      ) : (
        <p className="text-[11px] text-muted-foreground">
          Applies server-wide and takes effect immediately: chat, Teams, the CLI, the OpenAI-compatible API and the
          background lanes (titles, memory, minutes) all answer with this model and effort.
        </p>
      )}
      {error && <p className="text-[11px] text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
}
