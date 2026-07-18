import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  ChevronDown,
  ChevronRight,
  CircleCheck,
  GripVertical,
  Loader2,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ACTIVE_AGENT_CHANGED_EVENT } from '@/components/DeclarativeAgentManager'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/**
 * Model Settings modal (CTR-0176, PRP-0111, UDR-0090).
 *
 * A sidebar-footer icon (next to the About icon) opens a ~90% modal -- the
 * Declarative Agents management shape -- with a left settings-item list and a
 * right detail pane. The single settings item today is the Model Offering
 * Catalog editor, which COMPOSES the chat/embeddings/image model offerings and
 * their auth references (CTR-0175). Auth is referenced only by environment-
 * variable NAME; secret values are never displayed or accepted here -- the
 * server reports whether each referenced name is set (checked live via
 * /env-status), rendered as a "set"/"not set" chip. Saving PUTs the composed
 * catalog, which rebuilds the per-model agents (a brief "Applying model
 * configuration..." overlay) and broadcasts ACTIVE_AGENT_CHANGED_EVENT so the
 * model selector / options controls refresh without a page reload. In DEMO mode
 * the whole editor is read-only.
 */

type Provider = 'azure-openai' | 'anthropic' | 'openai' | 'foundry'
type Operation = 'chat' | 'embeddings' | 'image'
type Hosting = 'direct' | 'foundry'
type Family = 'openai-reasoning' | 'anthropic-adaptive' | 'bare'

/**
 * Image output-behavior defaults (PRP-0114, UDR-0095 D3). Valid ONLY on an image
 * offering; each field is the operator DEFAULT, still overridable per session (the
 * chat-input Image button, state.image_options) and per call (the model). An unset
 * field falls through to the API default.
 */
interface ImageDefaults {
  size?: string
  quality?: string
  format?: string
  compression?: number
  background?: string
}

interface Offering {
  id: string
  provider: Provider
  model_ref: string
  operations?: Operation[]
  endpoint?: string
  base_url?: string
  api_version?: string
  hosting?: Hosting
  family?: Family
  context_window?: number
  default?: boolean
  api_key_env?: string
  image_defaults?: ImageDefaults
  metadata?: Record<string, unknown>
}

/** A task-model role descriptor (PRP-0115, UDR-0096 D6), served by the backend. */
interface RoleDescriptor {
  key: string
  label: string
  description: string
}

interface CatalogStatus {
  active: boolean
  demo_mode: boolean
  present: boolean
  path: string | null
  valid: boolean
  errors: string[]
  offerings: Offering[]
  /**
   * Task-model assignments as authored (PRP-0115, UDR-0096). A role key -> an
   * offering id (or a { model } object). A full-document PUT MUST round-trip this
   * or a hand-authored block would be erased (the auth_profiles lesson, UDR-0093 D4).
   */
  roles?: Record<string, string | { model?: string }>
  /** Data-driven task-role registry the GUI renders (PRP-0115, UDR-0096 D6). */
  role_registry?: RoleDescriptor[]
  env_status: Record<string, boolean>
  /**
   * Named auth references, as authored (UDR-0093 D4).
   *
   * This editor does not EDIT auth_profiles -- but it must still round-trip them.
   * Before PRP-0112 the save sent only `{ offerings }`, and because the server
   * defaults `auth_profiles` to `{}` and omits an empty block when serializing, a
   * GUI save silently ERASED a hand-authored `auth_profiles` map -- after which any
   * offering referencing it via `auth_profile:` failed validation on the next load.
   * A full-document PUT surface MUST round-trip every field it does not edit.
   */
  auth_profiles?: Record<string, string>
}

/** Offering carrying a stable client-only key for React list rendering. */
type EditableOffering = Offering & { _key: string }

const PROVIDERS: Provider[] = ['azure-openai', 'anthropic', 'openai', 'foundry']
const FAMILIES: Family[] = ['openai-reasoning', 'anthropic-adaptive', 'bare']
const HOSTINGS: Hosting[] = ['direct', 'foundry']
const ALL_OPERATIONS: Operation[] = ['chat', 'embeddings', 'image']

// Image output-default enums (PRP-0114); mirror models_catalog._IMAGE_DEFAULT_ENUMS.
const IMAGE_SIZES = ['auto', '1024x1024', '1024x1536', '1536x1024']
const IMAGE_QUALITIES = ['auto', 'low', 'medium', 'high']
const IMAGE_FORMATS = ['png', 'jpeg', 'webp']
const IMAGE_BACKGROUNDS = ['auto', 'transparent', 'opaque']
const IMAGE_DEFAULTS_HELP =
  'Operator defaults for image generation output. Each is overridable per session (the Image button in the chat input) and per call (the model). Leave "API default" to send nothing. Compression applies to jpeg/webp only.'

/** Keep only the set image_defaults fields; returns undefined when empty. */
function cleanImageDefaults(d: ImageDefaults | undefined): ImageDefaults | undefined {
  if (!d) return undefined
  const out: ImageDefaults = {}
  for (const k of ['size', 'quality', 'format', 'background'] as const) {
    const v = d[k]
    if (typeof v === 'string' && v.trim()) out[k] = v.trim()
  }
  if (typeof d.compression === 'number' && Number.isFinite(d.compression)) {
    out.compression = Math.max(0, Math.min(100, Math.trunc(d.compression)))
  }
  return Object.keys(out).length ? out : undefined
}

const SETTINGS_ITEMS: Array<{ id: string; label: string; description: string }> = [
  {
    id: 'catalog',
    label: 'Model Offering Catalog',
    description: 'Compose the chat, embeddings, and image model offerings and their auth references.',
  },
]

const SECTIONS: Array<{ op: Operation; title: string; hint: string }> = [
  { op: 'chat', title: 'Chat', hint: 'At least one chat offering; exactly one is the default.' },
  { op: 'embeddings', title: 'Embeddings', hint: 'Optional; at most one embeddings offering.' },
  { op: 'image', title: 'Image', hint: 'Optional; at most one image offering.' },
]

const AUTH_HELP =
  'Auth is referenced by environment-variable name; the value is read from the server environment and never shown here.'
const FAMILY_HELP =
  "Reasoning / option catalog override. 'openai-reasoning' exposes reasoning-effort options; 'anthropic-adaptive' exposes adaptive thinking; 'bare' exposes no extra options. Leave as Auto to infer from the provider/model."
const HOSTING_HELP = "Anthropic only. 'direct' = Anthropic API; 'foundry' = served through Azure AI Foundry."
// The literal placeholder token is assembled from two quoted pieces so that the
// sequence `${` never appears in any single string -- neither in the source (which
// would trip biome's noTemplateCurlyInString) NOR in the minified output. A prior
// template-literal trick got constant-folded by the bundler into a real template
// literal `${VAR}`, which threw "VAR is not defined" at runtime. Quoted-string
// concatenation folds to an inert double-quoted string instead.
const ENDPOINT_HELP =
  'endpoint = Azure / Foundry resource URL. base_url = OpenAI-compatible gateway URL. Either may contain $' +
  '{VAR} placeholders resolved from the server environment.'
const PROVIDER_HELP = 'The API surface this model is served through.'

let keySeq = 0
const nextKey = (): string => `o${++keySeq}`

const opsOf = (o: Offering): Operation[] => (o.operations?.length ? o.operations : ['chat'])
const primaryOp = (o: Offering): Operation => opsOf(o)[0]

/** Environment-variable names an offering references (api_key_env, ${VAR}s). */
function offeringEnvNames(o: Offering): string[] {
  const names = new Set<string>()
  if (o.api_key_env?.trim()) names.add(o.api_key_env.trim())
  for (const field of [o.endpoint, o.base_url]) {
    if (!field) continue
    for (const match of field.matchAll(/\$\{([^}]+)\}/g)) names.add(match[1])
  }
  return [...names]
}

function toWireOfferings(offerings: EditableOffering[]): Offering[] {
  return offerings.map(({ _key, ...o }) => {
    const ops = opsOf(o)
    const out: Offering = { id: o.id.trim(), provider: o.provider, model_ref: o.model_ref.trim(), operations: ops }
    if (o.provider === 'anthropic' && o.hosting) out.hosting = o.hosting
    if (o.family) out.family = o.family
    if (o.context_window && o.context_window > 0) out.context_window = o.context_window
    if (o.endpoint?.trim()) out.endpoint = o.endpoint.trim()
    if (o.base_url?.trim()) out.base_url = o.base_url.trim()
    if (o.api_version?.trim()) out.api_version = o.api_version.trim()
    if (o.api_key_env?.trim()) out.api_key_env = o.api_key_env.trim()
    if (ops.includes('chat') && o.default) out.default = true
    if (ops.includes('image')) {
      const d = cleanImageDefaults(o.image_defaults)
      if (d) out.image_defaults = d
    }
    if (o.metadata) out.metadata = o.metadata
    return out
  })
}

/** Normalize a raw role value (string or { model }) to an offering-id string. */
function roleModel(value: string | { model?: string } | undefined): string {
  if (typeof value === 'string') return value.trim()
  if (value && typeof value === 'object' && typeof value.model === 'string') return value.model.trim()
  return ''
}

/** Read the raw roles map into a flat { key -> offering-id } editable map. */
function readRoles(raw: CatalogStatus['roles']): Record<string, string> {
  const out: Record<string, string> = {}
  if (raw && typeof raw === 'object') {
    for (const [k, v] of Object.entries(raw)) out[k] = roleModel(v)
  }
  return out
}

/** Drop unset (empty) role bindings for the wire / dirty comparison. */
function cleanRoles(roles: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(roles)) {
    if (v?.trim()) out[k] = v.trim()
  }
  return out
}

/** The chat offering ids currently composed, for the role-assignment dropdowns. */
function chatOfferingIds(offerings: EditableOffering[]): string[] {
  return offerings
    .filter((o) => opsOf(o).includes('chat'))
    .map((o) => o.id.trim())
    .filter(Boolean)
}

/** Click-to-open help marker (also shows on hover via title). */
function Help({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <span className="relative inline-flex">
      <button
        type="button"
        title={text}
        aria-label="Help"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-3.5 w-3.5 shrink-0 cursor-help items-center justify-center rounded-full border text-[9px] font-normal text-muted-foreground hover:bg-accent">
        ?
      </button>
      {open && (
        <>
          <button
            type="button"
            tabIndex={-1}
            aria-label="Close help"
            className="fixed inset-0 z-30 cursor-default border-none bg-transparent"
            onClick={() => setOpen(false)}
          />
          <span className="absolute left-0 top-5 z-40 w-64 rounded-md border bg-popover p-2 text-[11px] font-normal text-popover-foreground shadow-md">
            {text}
          </span>
        </>
      )}
    </span>
  )
}

function EnvChip({ name, ok }: { name: string; ok: boolean }) {
  return (
    <span
      title={ok ? `${name} is set in the server environment` : `${name} is not set in the server environment`}
      className={cn(
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px]',
        ok
          ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
          : 'bg-destructive/10 text-destructive dark:text-destructive',
      )}>
      {ok ? <CircleCheck className="h-3 w-3" /> : <TriangleAlert className="h-3 w-3" />}
      {name}
      <span className="opacity-70">{ok ? 'set' : 'not set'}</span>
    </span>
  )
}

const CONTROL_CLASS =
  'flex h-8 w-full rounded-md border border-input bg-transparent px-2 py-1 text-xs transition-colors focus-visible:outline-hidden focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50'

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="flex items-center gap-1 text-[11px] font-medium text-muted-foreground">
        {label}
        {hint && <Help text={hint} />}
      </span>
      {children}
    </div>
  )
}

interface OfferingCardProps {
  offering: EditableOffering
  index: number
  readOnly: boolean
  isChat: boolean
  envStatus: Record<string, boolean>
  expanded: boolean
  onToggleExpand: (key: string) => void
  onChange: (index: number, patch: Partial<Offering>) => void
  onRemove: (index: number) => void
  onSetDefault: (index: number) => void
}

function OfferingCard({
  offering,
  index,
  readOnly,
  isChat,
  envStatus,
  expanded,
  onToggleExpand,
  onChange,
  onRemove,
  onSetDefault,
}: OfferingCardProps) {
  const ops = opsOf(offering)
  const isAnthropic = offering.provider === 'anthropic'
  const isImage = ops.includes('image')
  const imgDefaults = offering.image_defaults ?? {}
  const setImgDefault = (patch: Partial<ImageDefaults>) =>
    onChange(index, { image_defaults: { ...imgDefaults, ...patch } })
  const envNames = offeringEnvNames(offering)

  // Drag-to-reorder (UDR-0093 D5). The array order IS the presentation order the
  // chat model selector renders, so dragging a card here is what puts a model where
  // the operator wants it. Reuses @dnd-kit, already a dependency (folder reordering,
  // UDR-0046 D3) -- no new package.
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: offering._key,
    disabled: readOnly,
  })
  const style = { transform: CSS.Transform.toString(transform), transition }

  const toggleOp = (op: Operation, checked: boolean) => {
    const set = new Set(ops)
    if (checked) set.add(op)
    else set.delete(op)
    const next = ALL_OPERATIONS.filter((o) => set.has(o))
    onChange(index, { operations: next.length ? next : ['chat'] })
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      data-offering-key={offering._key}
      className={cn('rounded-md border bg-card p-3', isDragging && 'z-10 opacity-80')}>
      <div className={cn('flex items-center justify-between gap-2', expanded && 'mb-2')}>
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {!readOnly && (
            /* @dnd-kit `attributes` injects role/aria at runtime, which Biome cannot see statically. */
            /* biome-ignore lint/a11y/useAriaPropsSupportedByRole: role is supplied at runtime by @dnd-kit `attributes` */
            <span
              aria-label="Reorder offering"
              className="flex h-6 w-4 shrink-0 cursor-grab touch-none items-center justify-center text-muted-foreground/60 hover:text-foreground active:cursor-grabbing"
              {...attributes}
              {...listeners}>
              <GripVertical className="h-3.5 w-3.5" />
            </span>
          )}
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
            aria-expanded={expanded}
            onClick={() => onToggleExpand(offering._key)}>
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            )}
            <span className="truncate font-mono text-[11px] text-muted-foreground">{offering.id || '(unnamed)'}</span>
            {!expanded && (
              <span className="truncate text-[11px] text-muted-foreground/70">
                {offering.provider}
                {offering.model_ref ? ` - ${offering.model_ref}` : ''}
              </span>
            )}
          </button>
          {isChat && (
            <label
              className="flex shrink-0 items-center gap-1.5 text-[11px] font-medium"
              title="Use as the default chat model (does NOT affect its position in the list)">
              <input
                type="radio"
                name="default-chat"
                checked={offering.default === true}
                disabled={readOnly}
                onChange={() => onSetDefault(index)}
              />
              Default
            </label>
          )}
        </div>
        {!readOnly && (
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-muted-foreground hover:text-destructive"
            onClick={() => onRemove(index)}
            aria-label="Remove offering"
            title="Remove this offering">
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>

      {expanded && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="ID" hint="Unique identifier for this offering; shown in the model selector.">
            <Input
              className="h-8 text-xs"
              value={offering.id}
              disabled={readOnly}
              placeholder="e.g. gpt-4o"
              onChange={(e) => onChange(index, { id: e.target.value })}
            />
          </Field>

          <Field label="Provider" hint={PROVIDER_HELP}>
            <select
              className={CONTROL_CLASS}
              value={offering.provider}
              disabled={readOnly}
              onChange={(e) => onChange(index, { provider: e.target.value as Provider })}>
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Model ref" hint="The provider-specific model / deployment name.">
            <Input
              className="h-8 text-xs"
              value={offering.model_ref}
              disabled={readOnly}
              placeholder="e.g. gpt-4o or claude-sonnet-4"
              onChange={(e) => onChange(index, { model_ref: e.target.value })}
            />
          </Field>

          <Field label="Family" hint={FAMILY_HELP}>
            <select
              className={CONTROL_CLASS}
              value={offering.family ?? ''}
              disabled={readOnly}
              onChange={(e) => onChange(index, { family: (e.target.value || undefined) as Family | undefined })}>
              <option value="">Auto</option>
              {FAMILIES.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Hosting" hint={HOSTING_HELP}>
            <select
              className={CONTROL_CLASS}
              value={offering.hosting ?? ''}
              disabled={readOnly || !isAnthropic}
              title={isAnthropic ? undefined : 'Hosting applies to the anthropic provider only'}
              onChange={(e) => onChange(index, { hosting: (e.target.value || undefined) as Hosting | undefined })}>
              <option value="">{isAnthropic ? '(default)' : 'anthropic only'}</option>
              {HOSTINGS.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Context window" hint="Positive integer token budget (optional).">
            <Input
              type="number"
              min={1}
              className="h-8 text-xs"
              value={offering.context_window ?? ''}
              disabled={readOnly}
              placeholder="e.g. 128000"
              onChange={(e) =>
                onChange(index, { context_window: e.target.value ? Number.parseInt(e.target.value, 10) : undefined })
              }
            />
          </Field>

          <Field label="Endpoint" hint={ENDPOINT_HELP}>
            <Input
              className="h-8 text-xs"
              value={offering.endpoint ?? ''}
              disabled={readOnly}
              placeholder="Azure / Foundry resource URL"
              onChange={(e) => onChange(index, { endpoint: e.target.value })}
            />
          </Field>

          <Field label="Base URL" hint={ENDPOINT_HELP}>
            <Input
              className="h-8 text-xs"
              value={offering.base_url ?? ''}
              disabled={readOnly}
              placeholder="OpenAI-compatible gateway URL"
              onChange={(e) => onChange(index, { base_url: e.target.value })}
            />
          </Field>

          <Field label="API version" hint="Azure API version (optional).">
            <Input
              className="h-8 text-xs"
              value={offering.api_version ?? ''}
              disabled={readOnly}
              placeholder="e.g. 2024-10-21"
              onChange={(e) => onChange(index, { api_version: e.target.value })}
            />
          </Field>

          <Field label="API key env" hint={AUTH_HELP}>
            <Input
              className="h-8 text-xs"
              value={offering.api_key_env ?? ''}
              disabled={readOnly}
              placeholder="ENV VAR NAME (never the value)"
              onChange={(e) => onChange(index, { api_key_env: e.target.value })}
            />
          </Field>

          <Field label="Operations" hint="Which capabilities this offering serves.">
            <div className="flex h-8 items-center gap-3">
              {ALL_OPERATIONS.map((op) => (
                <label key={op} className="flex items-center gap-1 text-[11px]">
                  <input
                    type="checkbox"
                    checked={ops.includes(op)}
                    disabled={readOnly}
                    onChange={(e) => toggleOp(op, e.target.checked)}
                  />
                  {op}
                </label>
              ))}
            </div>
          </Field>
        </div>
      )}

      {expanded && isImage && (
        <div className="mt-3 space-y-2 rounded-md border border-dashed p-2">
          <span className="flex items-center gap-1 text-[11px] font-medium text-muted-foreground">
            Image output defaults
            <Help text={IMAGE_DEFAULTS_HELP} />
          </span>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="Size">
              <select
                className={CONTROL_CLASS}
                value={imgDefaults.size ?? ''}
                disabled={readOnly}
                onChange={(e) => setImgDefault({ size: e.target.value || undefined })}>
                <option value="">API default</option>
                {IMAGE_SIZES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Quality">
              <select
                className={CONTROL_CLASS}
                value={imgDefaults.quality ?? ''}
                disabled={readOnly}
                onChange={(e) => setImgDefault({ quality: e.target.value || undefined })}>
                <option value="">API default</option>
                {IMAGE_QUALITIES.map((q) => (
                  <option key={q} value={q}>
                    {q}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Format">
              <select
                className={CONTROL_CLASS}
                value={imgDefaults.format ?? ''}
                disabled={readOnly}
                onChange={(e) => setImgDefault({ format: e.target.value || undefined })}>
                <option value="">API default</option>
                {IMAGE_FORMATS.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Background">
              <select
                className={CONTROL_CLASS}
                value={imgDefaults.background ?? ''}
                disabled={readOnly}
                onChange={(e) => setImgDefault({ background: e.target.value || undefined })}>
                <option value="">API default</option>
                {IMAGE_BACKGROUNDS.map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Compression" hint="0-100; applies to jpeg / webp only.">
              <Input
                type="number"
                min={0}
                max={100}
                className="h-8 text-xs"
                value={imgDefaults.compression ?? ''}
                disabled={readOnly}
                placeholder="API default"
                onChange={(e) =>
                  setImgDefault({ compression: e.target.value ? Number.parseInt(e.target.value, 10) : undefined })
                }
              />
            </Field>
          </div>
        </div>
      )}

      {expanded && envNames.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">Env</span>
          {envNames.map((name) => (
            <EnvChip key={name} name={name} ok={envStatus[name] === true} />
          ))}
        </div>
      )}
    </div>
  )
}

export function ModelSettingsManager() {
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [status, setStatus] = useState<CatalogStatus | null>(null)
  const [offerings, setOfferings] = useState<EditableOffering[]>([])
  // Not edited here, only preserved across a save (UDR-0093 D4).
  const [authProfiles, setAuthProfiles] = useState<Record<string, string>>({})
  // Task-model assignments (PRP-0115, UDR-0096): role key -> offering id.
  const [roles, setRoles] = useState<Record<string, string>>({})
  const [roleRegistry, setRoleRegistry] = useState<RoleDescriptor[]>([])
  const [baseline, setBaseline] = useState('{}')
  // Which offering cards are expanded for editing. Collapsed by default (UDR-0093
  // D6): dragging full-height editor cards to reorder them is unusable, and the
  // operator asked for the order to be quick to set.
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set())
  const [demoBlocked, setDemoBlocked] = useState(false)
  const [search, setSearch] = useState('')
  const [selectedItem, setSelectedItem] = useState<string>(SETTINGS_ITEMS[0].id)
  const [liveEnv, setLiveEnv] = useState<Record<string, boolean>>({})
  const [scrollKey, setScrollKey] = useState<string | null>(null)
  const [pendingDiscard, setPendingDiscard] = useState<null | 'close' | 'refresh'>(null)
  const bodyRef = useRef<HTMLDivElement>(null)

  const adopt = useCallback((data: CatalogStatus) => {
    setStatus(data)
    setDemoBlocked(data.demo_mode === true)
    const editable = (data.offerings ?? []).map((o) => ({ ...o, operations: opsOf(o), _key: nextKey() }))
    setOfferings(editable)
    // Preserve the auth_profiles block verbatim so the next save sends it back
    // (UDR-0093 D4). We never render or edit it -- we just refuse to destroy it.
    setAuthProfiles(data.auth_profiles ?? {})
    const nextRoles = readRoles(data.roles)
    setRoles(nextRoles)
    setRoleRegistry(data.role_registry ?? [])
    setBaseline(JSON.stringify({ offerings: toWireOfferings(editable), roles: cleanRoles(nextRoles) }))
  }, [])

  // Probe availability once on mount. GET /api/model-offerings is gated by an API
  // key with a localhost dev bypass; the icon shows whenever the endpoint answers.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/model-offerings')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: model settings management is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const fetchCatalog = useCallback(async () => {
    setLoading(true)
    setError(null)
    setSaveError(null)
    try {
      const res = await fetch('/api/model-offerings')
      if (!res.ok) throw new Error('Failed to load model offerings')
      adopt((await res.json()) as CatalogStatus)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load model offerings')
    } finally {
      setLoading(false)
    }
  }, [adopt])

  const openModal = useCallback(() => {
    setOpen(true)
    void fetchCatalog()
  }, [fetchCatalog])

  const readOnly = demoBlocked

  // Dirty = the composed catalog (offerings + task-model roles) differs from what
  // was last loaded/saved.
  const dirty = useMemo(
    () => JSON.stringify({ offerings: toWireOfferings(offerings), roles: cleanRoles(roles) }) !== baseline,
    [offerings, roles, baseline],
  )

  const closeNow = useCallback(() => {
    setOpen(false)
    setError(null)
    setSaveError(null)
    setPendingDiscard(null)
  }, [])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (next) {
        setOpen(true)
        return
      }
      if (busy) return // block close while applying
      if (dirty) {
        setPendingDiscard('close') // confirm before discarding unsaved edits
        return
      }
      closeNow()
    },
    [busy, dirty, closeNow],
  )

  const requestRefresh = useCallback(() => {
    if (dirty) {
      setPendingDiscard('refresh')
      return
    }
    void fetchCatalog()
  }, [dirty, fetchCatalog])

  const confirmDiscard = useCallback(() => {
    const action = pendingDiscard
    setPendingDiscard(null)
    if (action === 'close') closeNow()
    else if (action === 'refresh') void fetchCatalog()
  }, [pendingDiscard, closeNow, fetchCatalog])

  const updateOffering = useCallback((index: number, patch: Partial<Offering>) => {
    setOfferings((prev) => prev.map((o, i) => (i === index ? { ...o, ...patch } : o)))
  }, [])

  const removeOffering = useCallback((index: number) => {
    setOfferings((prev) => prev.filter((_, i) => i !== index))
  }, [])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const toggleExpanded = useCallback((key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  /**
   * Reorder within one operation section, mapping back onto the global array.
   *
   * The catalog is ONE `offerings[]` array; the Chat / Embeddings / Image sections
   * are just a view of it derived from each offering's `operations`. So a drag
   * inside a section must permute only the array slots that section occupies,
   * leaving every other section's slots exactly where they were.
   */
  const handleOfferingDragEnd = useCallback((op: Operation, event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    setOfferings((prev) => {
      const slots = prev.map((o, i) => ({ o, i })).filter(({ o }) => primaryOp(o) === op)
      const items = slots.map(({ o }) => o)
      const from = items.findIndex((o) => o._key === active.id)
      const to = items.findIndex((o) => o._key === over.id)
      if (from < 0 || to < 0 || from === to) return prev
      const moved = arrayMove(items, from, to)
      const next = [...prev]
      slots.forEach(({ i }, k) => {
        next[i] = moved[k]
      })
      return next
    })
  }, [])

  const setDefaultOffering = useCallback((index: number) => {
    setOfferings((prev) => prev.map((o, i) => (opsOf(o).includes('chat') ? { ...o, default: i === index } : o)))
  }, [])

  const addOffering = useCallback((op: Operation) => {
    const key = nextKey()
    setOfferings((prev) => {
      const hasChatDefault = prev.some((o) => opsOf(o).includes('chat') && o.default)
      const fresh: EditableOffering = {
        _key: key,
        id: '',
        provider: 'openai',
        model_ref: '',
        operations: [op],
        default: op === 'chat' && !hasChatDefault ? true : undefined,
      }
      return [...prev, fresh]
    })
    // A fresh offering is blank, so it MUST open expanded -- cards are collapsed by
    // default (UDR-0093 D6), and a collapsed blank card would be unfillable.
    setExpandedKeys((prev) => new Set(prev).add(key))
    setScrollKey(key) // auto-scroll to the new card (#2)
  }, [])

  // Bring a newly added offering card to the TOP of the editor viewport. Scroll
  // the editor container DIRECTLY (not el.scrollIntoView) because the focus() call
  // would otherwise re-scroll the focused input into view and fight it; focus is
  // done with preventScroll so it never moves the viewport. scrollKey is set in
  // the same batch as the insert, so this fires after the new card has committed.
  useLayoutEffect(() => {
    if (!scrollKey) return
    const container = bodyRef.current
    const el = container?.querySelector<HTMLElement>(`[data-offering-key="${scrollKey}"]`)
    if (container && el) {
      const delta = el.getBoundingClientRect().top - container.getBoundingClientRect().top
      container.scrollTo({ top: Math.max(0, container.scrollTop + delta - 8), behavior: 'smooth' })
      el.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true })
    }
    setScrollKey(null)
  }, [scrollKey])

  // Live env validation (#5): whenever the referenced env-var names change, ask the
  // server which are set. Debounced; booleans only (values never leave the server).
  const neededEnvKey = useMemo(() => {
    const s = new Set<string>()
    for (const o of offerings) for (const n of offeringEnvNames(o)) s.add(n)
    return [...s].sort().join(',')
  }, [offerings])

  useEffect(() => {
    if (!open) return
    const names = neededEnvKey ? neededEnvKey.split(',') : []
    if (names.length === 0) {
      setLiveEnv({})
      return
    }
    const timer = setTimeout(async () => {
      try {
        const res = await fetch('/api/model-offerings/env-status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ names }),
        })
        if (res.ok) setLiveEnv((await res.json()) as Record<string, boolean>)
      } catch {
        // Leave the prior env map; chips fall back to the last known state.
      }
    }, 350)
    return () => clearTimeout(timer)
  }, [neededEnvKey, open])

  const envStatus = useMemo(() => ({ ...(status?.env_status ?? {}), ...liveEnv }), [status, liveEnv])

  // Client-side invariant guidance (mirrors CTR-0175 validation). Save is blocked
  // while any of these hold; the backend remains the source of truth.
  const issues = useMemo(() => {
    const out: string[] = []
    const seen = new Set<string>()
    const dups = new Set<string>()
    for (const o of offerings) {
      const id = o.id.trim()
      if (!id) continue
      if (seen.has(id)) dups.add(id)
      seen.add(id)
    }
    if (offerings.some((o) => !o.id.trim())) out.push('Every offering needs an id.')
    if (dups.size) out.push(`Offering ids must be unique (duplicated: ${[...dups].join(', ')}).`)
    if (offerings.some((o) => !o.model_ref.trim())) out.push('Every offering needs a model ref.')

    const chat = offerings.filter((o) => opsOf(o).includes('chat'))
    const emb = offerings.filter((o) => opsOf(o).includes('embeddings'))
    const img = offerings.filter((o) => opsOf(o).includes('image'))
    if (chat.length < 1) out.push('At least one chat offering is required.')
    const defaults = chat.filter((o) => o.default).length
    if (chat.length >= 1 && defaults !== 1) out.push('Exactly one chat offering must be marked default.')
    if (emb.length > 1) out.push('At most one embeddings offering is allowed.')
    if (img.length > 1) out.push('At most one image offering is allowed.')
    return out
  }, [offerings])

  const doSave = useCallback(async () => {
    setBusy(true)
    setError(null)
    setSaveError(null)
    try {
      const res = await fetch('/api/model-offerings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        // auth_profiles is echoed back unchanged (UDR-0093 D4). Omitting it here is
        // what used to erase a hand-authored block.
        body: JSON.stringify({
          offerings: toWireOfferings(offerings),
          ...(Object.keys(authProfiles).length > 0 ? { auth_profiles: authProfiles } : {}),
          // Round-trip task-model assignments (PRP-0115, UDR-0096). Only non-empty
          // bindings are sent; an omitted role means "follow session / default".
          ...(Object.keys(cleanRoles(roles)).length > 0 ? { roles: cleanRoles(roles) } : {}),
        }),
      })
      if (res.ok) {
        adopt((await res.json()) as CatalogStatus)
        // Refresh the model selector / options controls without a page reload (#1).
        window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
        return
      }
      const detail = await res.json().catch(() => null)
      if (res.status === 409 || detail?.error === 'demo_mode') {
        setDemoBlocked(true)
        setSaveError(detail?.message || 'Editing is disabled in demo mode.')
        return
      }
      setSaveError(detail?.message || detail?.error || `Save failed (HTTP ${res.status}).`)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Save failed.')
    } finally {
      setBusy(false)
    }
  }, [offerings, authProfiles, roles, adopt])

  const setRole = useCallback((key: string, offeringId: string) => {
    setRoles((prev) => ({ ...prev, [key]: offeringId }))
  }, [])

  const filteredItems = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return SETTINGS_ITEMS
    return SETTINGS_ITEMS.filter((it) => it.label.toLowerCase().includes(q) || it.description.toLowerCase().includes(q))
  }, [search])

  if (!available) return null

  const activeItem = SETTINGS_ITEMS.find((it) => it.id === selectedItem) ?? SETTINGS_ITEMS[0]

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6 text-muted-foreground"
        onClick={openModal}
        aria-label="Model settings"
        title="Model settings (compose model offerings)">
        <SlidersHorizontal className="h-4 w-4" />
      </Button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle>Model Settings</DialogTitle>
            <DialogDescription>
              Compose the model offerings served to chat, embeddings, and image operations. Auth is referenced by
              environment-variable name only -- secret values live in the server environment and are never shown here.
              Saving rebuilds the per-model agents.
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <>
                {/* Left: settings-item list with search */}
                <div className="flex w-72 shrink-0 flex-col border-r">
                  <div className="border-b p-2">
                    <Input
                      className="h-8 text-xs"
                      value={search}
                      placeholder="Search settings..."
                      onChange={(e) => setSearch(e.target.value)}
                    />
                  </div>
                  <div className="min-h-0 flex-1 overflow-y-auto">
                    {filteredItems.length === 0 ? (
                      <div className="px-3 py-2 text-xs text-muted-foreground">No matching settings.</div>
                    ) : (
                      filteredItems.map((it) => (
                        <button
                          key={it.id}
                          type="button"
                          onClick={() => setSelectedItem(it.id)}
                          className={cn(
                            'flex w-full flex-col gap-0.5 border-b px-3 py-2 text-left',
                            selectedItem === it.id && 'bg-accent',
                          )}>
                          <span className="text-sm font-medium">{it.label}</span>
                          <span className="line-clamp-2 text-[11px] text-muted-foreground">{it.description}</span>
                        </button>
                      ))
                    )}
                  </div>
                </div>

                {/* Right: Model Offering Catalog editor */}
                <div className="flex min-w-0 flex-1 flex-col">
                  <div className="flex items-center justify-between gap-2 border-b px-5 py-3">
                    <div className="min-w-0">
                      <h3 className="truncate text-base font-semibold">{activeItem.label}</h3>
                      <p className="truncate text-[11px] text-muted-foreground">
                        {status?.path ? status.path : 'Catalog path not set'}
                        {status && !status.valid && ' -- catalog invalid'}
                        {dirty && ' -- unsaved changes'}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={requestRefresh}
                        disabled={busy}
                        title="Reload the catalog from the server (re-reads model_offerings.jsonc)">
                        <RefreshCw className="mr-1 h-3.5 w-3.5" /> Refresh
                      </Button>
                      <Button
                        size="sm"
                        onClick={() => void doSave()}
                        disabled={busy || readOnly || issues.length > 0 || !dirty}
                        title={
                          readOnly
                            ? 'Disabled in demo mode'
                            : !dirty
                              ? 'No changes to save'
                              : issues.length > 0
                                ? 'Resolve the highlighted issues before saving'
                                : 'Apply the composed catalog (rebuilds agents)'
                        }>
                        Save
                      </Button>
                    </div>
                  </div>

                  <div ref={bodyRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
                    {readOnly && (
                      <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[12px] text-amber-700 dark:text-amber-400">
                        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>Demo mode: the model catalog is read-only and cannot be changed.</span>
                      </div>
                    )}

                    {saveError && (
                      <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-2 text-[12px] text-destructive">
                        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                        <span className="whitespace-pre-wrap">{saveError}</span>
                      </div>
                    )}

                    {status && status.errors.length > 0 && (
                      <div className="rounded-md border border-destructive/50 bg-destructive/10 p-2 text-[12px] text-destructive">
                        <div className="mb-1 font-medium">The stored catalog has validation errors:</div>
                        <ul className="space-y-0.5 pl-5">
                          {status.errors.map((e) => (
                            <li key={e} className="list-disc whitespace-pre-wrap">
                              {e}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {issues.length > 0 && !readOnly && (
                      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
                        <div className="mb-1 flex items-center gap-1.5 font-medium">
                          <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
                          Resolve these before saving:
                        </div>
                        <ul className="space-y-0.5 pl-5">
                          {issues.map((i) => (
                            <li key={i} className="list-disc">
                              {i}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <p className="rounded-md bg-muted/50 p-2 text-[11px] text-muted-foreground">{AUTH_HELP}</p>

                    {/* Offering composer, grouped by operation. Within a group the
                        cards are drag-reorderable, and that order IS what the chat
                        model selector renders (UDR-0093 D1/D5). Dragging is scoped to
                        one section: a card's section is derived from its `operations`,
                        so a cross-section drag would be meaningless. */}
                    {SECTIONS.map((section) => {
                      const items = offerings.map((o, i) => ({ o, i })).filter(({ o }) => primaryOp(o) === section.op)
                      return (
                        <section key={section.op} className="space-y-2">
                          <div className="flex items-center gap-2">
                            <h4 className="text-sm font-semibold">{section.title}</h4>
                            <span className="text-[11px] text-muted-foreground">{section.hint}</span>
                          </div>
                          {section.op === 'chat' && items.length > 1 && (
                            <p className="text-[11px] text-muted-foreground">
                              Drag to set the order models appear in the chat selector. The default model is
                              preselected, not moved to the top.
                            </p>
                          )}
                          {items.length === 0 && (
                            <p className="text-[11px] text-muted-foreground">No {section.op} offerings.</p>
                          )}
                          <DndContext
                            sensors={sensors}
                            collisionDetection={closestCenter}
                            onDragEnd={(event) => handleOfferingDragEnd(section.op, event)}>
                            <SortableContext
                              items={items.map(({ o }) => o._key)}
                              strategy={verticalListSortingStrategy}>
                              <div className="space-y-2">
                                {items.map(({ o, i }) => (
                                  <OfferingCard
                                    key={o._key}
                                    offering={o}
                                    index={i}
                                    readOnly={readOnly}
                                    isChat={opsOf(o).includes('chat')}
                                    envStatus={envStatus}
                                    expanded={expandedKeys.has(o._key)}
                                    onToggleExpand={toggleExpanded}
                                    onChange={updateOffering}
                                    onRemove={removeOffering}
                                    onSetDefault={setDefaultOffering}
                                  />
                                ))}
                              </div>
                            </SortableContext>
                          </DndContext>
                          {!readOnly && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => addOffering(section.op)}
                              title={`Add a ${section.op} offering`}>
                              <Plus className="mr-1 h-3.5 w-3.5" /> Add {section.op} offering
                            </Button>
                          )}
                        </section>
                      )
                    })}

                    {/* Task model assignments (PRP-0115, UDR-0096). Assign a chat
                        offering to each background / internal task; an unset role
                        follows the session / default model. Registry-driven, so a
                        future task model needs no UI change (D6). */}
                    {roleRegistry.length > 0 && (
                      <section className="space-y-2 border-t pt-4">
                        <div className="flex items-center gap-2">
                          <h4 className="text-sm font-semibold">Task model assignments</h4>
                          <span className="text-[11px] text-muted-foreground">
                            Which model runs each background task. Leave "Follow session / default" to use the chat's
                            own model.
                          </span>
                        </div>
                        {(() => {
                          const chatIds = chatOfferingIds(offerings)
                          return roleRegistry.map((role) => {
                            const bound = roles[role.key] ?? ''
                            const dangling = bound !== '' && !chatIds.includes(bound)
                            return (
                              <div key={role.key} className="flex items-center gap-3 rounded-md border bg-card p-2">
                                <div className="min-w-0 flex-1">
                                  <div className="text-[12px] font-medium">{role.label}</div>
                                  <div className="truncate text-[11px] text-muted-foreground">{role.description}</div>
                                </div>
                                {dangling && (
                                  <span
                                    className="inline-flex shrink-0 items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-400"
                                    title={`'${bound}' is not a current chat offering; this task will use the session / default model.`}>
                                    <TriangleAlert className="h-3 w-3" /> unknown offering
                                  </span>
                                )}
                                <select
                                  className={cn(CONTROL_CLASS, 'w-56 shrink-0')}
                                  value={bound}
                                  disabled={readOnly}
                                  aria-label={`Model for ${role.label}`}
                                  onChange={(e) => setRole(role.key, e.target.value)}>
                                  <option value="">Follow session / default</option>
                                  {dangling && <option value={bound}>{bound} (missing)</option>}
                                  {chatIds.map((id) => (
                                    <option key={id} value={id}>
                                      {id}
                                    </option>
                                  ))}
                                </select>
                              </div>
                            )
                          })
                        })()}
                      </section>
                    )}
                  </div>
                </div>
              </>
            )}

            {/* Applying overlay while a PUT rebuilds the agents */}
            {busy && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
                <div className="flex items-center gap-2 text-sm">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  Applying model configuration...
                </div>
              </div>
            )}

            {/* Unsaved-changes confirmation (#6 close / #7 refresh) */}
            {pendingDiscard && (
              <div className="absolute inset-0 z-20 flex items-center justify-center bg-background/70">
                <div className="w-[360px] rounded-md border bg-card p-4 shadow-lg">
                  <div className="mb-1 flex items-center gap-2 text-sm font-semibold">
                    <TriangleAlert className="h-4 w-4 text-amber-500" /> Discard unsaved changes?
                  </div>
                  <p className="mb-3 text-xs text-muted-foreground">
                    You have unsaved edits to the model catalog. {pendingDiscard === 'close' ? 'Closing' : 'Refreshing'}{' '}
                    will discard them.
                  </p>
                  <div className="flex justify-end gap-2">
                    <Button variant="outline" size="sm" onClick={() => setPendingDiscard(null)}>
                      Keep editing
                    </Button>
                    <Button variant="destructive" size="sm" onClick={confirmDiscard}>
                      Discard
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t px-6 py-3">
            <span className="truncate text-xs text-destructive">{error}</span>
            <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)} disabled={busy}>
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
