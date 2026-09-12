/**
 * Application Settings panel (CTR-0176 v7, PRP-0136, UDR-0120 D8).
 *
 * Renders one settings GROUP entirely from the descriptors the backend advertises
 * at GET /api/app-settings. Nothing here knows the name of any individual
 * setting: `type` and `enum` choose the control, `label` / `help` supply the
 * text, `scope` drives the badge. That is the whole point -- adding a key in a
 * later release must be a backend-only change, the property `role_registry`
 * (UDR-0096 D6) already established for task-model rows.
 *
 * Two behaviours are load-bearing rather than cosmetic:
 *
 * - The save sends the FULL document including `unknown`, the bag of keys this
 *   build does not recognise (UDR-0120 D5). A full-document PUT that forgets a
 *   field it does not edit silently destroys it -- the defect UDR-0093 D4 found
 *   in `auth_profiles`. The server also defends against this (an omitted
 *   `unknown` preserves what is on disk), but the client sends it explicitly so
 *   the per-key delete control below can actually delete.
 * - `scope: restart` values are persisted and then reported back as
 *   `restart_required`. ChatWalaʻau never restarts itself (UDR-0120 D6), so the
 *   panel tells the operator which keys are waiting on them.
 */

import { Loader2, RefreshCw, RotateCcw, TriangleAlert, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

export type SettingScope = 'runtime' | 'rebuild' | 'restart'

export interface SettingDescriptor {
  key: string
  env_name: string
  label: string
  group: string
  type: 'bool' | 'int' | 'str' | 'enum'
  // Declared bounds for an `int` key (PRP-0163 / UDR-0141 D4). Advisory here:
  // the server enforces them regardless, because the store is also written by
  // the CLI and by hand.
  min?: number | null
  max?: number | null
  enum: string[] | null
  scope: SettingScope
  requires_restart: boolean
  help: string
  default: unknown
  deprecated: boolean
  // The descriptor whose value decides whether this row is ACTIVE, and the
  // parent value that makes it so (PRP-0164 / UDR-0142 D1). Optional because an
  // older backend does not send them; absent means the row has no parent and is
  // always active, which is the behaviour that predates this field.
  parent?: string | null
  enabled_when?: unknown
}

export interface SettingGroup {
  key: string
  label: string
  description: string
}

export interface AppSettingsStatus {
  schema_version: number
  settings: Record<string, unknown>
  descriptors: SettingDescriptor[]
  groups: SettingGroup[]
  unknown: Record<string, unknown>
  warnings: string[]
  residual_env_keys: string[]
  path: string | null
  present: boolean
  valid: boolean
  error: string | null
  demo_mode: boolean
  restart_required?: boolean
  restart_required_keys?: string[]
}

const SCOPE_BADGE: Record<SettingScope, { label: string; title: string; className: string }> = {
  runtime: {
    label: 'Applies immediately',
    title: 'Saving applies this value right away; no restart or rebuild is needed.',
    className: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  },
  rebuild: {
    label: 'Rebuilds agents',
    title: 'Saving rebuilds the per-model agents in place. No restart is needed.',
    className: 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-400',
  },
  restart: {
    label: 'Restart required',
    title: 'The value is saved now, but the server must be restarted before it takes effect.',
    className: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  },
}

/**
 * The nearest ancestor currently switching a row OFF, or null when the row is
 * active. The TypeScript mirror of `descriptors.is_active` (UDR-0142 D1),
 * resolved over the whole ancestor chain and returning the responsible link
 * rather than a bare boolean -- an operator told only that a control is
 * unavailable has been told the half they already knew.
 *
 * It reads FORM state, not the persisted snapshot, and that is the reason it
 * lives here rather than arriving as a computed flag on the descriptor (D3):
 * the operator expects a child to grey out the instant the parent toggle moves,
 * which is before any save, and a server-derived flag would be wrong for exactly
 * that interval.
 */
function blockingAncestor(
  descriptor: SettingDescriptor,
  values: Record<string, unknown>,
  byKey: Map<string, SettingDescriptor>,
): SettingDescriptor | null {
  const seen = new Set<string>([descriptor.key])
  let current: SettingDescriptor | undefined = descriptor

  while (current?.parent) {
    const parent = byKey.get(current.parent)
    // An unknown parent cannot ship (invariant I1). If one ever does, leave the
    // row usable rather than greying it out for a reason nothing on screen can
    // explain.
    if (!parent) return null
    const effective = current.parent in values ? values[current.parent] : parent.default
    // An enum parent may carry string options for an int-typed field (the audio
    // rates), so compare enum links as text; a bool parent compares exactly.
    const matched =
      parent.type === 'enum' ? String(effective) === String(current.enabled_when) : effective === current.enabled_when
    if (!matched) return parent
    // Cycles cannot ship either (invariant I2); refuse to spin if one ever does.
    if (seen.has(parent.key)) return null
    seen.add(parent.key)
    current = parent
  }

  return null
}

/**
 * Whether a row carries an unresolved issue, which un-greys it regardless of its
 * parent (UDR-0142 D4).
 *
 * Without this, `2K < N` plus an unconditional grey-out is a trap: the server
 * rejects the document, the message names two controls, and both are disabled
 * because the parent they hang off is switched off. Two sources, matching the
 * two rules that can produce one -- a declared bound (UDR-0141 D4), checked
 * locally, and a cross-field rule (D5), which lives only on the server and is
 * therefore recognised by the key it NAMES in the rejection.
 */
function hasUnresolvedIssue(descriptor: SettingDescriptor, value: unknown, saveError: string | null): boolean {
  if (typeof value === 'number') {
    if (descriptor.min != null && value < descriptor.min) return true
    if (descriptor.max != null && value > descriptor.max) return true
  }
  return Boolean(saveError?.includes(descriptor.env_name))
}

function ScopeBadge({ scope }: { scope: SettingScope }) {
  const meta = SCOPE_BADGE[scope]
  return (
    <span
      title={meta.title}
      className={cn('shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium', meta.className)}>
      {meta.label}
    </span>
  )
}

/** Render one descriptor as the control its `type` calls for. */
function SettingRow({
  descriptor,
  value,
  onChange,
  disabled,
  inactiveBecause,
}: {
  descriptor: SettingDescriptor
  value: unknown
  onChange: (key: string, value: unknown) => void
  disabled: boolean
  /**
   * Label of the parent that is currently switching this row off, or null when
   * the row is active. An inactive row stays VISIBLE and stays in the document
   * (UDR-0142 D4) -- hiding it would leave an operator without the one thing
   * they need, which is why their value is not taking effect.
   */
  inactiveBecause?: string | null
}) {
  const isDefault = JSON.stringify(value) === JSON.stringify(descriptor.default)

  const control = () => {
    if (descriptor.type === 'bool') {
      return (
        <input
          type="checkbox"
          className="mt-1 h-4 w-4"
          checked={Boolean(value)}
          disabled={disabled}
          aria-label={descriptor.label}
          onChange={(e) => onChange(descriptor.key, e.target.checked)}
        />
      )
    }
    if (descriptor.type === 'enum' && descriptor.enum) {
      return (
        <select
          className="h-8 w-56 rounded-md border bg-background px-2 text-xs"
          value={String(value ?? '')}
          disabled={disabled}
          aria-label={descriptor.label}
          onChange={(e) => {
            // An int-typed enum (the audio rates) still carries string options,
            // so coerce back before it leaves the control -- the server would
            // coerce it anyway, but a matching local type keeps the dirty check
            // and the "is default" marker honest.
            const raw = e.target.value
            onChange(descriptor.key, typeof descriptor.default === 'number' ? Number(raw) : raw)
          }}>
          {descriptor.enum.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      )
    }
    if (descriptor.type === 'int') {
      return (
        <Input
          type="number"
          className="h-8 w-56 text-xs"
          value={value === null || value === undefined ? '' : String(value)}
          min={descriptor.min ?? undefined}
          max={descriptor.max ?? undefined}
          disabled={disabled}
          aria-label={descriptor.label}
          onChange={(e) => {
            const raw = e.target.value
            onChange(descriptor.key, raw === '' ? descriptor.default : Number(raw))
          }}
        />
      )
    }
    return (
      <Input
        className="h-8 w-56 text-xs"
        value={value === null || value === undefined ? '' : String(value)}
        disabled={disabled}
        aria-label={descriptor.label}
        onChange={(e) => onChange(descriptor.key, e.target.value)}
      />
    )
  }

  return (
    <div className="flex items-start justify-between gap-4 border-b py-2.5 last:border-b-0">
      <div className={cn('min-w-0 flex-1', inactiveBecause && 'opacity-60')}>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{descriptor.label}</span>
          <ScopeBadge scope={descriptor.scope} />
          {inactiveBecause && (
            <span
              title={`This value is not read while "${inactiveBecause}" is off. It is still stored, so turning that back on restores it.`}
              className="shrink-0 rounded border border-muted-foreground/30 bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
              Needs {inactiveBecause}
            </span>
          )}
          {!isDefault && (
            <button
              type="button"
              disabled={disabled}
              onClick={() => onChange(descriptor.key, descriptor.default)}
              title={`Reset to the built-in default (${JSON.stringify(descriptor.default)})`}
              className="inline-flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50">
              <RotateCcw className="h-3 w-3" /> Reset
            </button>
          )}
        </div>
        {descriptor.help && <p className="mt-0.5 text-[11px] text-muted-foreground">{descriptor.help}</p>}
        <p className="mt-0.5 font-mono text-[10px] text-muted-foreground/70">{descriptor.env_name}</p>
      </div>
      <div className="shrink-0 pt-0.5">{control()}</div>
    </div>
  )
}

export function AppSettingsPanel({
  groupKey,
  status,
  onRefresh,
  onSaved,
}: {
  groupKey: string
  status: AppSettingsStatus
  onRefresh: () => void
  onSaved: (next: AppSettingsStatus) => void
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>(status.settings)
  const [unknown, setUnknown] = useState<Record<string, unknown>>(status.unknown)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [restartKeys, setRestartKeys] = useState<string[]>([])

  // Re-seed whenever the server hands us a fresh snapshot (open, refresh, save).
  useEffect(() => {
    setDraft(status.settings)
    setUnknown(status.unknown)
  }, [status])

  const group = useMemo(() => status.groups.find((g) => g.key === groupKey), [status.groups, groupKey])
  const rows = useMemo(
    () => status.descriptors.filter((d) => d.group === groupKey && !d.deprecated),
    [status.descriptors, groupKey],
  )
  // Indexed over EVERY descriptor, not just this group's rows. An invariant test
  // keeps a parent in the same group as its child, but resolving from the whole
  // registry means a violation would misrender rather than crash here.
  const byKey = useMemo(() => new Map(status.descriptors.map((d) => [d.key, d])), [status.descriptors])

  const dirty = useMemo(
    () =>
      JSON.stringify(draft) !== JSON.stringify(status.settings) ||
      JSON.stringify(unknown) !== JSON.stringify(status.unknown),
    [draft, unknown, status.settings, status.unknown],
  )

  const handleChange = useCallback((key: string, value: unknown) => {
    setDraft((prev) => ({ ...prev, [key]: value }))
  }, [])

  const dropUnknown = useCallback((key: string) => {
    setUnknown((prev) => {
      const next = { ...prev }
      delete next[key]
      return next
    })
  }, [])

  const doSave = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      // Full document: every owned key plus the preserved unknown bag. Sending a
      // subset here is what silently deletes operator data (UDR-0120 D5).
      const res = await fetch('/api/app-settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: draft, unknown }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        setError(detail?.detail?.message ?? `Save failed (HTTP ${res.status})`)
        return
      }
      const next = (await res.json()) as AppSettingsStatus
      setRestartKeys(next.restart_required ? (next.restart_required_keys ?? []) : [])
      onSaved(next)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }, [draft, unknown, onSaved])

  const unknownKeys = Object.keys(unknown)

  return (
    <>
      <div className="flex items-center justify-between gap-2 border-b px-5 py-3">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold">{group?.label ?? groupKey}</h3>
          <p className="truncate text-[11px] text-muted-foreground">
            {status.path ?? 'APP_SETTINGS_FILE not set'}
            {!status.present && ' -- not created yet'}
            {dirty && ' -- unsaved changes'}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onRefresh}
            disabled={busy}
            title="Re-read app_settings.jsonc from the server">
            <RefreshCw className="mr-1 h-3.5 w-3.5" /> Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => void doSave()}
            disabled={busy || !dirty}
            title={dirty ? 'Save every application setting' : 'No changes to save'}>
            Save
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
        {error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-2 text-[12px] text-destructive">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="whitespace-pre-wrap">{error}</span>
          </div>
        )}

        {restartKeys.length > 0 && (
          <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[12px] text-amber-700 dark:text-amber-400">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              Saved. Restart the server for these to take effect: <b>{restartKeys.join(', ')}</b>
            </span>
          </div>
        )}

        {status.residual_env_keys.length > 0 && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
            <div className="mb-1 flex items-center gap-1.5 font-medium">
              <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
              These variables are still set in .env but are no longer read
            </div>
            <p className="mb-1 font-mono">{status.residual_env_keys.join(', ')}</p>
            <p>
              Their values are not in effect. Set them here instead, or run{' '}
              <code>chatwalaau settings migrate --write</code> to copy them across in one step.
            </p>
          </div>
        )}

        {status.warnings.length > 0 && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
            <div className="mb-1 font-medium">Values that fell back to their default:</div>
            <ul className="space-y-0.5 pl-5">
              {status.warnings.map((w) => (
                <li key={w} className="list-disc whitespace-pre-wrap">
                  {w}
                </li>
              ))}
            </ul>
          </div>
        )}

        {group?.description && (
          <p className="rounded-md bg-muted/50 p-2 text-[11px] text-muted-foreground">{group.description}</p>
        )}

        <div>
          {rows.map((d) => {
            // Inactive means "not read", never "not stored" (UDR-0142 D5): the
            // value still travels in the save, is still coerced, and is still
            // spanned by the cross-field rules. Only the control is disabled --
            // and even that yields to an unresolved issue (D4), which is what
            // keeps a rejected document correctable from this screen.
            const blocker = blockingAncestor(d, draft, byKey)
            const escaped = blocker !== null && hasUnresolvedIssue(d, draft[d.key], error)
            return (
              <SettingRow
                key={d.key}
                descriptor={d}
                value={draft[d.key]}
                onChange={handleChange}
                disabled={busy || (blocker !== null && !escaped)}
                inactiveBecause={blocker?.label ?? null}
              />
            )
          })}
          {rows.length === 0 && <p className="text-[11px] text-muted-foreground">No settings in this group.</p>}
        </div>

        {unknownKeys.length > 0 && (
          <section className="space-y-2">
            <h4 className="text-sm font-semibold">Unknown / deprecated settings</h4>
            <p className="text-[11px] text-muted-foreground">
              This build does not recognise these keys, so they are not applied. They are kept on disk rather than
              deleted, so a file written by a newer release still loads here. Remove one only if you are sure it is no
              longer needed.
            </p>
            <div className="rounded-md border">
              {unknownKeys.map((key) => (
                <div key={key} className="flex items-center justify-between gap-2 border-b px-3 py-2 last:border-b-0">
                  <div className="min-w-0">
                    <p className="truncate font-mono text-xs">{key}</p>
                    <p className="truncate text-[11px] text-muted-foreground">{JSON.stringify(unknown[key])}</p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 shrink-0"
                    disabled={busy}
                    onClick={() => dropUnknown(key)}
                    aria-label={`Remove ${key}`}
                    title="Remove this key on the next save">
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
            </div>
          </section>
        )}
      </div>

      {busy && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
          <div className="flex items-center gap-2 text-sm">
            <Loader2 className="h-5 w-5 animate-spin" />
            Applying application settings...
          </div>
        </div>
      )}
    </>
  )
}
