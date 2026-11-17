import { Check, Copy, KeyRound, Loader2, Plus, RefreshCw, ShieldCheck, Trash2, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/**
 * API Keys screen (CTR-0284, FEAT-0088, PRP-0253, UDR-0234).
 *
 * Same frame as the Skills screen (CTR-0124): an icon-only launcher in the sidebar footer
 * opens a ~90% dialog with a key list on the left and the selected key on the right.
 *
 * - The launcher is self-probing: it renders only when GET /api/api-keys answers 200, so a
 *   `responses` / `api` caller or an anonymous LAN visitor never sees it (UDR-0234 D7).
 * - A secret appears ONCE, in the panel shown after Issue or Reissue; closing that panel
 *   drops it from memory. The list never has it (D4).
 * - Name, scope, expiry and the enabled switch are editable. The secret is reissue-only (D5).
 * - Issue, Save, Reissue, Enable/Disable and Delete show a spinner on the pressed button and
 *   lock every action until the server answers. Reissue and Delete confirm inside the pane.
 * - PRP-0254 (UDR-0235 D5): each key may carry /v1/responses limits (requests per minute, day,
 *   month; total tokens per day, month), edited with the other fields and saved by Save. The
 *   left pane shows today's and this month's global usage against the App Settings limits.
 * - PRP-0256 (UDR-0237 D1/D8): a key may be bound to a custom agent (its tools, knowledge scope
 *   and grounding) and may store its conversations as temporary. Both are saved by Save.
 */

type Scope = 'responses' | 'api' | 'admin'

interface ApiKey {
  id: string
  name: string
  scope: Scope
  prefix: string
  enabled: boolean
  created_at: string
  updated_at: string
  expires_at: string | null
  last_used_at: string | null
  created_by: string
  expired: boolean
  limits: KeyLimits | null
  usage?: KeyUsage
  agent?: string | null
  storage?: Storage
}

type Storage = 'saved' | 'temporary'

interface BindableAgent {
  id: string
  name: string
  ready: boolean
  grounding: string
}

const LIMIT_FIELDS = [
  { key: 'requests_per_minute', label: 'Requests per minute' },
  { key: 'requests_per_day', label: 'Requests per day' },
  { key: 'requests_per_month', label: 'Requests per month' },
  { key: 'total_tokens_per_day', label: 'Total tokens per day' },
  { key: 'total_tokens_per_month', label: 'Total tokens per month' },
] as const

type LimitField = (typeof LIMIT_FIELDS)[number]['key']
type KeyLimits = Partial<Record<LimitField, number>>
type LimitForm = Record<LimitField, string>

interface KeyUsage {
  requests_today: number
  requests_month: number
  total_tokens_today: number
  total_tokens_month: number
}

interface ResponsesLimits {
  limits: Record<string, number>
  usage: KeyUsage & Record<string, number>
  timezone: string
  in_flight: number
}

interface AuthStatus {
  app_require_auth_on_lan: boolean
  web_auth_enabled: boolean
  store_path_configured: boolean
}

interface ListResponse {
  keys: ApiKey[]
  auth: AuthStatus
  caller: { source: string; scope: Scope; key_id: string | null }
  responses_limits?: ResponsesLimits
  agents?: BindableAgent[]
}

type Pending = 'issue' | 'save' | 'reissue' | 'toggle' | 'delete' | null
type Confirm = 'reissue' | 'delete' | null
type ExpiryChoice = 'keep' | 'none' | '30' | '90' | '365' | 'date'

const NEW_KEY = '__new__'
const SCOPE_RANK: Record<Scope, number> = { responses: 0, api: 1, admin: 2 }
const SCOPES: { value: Scope; label: string; help: string }[] = [
  {
    value: 'responses',
    label: 'Responses',
    help: 'POST /v1/responses only. For a company site server or an external app.',
  },
  { value: 'api', label: 'API', help: 'Every API except key management. For the CLI and automation.' },
  { value: 'admin', label: 'Admin', help: 'Every API, including managing keys. Give it only where that is needed.' },
]
const PENDING_LABEL: Record<Exclude<Pending, null>, string> = {
  issue: 'Issuing...',
  save: 'Saving...',
  reissue: 'Reissuing...',
  toggle: 'Updating...',
  delete: 'Deleting...',
}

const EMPTY_LIMITS: LimitForm = {
  requests_per_minute: '',
  requests_per_day: '',
  requests_per_month: '',
  total_tokens_per_day: '',
  total_tokens_per_month: '',
}

function limitsToForm(limits: KeyLimits | null | undefined): LimitForm {
  const form = { ...EMPTY_LIMITS }
  for (const { key } of LIMIT_FIELDS) {
    const value = limits?.[key]
    if (value) form[key] = String(value)
  }
  return form
}

/** The non-zero limits of the form, or null when none is set (0 / empty = no limit). */
function formToLimits(form: LimitForm): KeyLimits | null {
  const out: KeyLimits = {}
  for (const { key } of LIMIT_FIELDS) {
    const value = Number(form[key])
    if (form[key].trim() !== '' && Number.isInteger(value) && value > 0) out[key] = value
  }
  return Object.keys(out).length ? out : null
}

function limitsValid(form: LimitForm): boolean {
  return LIMIT_FIELDS.every(({ key }) => {
    const raw = form[key].trim()
    return raw === '' || (/^\d+$/.test(raw) && Number(raw) <= 1_000_000_000_000)
  })
}

function sameLimits(a: KeyLimits | null, b: KeyLimits | null | undefined): boolean {
  return JSON.stringify(a ?? {}) === JSON.stringify(formToLimits(limitsToForm(b)) ?? {})
}

function usedOf(used: number, limit: number | undefined): string {
  const n = used.toLocaleString()
  return limit ? `${n} / ${limit.toLocaleString()}` : n
}

function formatDate(value: string | null): string {
  if (!value) return ''
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

function expiryState(key: ApiKey): { text: string; tone: 'muted' | 'warn' | 'bad' } {
  if (!key.expires_at) return { text: 'No expiry', tone: 'muted' }
  if (key.expired) return { text: 'Expired', tone: 'bad' }
  const days = Math.ceil((new Date(key.expires_at).getTime() - Date.now()) / 86_400_000)
  if (days <= 14) return { text: `Expires in ${days} day${days === 1 ? '' : 's'}`, tone: 'warn' }
  return { text: `Expires ${new Date(key.expires_at).toLocaleDateString()}`, tone: 'muted' }
}

function expiryToIso(choice: ExpiryChoice, date: string): string | null | undefined {
  if (choice === 'keep') return undefined
  if (choice === 'none') return null
  if (choice === 'date') return date ? new Date(`${date}T23:59:59`).toISOString() : undefined
  return new Date(Date.now() + Number(choice) * 86_400_000).toISOString()
}

async function errorText(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') return body.detail
  } catch {
    // fall through
  }
  return `Request failed (${res.status})`
}

export function ApiKeysManager() {
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [data, setData] = useState<ListResponse | null>(null)
  const [selected, setSelected] = useState<string>(NEW_KEY)
  const [name, setName] = useState('')
  const [scope, setScope] = useState<Scope>('responses')
  const [expiry, setExpiry] = useState<ExpiryChoice>('none')
  const [expiryDate, setExpiryDate] = useState('')
  const [limitForm, setLimitForm] = useState<LimitForm>(EMPTY_LIMITS)
  const [agent, setAgent] = useState('')
  const [storage, setStorage] = useState<Storage>('saved')
  const [pending, setPending] = useState<Pending>(null)
  const [confirm, setConfirm] = useState<Confirm>(null)
  const [error, setError] = useState<string | null>(null)
  const [secret, setSecret] = useState<{ name: string; value: string } | null>(null)
  const [copied, setCopied] = useState(false)

  // Self-probe (UDR-0234 D7): only a caller allowed to manage keys sees the launcher.
  useEffect(() => {
    let cancelled = false
    fetch('/api/api-keys')
      .then((res) => {
        if (!cancelled) setAvailable(res.ok)
      })
      .catch(() => {
        if (!cancelled) setAvailable(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const keys = data?.keys ?? []
  const current = useMemo(() => keys.find((k) => k.id === selected) ?? null, [keys, selected])
  const callerRank = SCOPE_RANK[data?.caller.scope ?? 'admin']

  const resetForm = useCallback((key: ApiKey | null) => {
    setName(key?.name ?? '')
    setScope(key?.scope ?? 'responses')
    setExpiry(key ? 'keep' : 'none')
    setExpiryDate('')
    setLimitForm(limitsToForm(key?.limits))
    setAgent(key?.agent ?? '')
    setStorage(key?.storage ?? 'saved')
    setConfirm(null)
    setError(null)
  }, [])

  const load = useCallback(async (): Promise<ListResponse | null> => {
    const res = await fetch('/api/api-keys')
    if (!res.ok) {
      setError(await errorText(res))
      return null
    }
    const body = (await res.json()) as ListResponse
    setData(body)
    return body
  }, [])

  const openModal = useCallback(async () => {
    setOpen(true)
    setLoading(true)
    setSecret(null)
    try {
      await load()
      setSelected(NEW_KEY)
      resetForm(null)
    } finally {
      setLoading(false)
    }
  }, [load, resetForm])

  const select = useCallback(
    (id: string) => {
      if (pending) return
      setSelected(id)
      resetForm(keys.find((k) => k.id === id) ?? null)
    },
    [keys, pending, resetForm],
  )

  const run = useCallback(
    async (kind: Exclude<Pending, null>, request: () => Promise<Response>, after: (body: unknown) => string | null) => {
      setPending(kind)
      setError(null)
      try {
        const res = await request()
        if (!res.ok) {
          setError(await errorText(res))
          return
        }
        const body = res.status === 204 ? null : await res.json()
        const nextSelected = after(body)
        const listed = await load()
        const target = nextSelected ?? NEW_KEY
        setSelected(target)
        resetForm(listed?.keys.find((k) => k.id === target) ?? null)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setPending(null)
        setConfirm(null)
      }
    },
    [load, resetForm],
  )

  const issue = () => {
    const expires = expiryToIso(expiry, expiryDate)
    void run(
      'issue',
      () =>
        fetch('/api/api-keys', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name,
            scope,
            expires_at: expires ?? null,
            limits: formToLimits(limitForm),
            agent: agent || null,
            storage,
          }),
        }),
      (body) => {
        const b = body as { key: ApiKey; secret: string }
        setSecret({ name: b.key.name, value: b.secret })
        setCopied(false)
        return b.key.id
      },
    )
  }

  const save = () => {
    if (!current) return
    const patch: Record<string, unknown> = {}
    if (name !== current.name) patch.name = name
    if (scope !== current.scope) patch.scope = scope
    const expires = expiryToIso(expiry, expiryDate)
    if (expires !== undefined) patch.expires_at = expires
    const limits = formToLimits(limitForm)
    if (!sameLimits(limits, current.limits)) patch.limits = limits
    if (agent !== (current.agent ?? '')) patch.agent = agent || null
    if (storage !== (current.storage ?? 'saved')) patch.storage = storage
    void run(
      'save',
      () =>
        fetch(`/api/api-keys/${encodeURIComponent(current.id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch),
        }),
      () => current.id,
    )
  }

  const toggle = () => {
    if (!current) return
    void run(
      'toggle',
      () =>
        fetch(`/api/api-keys/${encodeURIComponent(current.id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: !current.enabled }),
        }),
      () => current.id,
    )
  }

  const reissue = () => {
    if (!current) return
    void run(
      'reissue',
      () => fetch(`/api/api-keys/${encodeURIComponent(current.id)}/reissue`, { method: 'POST' }),
      (body) => {
        const b = body as { key: ApiKey; secret: string }
        setSecret({ name: b.key.name, value: b.secret })
        setCopied(false)
        return b.key.id
      },
    )
  }

  const remove = () => {
    if (!current) return
    void run(
      'delete',
      () => fetch(`/api/api-keys/${encodeURIComponent(current.id)}`, { method: 'DELETE' }),
      () => null,
    )
  }

  const copySecret = async () => {
    if (!secret) return
    try {
      await navigator.clipboard.writeText(secret.value)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const handleOpenChange = (next: boolean) => {
    if (!next && pending) return
    setOpen(next)
    if (!next) setSecret(null)
  }

  if (!available) return null

  const busy = pending !== null
  const isNew = selected === NEW_KEY
  const nameValid = name.trim().length > 0 && name.trim().length <= 64
  const expiryValid = expiry !== 'date' || expiryDate !== ''
  const limitsOk = limitsValid(limitForm)
  const dirty =
    current !== null &&
    (name !== current.name ||
      scope !== current.scope ||
      expiryToIso(expiry, expiryDate) !== undefined ||
      !sameLimits(formToLimits(limitForm), current.limits) ||
      agent !== (current.agent ?? '') ||
      storage !== (current.storage ?? 'saved'))
  const globalLimits = data?.responses_limits
  const agents = data?.agents ?? []
  const boundAgent = agents.find((a) => a.id === agent)

  const actionButton = (
    kind: Exclude<Pending, null>,
    label: string,
    icon: React.ReactNode,
    onClick: () => void,
    props: { variant?: 'default' | 'outline' | 'destructive'; disabled?: boolean } = {},
  ) => (
    <Button
      type="button"
      size="sm"
      variant={props.variant ?? 'outline'}
      disabled={busy || props.disabled}
      onClick={onClick}
      aria-busy={pending === kind}
      data-pending={pending === kind ? 'true' : undefined}>
      {pending === kind ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : icon}
      {pending === kind ? PENDING_LABEL[kind] : label}
    </Button>
  )

  return (
    <>
      <button
        type="button"
        onClick={() => void openModal()}
        aria-label="API Keys"
        title="API Keys"
        className={cn(
          'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md transition-colors',
          'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
        )}>
        <KeyRound className="h-4 w-4 shrink-0" />
      </button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle>API Keys</DialogTitle>
            <DialogDescription>
              Issue a key per caller, choose what it may call, and set when it expires. A secret is shown once, when it
              is issued or reissued; only its hash is stored.
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <>
                <div className="flex w-72 shrink-0 flex-col border-r">
                  <div className="border-b p-3">
                    <Button type="button" size="sm" className="w-full" disabled={busy} onClick={() => select(NEW_KEY)}>
                      <Plus className="mr-1.5 h-3.5 w-3.5" /> New key
                    </Button>
                  </div>
                  <div className="min-h-0 flex-1 overflow-y-auto" data-testid="api-key-list">
                    {keys.length === 0 && <p className="px-3 py-4 text-xs text-muted-foreground">No keys yet.</p>}
                    {keys.map((k) => {
                      const state = expiryState(k)
                      return (
                        <button
                          key={k.id}
                          type="button"
                          onClick={() => select(k.id)}
                          className={cn(
                            'flex w-full flex-col items-start gap-0.5 border-b px-3 py-2 text-left',
                            selected === k.id && 'bg-accent',
                          )}>
                          <span className="flex w-full items-center gap-1.5">
                            <span className="truncate text-sm font-medium">{k.name}</span>
                            <span className="ml-auto rounded border px-1 text-[10px] uppercase text-muted-foreground">
                              {k.scope}
                            </span>
                          </span>
                          <span className="font-mono text-[11px] text-muted-foreground">{k.prefix}...</span>
                          <span className="flex items-center gap-2 text-[11px]">
                            {!k.enabled && <span className="text-muted-foreground">Disabled</span>}
                            <span
                              className={cn(
                                state.tone === 'muted' && 'text-muted-foreground',
                                state.tone === 'warn' && 'text-amber-600 dark:text-amber-500',
                                state.tone === 'bad' && 'text-destructive',
                              )}>
                              {state.text}
                            </span>
                          </span>
                          <span className="text-[11px] text-muted-foreground">
                            {k.last_used_at ? `Last used ${formatDate(k.last_used_at)}` : 'Never used'}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                  {globalLimits && (
                    <div
                      className="space-y-0.5 border-t px-3 py-2 text-[11px] text-muted-foreground"
                      data-testid="responses-usage">
                      <p className="font-medium text-foreground">/v1/responses usage ({globalLimits.timezone})</p>
                      <p>
                        Today: {usedOf(globalLimits.usage.requests_today, globalLimits.limits.requests_per_day)}{' '}
                        requests,{' '}
                        {usedOf(globalLimits.usage.total_tokens_today, globalLimits.limits.total_tokens_per_day)} tokens
                      </p>
                      <p>
                        This month: {usedOf(globalLimits.usage.requests_month, globalLimits.limits.requests_per_month)}{' '}
                        requests,{' '}
                        {usedOf(globalLimits.usage.total_tokens_month, globalLimits.limits.total_tokens_per_month)}{' '}
                        tokens
                      </p>
                      <p>Global limits are set in App Settings.</p>
                    </div>
                  )}
                  {data && (
                    <div
                      className="space-y-1 border-t px-3 py-2 text-[11px] text-muted-foreground"
                      data-testid="auth-status">
                      <p className="flex items-center gap-1.5">
                        <ShieldCheck className="h-3.5 w-3.5" />
                        APP_REQUIRE_AUTH_ON_LAN: {data.auth.app_require_auth_on_lan ? 'on' : 'off'}
                      </p>
                      <p>Web sign-in: {data.auth.web_auth_enabled ? 'on' : 'off'}</p>
                      <p>Requests from this machine pass without a key.</p>
                    </div>
                  )}
                </div>

                <div className="min-w-0 flex-1 overflow-y-auto p-6">
                  {secret && (
                    <div
                      className="mb-6 rounded-md border border-amber-500/50 bg-amber-500/10 p-4"
                      data-testid="api-key-secret">
                      <p className="mb-2 flex items-center gap-1.5 text-sm font-medium">
                        <TriangleAlert className="h-4 w-4 text-amber-600" /> Copy it now. It will not be shown again.
                      </p>
                      <p className="mb-2 text-xs text-muted-foreground">Secret for {secret.name}</p>
                      <div className="flex items-center gap-2">
                        <code className="min-w-0 flex-1 break-all rounded bg-muted px-2 py-1.5 font-mono text-xs">
                          {secret.value}
                        </code>
                        <Button type="button" size="sm" variant="outline" onClick={() => void copySecret()}>
                          {copied ? <Check className="mr-1.5 h-3.5 w-3.5" /> : <Copy className="mr-1.5 h-3.5 w-3.5" />}
                          {copied ? 'Copied' : 'Copy'}
                        </Button>
                        <Button type="button" size="sm" variant="ghost" onClick={() => setSecret(null)}>
                          Done
                        </Button>
                      </div>
                    </div>
                  )}

                  <h3 className="mb-4 text-base font-semibold">{isNew ? 'New key' : current?.name}</h3>
                  {!isNew && current && (
                    <dl className="mb-4 grid grid-cols-[8rem_1fr] gap-y-1 text-xs">
                      <dt className="text-muted-foreground">Key</dt>
                      <dd className="font-mono">{current.prefix}...</dd>
                      <dt className="text-muted-foreground">Created</dt>
                      <dd>
                        {formatDate(current.created_at)}
                        {current.created_by ? ` by ${current.created_by}` : ''}
                      </dd>
                      <dt className="text-muted-foreground">Last used</dt>
                      <dd>{current.last_used_at ? formatDate(current.last_used_at) : 'Never'}</dd>
                    </dl>
                  )}

                  <div className="max-w-2xl space-y-5">
                    <div className="space-y-1.5">
                      <label htmlFor="api-key-name" className="text-sm font-medium">
                        Name
                      </label>
                      <Input
                        id="api-key-name"
                        value={name}
                        maxLength={64}
                        disabled={busy}
                        placeholder="company-site-bff"
                        onChange={(e) => setName(e.target.value)}
                      />
                    </div>

                    <fieldset className="space-y-1.5" disabled={busy}>
                      <legend className="text-sm font-medium">Scope</legend>
                      <div className="grid gap-2 sm:grid-cols-3">
                        {SCOPES.map((s) => {
                          const allowed = SCOPE_RANK[s.value] <= callerRank
                          return (
                            <label
                              key={s.value}
                              className={cn(
                                'flex cursor-pointer flex-col gap-1 rounded-md border p-3 text-left',
                                scope === s.value && 'border-primary bg-accent',
                                !allowed && 'cursor-not-allowed opacity-50',
                              )}>
                              <span className="flex items-center gap-2 text-sm font-medium">
                                <input
                                  type="radio"
                                  name="api-key-scope"
                                  value={s.value}
                                  checked={scope === s.value}
                                  disabled={!allowed}
                                  onChange={() => setScope(s.value)}
                                />
                                {s.label}
                              </span>
                              <span className="text-xs text-muted-foreground">{s.help}</span>
                            </label>
                          )
                        })}
                      </div>
                    </fieldset>

                    <div className="space-y-1.5">
                      <label htmlFor="api-key-expiry" className="text-sm font-medium">
                        Expiry
                      </label>
                      <div className="flex flex-wrap items-center gap-2">
                        <select
                          id="api-key-expiry"
                          className="h-9 rounded-md border bg-background px-2 text-sm"
                          value={expiry}
                          disabled={busy}
                          onChange={(e) => setExpiry(e.target.value as ExpiryChoice)}>
                          {!isNew && (
                            <option value="keep">
                              {current?.expires_at ? `Keep (${formatDate(current.expires_at)})` : 'Keep (no expiry)'}
                            </option>
                          )}
                          <option value="none">No expiry</option>
                          <option value="30">30 days</option>
                          <option value="90">90 days</option>
                          <option value="365">365 days</option>
                          <option value="date">On a date</option>
                        </select>
                        {expiry === 'date' && (
                          <Input
                            type="date"
                            className="w-44"
                            value={expiryDate}
                            disabled={busy}
                            aria-label="Expiry date"
                            onChange={(e) => setExpiryDate(e.target.value)}
                          />
                        )}
                      </div>
                    </div>

                    <fieldset className="space-y-1.5" disabled={busy} data-testid="api-key-limits">
                      <legend className="text-sm font-medium">Limits for /v1/responses</legend>
                      <p className="text-xs text-muted-foreground">
                        Empty or 0 = no limit for this key. The global limits in App Settings apply as well; the
                        stricter one refuses first (429).
                      </p>
                      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        {LIMIT_FIELDS.map((f) => (
                          <div key={f.key} className="space-y-1">
                            <label htmlFor={`api-key-limit-${f.key}`} className="text-xs text-muted-foreground">
                              {f.label}
                            </label>
                            <Input
                              id={`api-key-limit-${f.key}`}
                              inputMode="numeric"
                              value={limitForm[f.key]}
                              placeholder="No limit"
                              onChange={(e) => setLimitForm((prev) => ({ ...prev, [f.key]: e.target.value }))}
                            />
                          </div>
                        ))}
                      </div>
                      {!isNew && current?.usage && (
                        <p className="text-xs text-muted-foreground" data-testid="api-key-usage">
                          Used today: {current.usage.requests_today.toLocaleString()} requests,{' '}
                          {current.usage.total_tokens_today.toLocaleString()} tokens. This month:{' '}
                          {current.usage.requests_month.toLocaleString()} requests,{' '}
                          {current.usage.total_tokens_month.toLocaleString()} tokens.
                        </p>
                      )}
                      {!limitsOk && <p className="text-xs text-destructive">Use whole numbers only.</p>}
                    </fieldset>

                    <fieldset className="space-y-3" disabled={busy} data-testid="api-key-binding">
                      <legend className="text-sm font-medium">Agent and conversations for /v1/responses</legend>
                      <div className="space-y-1">
                        <label htmlFor="api-key-agent" className="text-xs text-muted-foreground">
                          Agent
                        </label>
                        <select
                          id="api-key-agent"
                          className="h-9 w-full max-w-md rounded-md border bg-background px-2 text-sm"
                          value={agent}
                          onChange={(e) => setAgent(e.target.value)}>
                          <option value="">None (the request chooses the model)</option>
                          {agents.map((a) => (
                            <option key={a.id} value={a.id}>
                              {a.name}
                              {a.grounding === 'strict' ? ' (documents only)' : ''}
                              {a.ready ? '' : ' (has problems)'}
                            </option>
                          ))}
                          {agent && !boundAgent && <option value={agent}>{agent} (not found)</option>}
                        </select>
                        <p className="text-xs text-muted-foreground">
                          A key bound to an agent runs only that agent, with its tools and documents. Send model
                          &quot;chatwalaau&quot;.
                        </p>
                        {boundAgent && !boundAgent.ready && (
                          <p className="text-xs text-destructive">
                            This agent has problems. Requests get 503 until it is fixed.
                          </p>
                        )}
                      </div>
                      <div className="space-y-1">
                        <label htmlFor="api-key-storage" className="text-xs text-muted-foreground">
                          Conversations
                        </label>
                        <select
                          id="api-key-storage"
                          className="h-9 w-full max-w-md rounded-md border bg-background px-2 text-sm"
                          value={storage}
                          onChange={(e) => setStorage(e.target.value as Storage)}>
                          <option value="saved">Saved (listed in the chat history)</option>
                          <option value="temporary">Temporary (not listed, deleted after the retention period)</option>
                        </select>
                      </div>
                    </fieldset>

                    {error && (
                      <p className="flex items-center gap-1.5 text-sm text-destructive" role="alert">
                        <TriangleAlert className="h-4 w-4" /> {error}
                      </p>
                    )}

                    {isNew ? (
                      <div className="flex gap-2">
                        {actionButton('issue', 'Issue', <KeyRound className="mr-1.5 h-3.5 w-3.5" />, issue, {
                          variant: 'default',
                          disabled: !nameValid || !expiryValid || !limitsOk,
                        })}
                      </div>
                    ) : current ? (
                      <div className="space-y-4">
                        <p className="text-xs text-muted-foreground">
                          Issued secrets cannot be shown. Reissue to get a new one.
                        </p>
                        <div className="flex flex-wrap gap-2">
                          {actionButton('save', 'Save', <Check className="mr-1.5 h-3.5 w-3.5" />, save, {
                            variant: 'default',
                            disabled: !dirty || !nameValid || !expiryValid || !limitsOk,
                          })}
                          {actionButton(
                            'toggle',
                            current.enabled ? 'Disable' : 'Enable',
                            <ShieldCheck className="mr-1.5 h-3.5 w-3.5" />,
                            toggle,
                          )}
                          {actionButton('reissue', 'Reissue', <RefreshCw className="mr-1.5 h-3.5 w-3.5" />, () =>
                            setConfirm('reissue'),
                          )}
                          {actionButton(
                            'delete',
                            'Delete',
                            <Trash2 className="mr-1.5 h-3.5 w-3.5" />,
                            () => setConfirm('delete'),
                            { variant: 'destructive' },
                          )}
                        </div>
                        {confirm && (
                          <div className="rounded-md border p-3 text-sm" data-testid="api-key-confirm">
                            <p className="mb-2">
                              {confirm === 'reissue'
                                ? `Reissue ${current.name}? The current secret stops working at once.`
                                : `Delete ${current.name}? Callers using it get 401 at once.`}
                            </p>
                            <div className="flex gap-2">
                              {confirm === 'reissue'
                                ? actionButton(
                                    'reissue',
                                    'Reissue now',
                                    <RefreshCw className="mr-1.5 h-3.5 w-3.5" />,
                                    reissue,
                                    {
                                      variant: 'default',
                                    },
                                  )
                                : actionButton(
                                    'delete',
                                    'Delete now',
                                    <Trash2 className="mr-1.5 h-3.5 w-3.5" />,
                                    remove,
                                    {
                                      variant: 'destructive',
                                    },
                                  )}
                              <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                disabled={busy}
                                onClick={() => setConfirm(null)}>
                                Cancel
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>
                    ) : null}
                  </div>
                </div>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
