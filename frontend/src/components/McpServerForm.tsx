import { Loader2, Lock, Plus, Trash2 } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/**
 * Add / edit form for one MCP server (CTR-0122 v3, CTR-0278, PRP-0242, UDR-0223).
 *
 * Secrets are never typed here: each env / header row names an environment
 * VARIABLE, and the entry saves `${NAME}` (or `<scheme> ${NAME}` for a header).
 * A literal value written by hand in the file arrives as `{literal: true}` and is
 * kept unchanged when sent back (UDR-0223 D4 / D5). A stdio server runs a command on
 * the backend's machine, so when this request may not edit stdio the stdio fields
 * are locked and the reason names the `.env` switch (UDR-0223 D2).
 */

export type LiteralMarker = { literal: true }
export type SecretValue = string | LiteralMarker

export interface McpServerEntry {
  command?: string
  args?: string[]
  env?: Record<string, SecretValue>
  url?: string
  headers?: Record<string, SecretValue>
  load_tools?: boolean
  load_prompts?: boolean
  request_timeout?: number
  [key: string]: unknown
}

export interface McpServerConfigView {
  servers: { name: string; transport: string; entry: McpServerEntry }[]
  env_status: Record<string, boolean>
  revision: string
  source: string
  config_path: string
  target_path: string
  writable: boolean
  error: string | null
  stdio_editable: boolean
}

type Transport = 'stdio' | 'http'
type Tri = '' | 'true' | 'false'

interface SecretRow {
  key: string
  scheme: string
  variable: string
  literal: boolean
}

const VAR_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/
const SERVER_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/
const REF = /^(?:([A-Za-z][A-Za-z0-9._~+-]*) )?\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/

export const STDIO_LOCK_NOTE =
  'stdio servers run a command on the server machine, so they can be added or edited only on that machine. ' +
  'Set MCP_STDIO_REMOTE_EDIT=true in .env to allow it from elsewhere.'

function toRows(block: Record<string, SecretValue> | undefined): SecretRow[] {
  return Object.entries(block ?? {}).map(([key, value]) => {
    if (typeof value !== 'string') return { key, scheme: '', variable: '', literal: true }
    const m = REF.exec(value)
    return { key, scheme: m?.[1] ?? '', variable: m?.[2] ?? '', literal: false }
  })
}

function fromRows(rows: SecretRow[], withScheme: boolean): Record<string, SecretValue> {
  const out: Record<string, SecretValue> = {}
  for (const r of rows) {
    if (!r.key.trim()) continue
    if (r.literal) out[r.key.trim()] = { literal: true }
    else {
      const ref = `\${${r.variable.trim()}}`
      out[r.key.trim()] = withScheme && r.scheme.trim() ? `${r.scheme.trim()} ${ref}` : ref
    }
  }
  return out
}

function tri(v: boolean | undefined): Tri {
  return v === undefined ? '' : v ? 'true' : 'false'
}

export function detailMessage(body: unknown, fallback: string): string {
  const d = body as { detail?: { message?: string; error?: string } | string } | null
  if (typeof d?.detail === 'string') return d.detail
  return d?.detail?.message || d?.detail?.error || fallback
}

interface Props {
  mode: 'add' | 'edit'
  initialName?: string
  initialEntry?: McpServerEntry
  config: McpServerConfigView
  existingNames: string[]
  onSaved: (name: string) => void
  onCancel: () => void
}

export function McpServerForm({ mode, initialName, initialEntry, config, existingNames, onSaved, onCancel }: Props) {
  const initialTransport: Transport = initialEntry?.url !== undefined && !initialEntry?.command ? 'http' : 'stdio'
  const uid = useId()
  const [name, setName] = useState(initialName ?? '')
  const [transport, setTransport] = useState<Transport>(
    mode === 'add' && !config.stdio_editable ? 'http' : initialTransport,
  )
  const [command, setCommand] = useState(initialEntry?.command ?? '')
  const [argsText, setArgsText] = useState((initialEntry?.args ?? []).join('\n'))
  const [envRows, setEnvRows] = useState<SecretRow[]>(toRows(initialEntry?.env))
  const [url, setUrl] = useState(initialEntry?.url ?? '')
  const [headerRows, setHeaderRows] = useState<SecretRow[]>(toRows(initialEntry?.headers))
  const [loadTools, setLoadTools] = useState<Tri>(tri(initialEntry?.load_tools))
  const [loadPrompts, setLoadPrompts] = useState<Tri>(tri(initialEntry?.load_prompts))
  const [timeout, setTimeoutText] = useState(initialEntry?.request_timeout ? String(initialEntry.request_timeout) : '')
  const [busy, setBusy] = useState<'save' | 'test' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ ok: boolean; tools: { name: string }[]; error: string | null } | null>(
    null,
  )

  // Editing an existing stdio server, or choosing stdio, is locked without permission.
  const editingStdio = mode === 'edit' && initialTransport === 'stdio'
  const stdioLocked = !config.stdio_editable && (transport === 'stdio' || editingStdio)
  const lockedAll = !config.stdio_editable && editingStdio

  const problems = useMemo(() => {
    const out: string[] = []
    if (!SERVER_NAME.test(name))
      out.push('Name: 1-64 letters, digits, "-", "_" or ".", starting with a letter or digit.')
    else if (name !== initialName && existingNames.includes(name)) out.push(`A server named "${name}" already exists.`)
    if (transport === 'stdio' && !command.trim()) out.push('Command is required.')
    if (transport === 'http' && !/^(https?:\/\/|\$\{)/.test(url.trim()))
      out.push('URL must start with http:// or https://.')
    const rows = transport === 'stdio' ? envRows : headerRows
    for (const r of rows) {
      if (!r.key.trim()) out.push('Every row needs a name.')
      else if (!r.literal && !VAR_NAME.test(r.variable.trim()))
        out.push(`"${r.key}": enter an environment variable name (letters, digits, _).`)
    }
    if (timeout && !(Number.isInteger(Number(timeout)) && Number(timeout) > 0))
      out.push('Request timeout must be a positive whole number of seconds.')
    return out
  }, [name, initialName, existingNames, transport, command, url, envRows, headerRows, timeout])

  const buildEntry = (): McpServerEntry => {
    const entry: McpServerEntry = {}
    if (transport === 'stdio') {
      entry.command = command.trim()
      const args = argsText
        .split('\n')
        .map((a) => a.trim())
        .filter(Boolean)
      if (args.length) entry.args = args
      const env = fromRows(envRows, false)
      if (Object.keys(env).length) entry.env = env
    } else {
      entry.url = url.trim()
      const headers = fromRows(headerRows, true)
      if (Object.keys(headers).length) entry.headers = headers
    }
    if (loadTools) entry.load_tools = loadTools === 'true'
    if (loadPrompts) entry.load_prompts = loadPrompts === 'true'
    if (timeout) entry.request_timeout = Number(timeout)
    return entry
  }

  const save = async () => {
    setBusy('save')
    setError(null)
    try {
      const body = JSON.stringify({ name, entry: buildEntry(), revision: config.revision })
      const res =
        mode === 'add'
          ? await fetch('/api/mcp/servers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
          : await fetch(`/api/mcp/servers/${encodeURIComponent(initialName ?? '')}`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body,
            })
      if (!res.ok) throw new Error(detailMessage(await res.json().catch(() => null), 'Failed to save the server'))
      onSaved(name)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save the server')
    } finally {
      setBusy(null)
    }
  }

  const test = async () => {
    setBusy('test')
    setError(null)
    setTestResult(null)
    try {
      const res = await fetch('/api/mcp/servers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: mode === 'edit' ? initialName : null, entry: buildEntry() }),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) throw new Error(detailMessage(body, 'Test failed'))
      setTestResult(body)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Test failed')
    } finally {
      setBusy(null)
    }
  }

  const rows = transport === 'stdio' ? envRows : headerRows
  const setRows = transport === 'stdio' ? setEnvRows : setHeaderRows
  const updateRow = (i: number, patch: Partial<SecretRow>) =>
    setRows((prev) => prev.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  return (
    <div className="space-y-4" data-testid="mcp-server-form">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{mode === 'add' ? 'Add MCP server' : `Edit ${initialName}`}</h3>
      </div>

      {(stdioLocked || (mode === 'add' && !config.stdio_editable)) && (
        <p className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
          <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {STDIO_LOCK_NOTE}
        </p>
      )}

      <fieldset disabled={lockedAll || busy !== null} className="space-y-4">
        <div className="block space-y-1">
          <label htmlFor={`${uid}-name`} className="text-xs font-medium">
            Name
          </label>
          <Input id={`${uid}-name`} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. github" />
        </div>

        <div className="space-y-1">
          <span className="text-xs font-medium">Transport</span>
          <div className="flex gap-4 text-sm">
            {(['stdio', 'http'] as const).map((t) => (
              <label key={t} className="flex items-center gap-1.5">
                <input
                  type="radio"
                  name="mcp-transport"
                  value={t}
                  checked={transport === t}
                  disabled={t === 'stdio' && !config.stdio_editable}
                  onChange={() => setTransport(t)}
                />
                {t === 'stdio' ? 'stdio (local command)' : 'HTTP (remote URL)'}
              </label>
            ))}
          </div>
        </div>

        {transport === 'stdio' ? (
          <>
            <div className="block space-y-1">
              <label htmlFor={`${uid}-command`} className="text-xs font-medium">
                Command
              </label>
              <Input
                id={`${uid}-command`}
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="e.g. npx"
              />
            </div>
            <label className="block space-y-1">
              <span className="text-xs font-medium">Arguments (one per line)</span>
              <textarea
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
                rows={3}
                placeholder={'-y\n@modelcontextprotocol/server-filesystem\nC:\\data'}
                className="w-full rounded-md border bg-background px-3 py-2 font-mono text-sm"
              />
            </label>
          </>
        ) : (
          <div className="block space-y-1">
            <label htmlFor={`${uid}-url`} className="text-xs font-medium">
              URL
            </label>
            <Input
              id={`${uid}-url`}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://example.com/mcp"
            />
          </div>
        )}

        <div className="space-y-1">
          <span className="text-xs font-medium">
            {transport === 'stdio' ? 'Environment variables' : 'Headers'} (values come from your .env)
          </span>
          {rows.map((r, i) => {
            const set = r.literal ? null : config.env_status[r.variable.trim()]
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: rows have no stable id while typing
              <div key={i} className="flex items-center gap-2">
                <Input
                  value={r.key}
                  onChange={(e) => updateRow(i, { key: e.target.value })}
                  placeholder={transport === 'stdio' ? 'NAME' : 'Authorization'}
                  className="w-40"
                  aria-label="Name"
                />
                {r.literal ? (
                  <span className="flex-1 text-xs text-muted-foreground">
                    A value written in the file by hand -- kept as it is
                  </span>
                ) : (
                  <>
                    {transport === 'http' && (
                      <Input
                        value={r.scheme}
                        onChange={(e) => updateRow(i, { scheme: e.target.value })}
                        placeholder="Bearer (optional)"
                        className="w-36"
                        aria-label="Scheme"
                      />
                    )}
                    <Input
                      value={r.variable}
                      onChange={(e) => updateRow(i, { variable: e.target.value })}
                      placeholder="ENV VAR NAME (never the value)"
                      className="flex-1 font-mono"
                      aria-label="Variable"
                    />
                    <span
                      className={
                        set === undefined
                          ? 'text-[11px] text-muted-foreground'
                          : set
                            ? 'text-[11px] text-emerald-600'
                            : 'text-[11px] text-amber-600'
                      }>
                      {set === undefined ? '' : set ? 'set' : 'not set'}
                    </span>
                  </>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label="Remove row"
                  onClick={() => setRows((prev) => prev.filter((_, j) => j !== i))}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            )
          })}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setRows((prev) => [...prev, { key: '', scheme: '', variable: '', literal: false }])}>
            <Plus className="mr-1 h-3.5 w-3.5" /> {transport === 'stdio' ? 'Add variable' : 'Add header'}
          </Button>
        </div>

        <div className="flex flex-wrap gap-4 text-xs">
          {(
            [
              ['Load tools', loadTools, setLoadTools],
              ['Load prompts', loadPrompts, setLoadPrompts],
            ] as const
          ).map(([label, value, setter]) => (
            <label key={label} className="flex items-center gap-1.5">
              {label}
              <select
                value={value}
                onChange={(e) => setter(e.target.value as Tri)}
                className="rounded-md border bg-background px-2 py-1">
                <option value="">default</option>
                <option value="true">on</option>
                <option value="false">off</option>
              </select>
            </label>
          ))}
          <div className="flex items-center gap-1.5">
            <label htmlFor={`${uid}-timeout`}>Request timeout (s)</label>
            <Input
              id={`${uid}-timeout`}
              value={timeout}
              onChange={(e) => setTimeoutText(e.target.value)}
              placeholder="default"
              className="h-7 w-24"
            />
          </div>
        </div>
      </fieldset>

      <p className="text-[11px] text-muted-foreground">
        Saving rewrites {config.target_path || 'the MCP config'} and reconnects the servers. Comments in the file are
        not kept; the previous file is saved next to it as .bak.
      </p>

      {testResult && (
        <div
          className={
            testResult.ok
              ? 'rounded-md border border-emerald-500/40 p-2 text-xs'
              : 'rounded-md border border-destructive/40 p-2 text-xs text-destructive'
          }>
          {testResult.ok
            ? `Connected: ${testResult.tools.length} tool(s)${
                testResult.tools.length ? ` -- ${testResult.tools.map((t) => t.name).join(', ')}` : ''
              }`
            : `Test failed: ${testResult.error}`}
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {problems.length > 0 && !lockedAll && (
        <ul className="list-disc pl-5 text-[11px] text-amber-600">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="outline" size="sm" onClick={onCancel} disabled={busy !== null}>
          Cancel
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={test}
          disabled={busy !== null || problems.length > 0 || (transport === 'stdio' && !config.stdio_editable)}>
          {busy === 'test' && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
          Test
        </Button>
        <Button
          size="sm"
          onClick={save}
          disabled={busy !== null || problems.length > 0 || stdioLocked || !config.writable}>
          {busy === 'save' && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
          Save
        </Button>
      </div>
    </div>
  )
}
