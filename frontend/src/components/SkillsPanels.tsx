import { FilePlus2, Loader2, Trash2, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/**
 * Panels of the Skills modal added by PRP-0266 (CTR-0124 v3, UDR-0247).
 *
 * Each panel renders inside the modal's overlay, like the existing confirmations:
 *
 *   AddSourcePanel   -- a GitHub URL, a preview of what it publishes, then Add
 *   ReviewPanel      -- the diff before an update, and the consent of an
 *                       unverified install (repository, ref, commit, files,
 *                       the start of SKILL.md, a warning; never a block)
 *   NewSkillPanel    -- create local/<name>, or duplicate a skill into it
 *   EditorPanel      -- edit the text files of a local skill; Save is one request
 *   ManifestPanel    -- import a skills-lock.json or a chatwalaau-skills.json
 *
 * Every write here goes to an admin-only route (UDR-0247 D2); a refusal comes back
 * as the route's message.
 */

export interface ApiResult<T = unknown> {
  ok: boolean
  status: number
  body: T
}

export async function api<T = unknown>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  const res = await fetch(path, init)
  const body = (await res.json().catch(() => null)) as T
  return { ok: res.ok, status: res.status, body }
}

export function apiError(body: unknown, fallback: string): string {
  const detail = (body as { detail?: { message?: string; error?: string } | string } | undefined)?.detail
  if (typeof detail === 'string') return detail
  return detail?.message || detail?.error || fallback
}

function jsonInit(method: string, payload: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }
}

function PanelShell({
  title,
  wide,
  children,
  footer,
  testId,
}: {
  title: string
  wide?: boolean
  children: React.ReactNode
  footer: React.ReactNode
  testId: string
}) {
  return (
    <div
      data-testid={testId}
      className={cn(
        'flex max-h-[80vh] flex-col rounded-lg border bg-background shadow-lg',
        wide ? 'w-[min(960px,92%)]' : 'w-[min(560px,92%)]',
      )}>
      <div className="border-b px-4 py-2.5 text-sm font-medium">{title}</div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-xs">{children}</div>
      <div className="flex justify-end gap-2 border-t px-4 py-2.5">{footer}</div>
    </div>
  )
}

function Warning({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-start gap-1.5 rounded-md border border-amber-500/40 p-2 text-[11px] text-amber-600 dark:text-amber-500">
      <TriangleAlert className="mt-px h-3.5 w-3.5 shrink-0" />
      <span>{children}</span>
    </p>
  )
}

export const UNVERIFIED_WARNING =
  'This skill comes from a source nobody has reviewed. Its scripts can run on this server when CODING_ENABLED is on.'

// ---------------------------------------------------------------------------
// Add source
// ---------------------------------------------------------------------------

interface PreviewSkill {
  name: string
  description?: string
  repo_path: string
  file_count?: number
  has_scripts?: boolean
  warnings?: string[]
}

interface PreviewResult {
  repo: string
  ref: string
  path: string
  suggested_id: string
  commit: string
  skills: PreviewSkill[]
}

export function AddSourcePanel({ onDone, onCancel }: { onDone: (catalog: unknown) => void; onCancel: () => void }) {
  const [url, setUrl] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const runPreview = useCallback(async () => {
    setBusy(true)
    setError(null)
    setPreview(null)
    try {
      const r = await api<PreviewResult>('/api/skills/sources/preview', jsonInit('POST', { url }))
      if (!r.ok) throw new Error(apiError(r.body, 'The source could not be read'))
      setPreview(r.body)
      setSourceId(r.body.suggested_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The source could not be read')
    } finally {
      setBusy(false)
    }
  }, [url])

  const runAdd = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await api('/api/skills/sources', jsonInit('POST', { url, id: sourceId }))
      if (!r.ok) throw new Error(apiError(r.body, 'The source could not be added'))
      onDone(r.body)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The source could not be added')
      setBusy(false)
    }
  }, [url, sourceId, onDone])

  return (
    <PanelShell
      title="Add a skill source"
      testId="skills-add-source"
      footer={
        <>
          <Button variant="outline" size="sm" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant="outline" size="sm" onClick={runPreview} disabled={busy || !url.trim()}>
            {busy && !preview ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
            Preview
          </Button>
          <Button size="sm" onClick={runAdd} disabled={busy || !preview || !sourceId.trim()}>
            Add source
          </Button>
        </>
      }>
      <label className="block text-[11px] text-muted-foreground" htmlFor="skills-source-url">
        GitHub repository: owner/repo, owner/repo@tag, or a github.com URL (optionally /tree/&lt;ref&gt;/&lt;path&gt;)
      </label>
      <input
        id="skills-source-url"
        value={url}
        onChange={(e) => {
          setUrl(e.target.value)
          setPreview(null)
        }}
        placeholder="playcanvas/skills"
        className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs"
      />
      {error && <p className="mt-2 text-[11px] text-destructive">{error}</p>}
      {preview && (
        <div className="mt-3 space-y-2">
          <p className="text-muted-foreground">
            {preview.repo}
            {preview.ref ? ` @ ${preview.ref}` : ' (tracks the default branch)'}
            {preview.path ? `, path ${preview.path}` : ''}, commit{' '}
            <span className="font-mono">{preview.commit.slice(0, 12) || '-'}</span>
          </p>
          <Warning>
            Sources added here are labelled Unverified. Nobody has reviewed what they publish; check each skill before
            installing it.
          </Warning>
          <label className="block text-[11px] text-muted-foreground" htmlFor="skills-source-id">
            Source id (also the folder group under the skills directory)
          </label>
          <input
            id="skills-source-id"
            value={sourceId}
            onChange={(e) => setSourceId(e.target.value)}
            className="w-full rounded-md border bg-background px-2 py-1 font-mono text-xs"
          />
          <p className="font-medium">{preview.skills.length} skill(s) found</p>
          <ul className="space-y-1" data-testid="skills-source-preview">
            {preview.skills.map((s) => (
              <li key={s.repo_path} className="rounded-md border p-1.5">
                <span className="font-mono">{s.name}</span>
                <span className="ml-2 text-[10px] text-muted-foreground">{s.repo_path}</span>
                {s.has_scripts && <span className="ml-2 text-[10px] text-amber-600">scripts</span>}
                {s.description && <div className="text-[11px] text-muted-foreground">{s.description}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </PanelShell>
  )
}

// ---------------------------------------------------------------------------
// Review (diff before an update, consent of an unverified install)
// ---------------------------------------------------------------------------

interface DiffFile {
  path: string
  size: number
}

export interface SkillDiff {
  id: string
  name: string
  installed: boolean
  trust: string
  repo: string
  ref: string
  commit: string
  revision: string
  added: DiffFile[]
  changed: DiffFile[]
  removed: DiffFile[]
  scripts_added: string[]
  dependencies_changed: string[]
  locally_modified: boolean
  skill_md: string
}

export function ReviewPanel({
  skillId,
  licenseText,
  onApply,
  onCancel,
}: {
  skillId: string
  licenseText: string
  onApply: (diff: SkillDiff) => void
  onCancel: () => void
}) {
  const [diff, setDiff] = useState<SkillDiff | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const r = await api<SkillDiff>(`/api/skills/install/${skillId}/diff`)
        if (!r.ok) throw new Error(apiError(r.body, 'The skill could not be downloaded for review'))
        if (!cancelled) setDiff(r.body)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'The skill could not be downloaded for review')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [skillId])

  const isUpdate = diff?.installed === true
  const noChange = diff !== null && diff.added.length + diff.changed.length + diff.removed.length === 0
  return (
    <PanelShell
      wide
      title={diff ? `${isUpdate ? 'Update' : 'Install'} ${diff.name}?` : 'Reviewing...'}
      testId="skills-review"
      footer={
        <>
          <Button variant="outline" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            size="sm"
            variant={diff?.locally_modified ? 'destructive' : 'default'}
            disabled={!diff}
            onClick={() => diff && onApply(diff)}>
            {isUpdate ? (diff?.locally_modified ? 'Discard my edits and update' : 'Update') : 'Install'}
          </Button>
        </>
      }>
      {error && <p className="text-destructive">{error}</p>}
      {!diff && !error && (
        <p className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Downloading the skill to compare...
        </p>
      )}
      {diff && (
        <div className="space-y-2">
          <p className="text-muted-foreground">
            {diff.repo} {diff.ref ? `@ ${diff.ref}` : '(default branch)'}, commit{' '}
            <span className="font-mono">{diff.commit.slice(0, 12)}</span>. License: {licenseText}.
          </p>
          {diff.trust === 'unverified' && <Warning>{UNVERIFIED_WARNING}</Warning>}
          {diff.scripts_added.length > 0 && (
            <Warning>
              New script file(s): {diff.scripts_added.join(', ')}. The agent can run them once installed.
            </Warning>
          )}
          {diff.dependencies_changed.length > 0 && (
            <Warning>
              Dependency file(s): {diff.dependencies_changed.join(', ')}. ChatWalaau does not install a skill's own
              dependencies; install them yourself if its scripts need them.
            </Warning>
          )}
          {diff.locally_modified && <Warning>This skill has local edits. Updating replaces them.</Warning>}
          {noChange && <p className="text-muted-foreground">No file differs from what is installed.</p>}
          <div className="grid grid-cols-3 gap-2" data-testid="skills-review-files">
            {(
              [
                ['Added', diff.added],
                ['Changed', diff.changed],
                ['Removed', diff.removed],
              ] as const
            ).map(([label, files]) => (
              <div key={label} className="rounded-md border p-1.5">
                <div className="mb-1 font-medium">
                  {label} ({files.length})
                </div>
                <ul className="max-h-32 overflow-y-auto font-mono text-[10px]">
                  {files.map((f) => (
                    <li key={f.path} className="truncate" title={f.path}>
                      {f.path}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <div>
            <div className="mb-1 font-medium">SKILL.md (first 4000 characters; it goes into the agent's context)</div>
            <pre
              data-testid="skills-review-skillmd"
              className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/40 p-2 font-mono text-[10px]">
              {diff.skill_md}
            </pre>
          </div>
        </div>
      )}
    </PanelShell>
  )
}

// ---------------------------------------------------------------------------
// New skill / duplicate
// ---------------------------------------------------------------------------

export function NewSkillPanel({
  fromId,
  onDone,
  onCancel,
}: {
  fromId?: string
  onDone: (catalog: unknown, name: string) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(fromId ? `${fromId.split('/').pop()}-copy` : '')
  const [description, setDescription] = useState('')
  const [body, setBody] = useState('# Instructions\n\nDescribe when and how the agent should use this skill.\n')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const payload = fromId ? { name, from_id: fromId } : { name, description, body }
      const r = await api<{ catalog: unknown }>('/api/skills/local', jsonInit('POST', payload))
      if (!r.ok) throw new Error(apiError(r.body, 'The skill could not be created'))
      onDone(r.body.catalog, name)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The skill could not be created')
      setBusy(false)
    }
  }, [fromId, name, description, body, onDone])

  return (
    <PanelShell
      title={fromId ? `Duplicate ${fromId} to edit` : 'New skill'}
      testId="skills-new-skill"
      footer={
        <>
          <Button variant="outline" size="sm" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={run} disabled={busy || !name.trim() || (!fromId && !description.trim())}>
            {busy && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            {fromId ? 'Duplicate' : 'Create'}
          </Button>
        </>
      }>
      <p className="mb-2 text-muted-foreground">
        The skill is written to the <span className="font-mono">local</span> group. It never receives updates from a
        source.
      </p>
      <label className="block text-[11px] text-muted-foreground" htmlFor="skills-new-name">
        Name (lowercase letters, digits and hyphens)
      </label>
      <input
        id="skills-new-name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="mt-1 w-full rounded-md border bg-background px-2 py-1 font-mono text-xs"
      />
      {!fromId && (
        <>
          <label className="mt-2 block text-[11px] text-muted-foreground" htmlFor="skills-new-description">
            Description (tells the agent when to use it)
          </label>
          <input
            id="skills-new-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="mt-1 w-full rounded-md border bg-background px-2 py-1 text-xs"
          />
          <label className="mt-2 block text-[11px] text-muted-foreground" htmlFor="skills-new-body">
            Instructions (the body of SKILL.md)
          </label>
          <textarea
            id="skills-new-body"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={10}
            className="mt-1 w-full rounded-md border bg-background px-2 py-1 font-mono text-xs"
          />
        </>
      )}
      {error && <p className="mt-2 text-[11px] text-destructive">{error}</p>}
    </PanelShell>
  )
}

// ---------------------------------------------------------------------------
// Editor (local skills only)
// ---------------------------------------------------------------------------

interface LocalFile {
  path: string
  size: number
  text: string | null
  binary: boolean
}

export function EditorPanel({
  name,
  readOnly,
  onSaved,
  onCancel,
}: {
  name: string
  readOnly: boolean
  onSaved: (catalog: unknown) => void
  onCancel: () => void
}) {
  const [original, setOriginal] = useState<LocalFile[]>([])
  const [texts, setTexts] = useState<Record<string, string>>({})
  const [deleted, setDeleted] = useState<string[]>([])
  const [current, setCurrent] = useState('SKILL.md')
  const [newPath, setNewPath] = useState('')
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const r = await api<{ files: LocalFile[] }>(`/api/skills/local/${encodeURIComponent(name)}`)
        if (!r.ok) throw new Error(apiError(r.body, 'The skill could not be read'))
        if (cancelled) return
        setOriginal(r.body.files)
        const map: Record<string, string> = {}
        for (const f of r.body.files) if (f.text !== null) map[f.path] = f.text
        setTexts(map)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'The skill could not be read')
      } finally {
        if (!cancelled) setBusy(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [name])

  const changed = useMemo(() => {
    const out: Record<string, string> = {}
    const before = new Map(original.map((f) => [f.path, f.text]))
    for (const [path, text] of Object.entries(texts)) if (before.get(path) !== text) out[path] = text
    return out
  }, [original, texts])
  const dirty = Object.keys(changed).length > 0 || deleted.length > 0
  const paths = useMemo(() => {
    const all = new Set([...original.map((f) => f.path), ...Object.keys(texts)])
    return [...all].filter((p) => !deleted.includes(p)).sort()
  }, [original, texts, deleted])
  const binary = new Set(original.filter((f) => f.binary).map((f) => f.path))

  const save = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await api<{ catalog: unknown }>(
        `/api/skills/local/${encodeURIComponent(name)}`,
        jsonInit('PUT', { files: changed, deleted }),
      )
      if (!r.ok) throw new Error(apiError(r.body, 'The skill could not be saved'))
      onSaved(r.body.catalog)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The skill could not be saved')
      setBusy(false)
    }
  }, [name, changed, deleted, onSaved])

  return (
    <PanelShell
      wide
      title={`Edit local/${name}`}
      testId="skills-editor"
      footer={
        <>
          <span className="mr-auto self-center text-[11px] text-destructive">{error}</span>
          <Button variant="outline" size="sm" onClick={onCancel} disabled={busy && original.length > 0}>
            {dirty ? 'Discard' : 'Close'}
          </Button>
          {!readOnly && (
            <Button size="sm" onClick={save} disabled={busy || !dirty}>
              {busy && original.length > 0 && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
              Save
            </Button>
          )}
        </>
      }>
      <div className="flex h-[55vh] gap-3">
        <div className="flex w-52 shrink-0 flex-col gap-1 overflow-y-auto">
          {paths.map((p) => (
            <div key={p} className={cn('flex items-center gap-1 rounded px-1', current === p && 'bg-accent')}>
              <button
                type="button"
                className="min-w-0 flex-1 truncate py-0.5 text-left font-mono text-[11px]"
                onClick={() => setCurrent(p)}
                title={p}>
                {p}
              </button>
              {!readOnly && p !== 'SKILL.md' && (
                <button
                  type="button"
                  aria-label={`Delete ${p}`}
                  className="text-muted-foreground hover:text-destructive"
                  onClick={() => {
                    setDeleted((d) => [...d, p])
                    setTexts((t) => {
                      const next = { ...t }
                      delete next[p]
                      return next
                    })
                    if (current === p) setCurrent('SKILL.md')
                  }}>
                  <Trash2 className="h-3 w-3" />
                </button>
              )}
            </div>
          ))}
          {!readOnly && (
            <div className="mt-2 flex gap-1">
              <input
                value={newPath}
                onChange={(e) => setNewPath(e.target.value)}
                placeholder="references/notes.md"
                aria-label="New file path"
                className="min-w-0 flex-1 rounded border bg-background px-1 py-0.5 font-mono text-[10px]"
              />
              <button
                type="button"
                aria-label="Add file"
                className="text-muted-foreground hover:text-foreground"
                disabled={!newPath.trim()}
                onClick={() => {
                  const p = newPath.trim().replace(/\\/g, '/')
                  setTexts((t) => ({ ...t, [p]: t[p] ?? '' }))
                  setDeleted((d) => d.filter((x) => x !== p))
                  setCurrent(p)
                  setNewPath('')
                }}>
                <FilePlus2 className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </div>
        <div className="min-w-0 flex-1">
          {binary.has(current) ? (
            <p className="text-muted-foreground">This is a binary file. Replace it by importing a ZIP.</p>
          ) : (
            <textarea
              aria-label={`Content of ${current}`}
              value={texts[current] ?? ''}
              readOnly={readOnly}
              onChange={(e) => setTexts((t) => ({ ...t, [current]: e.target.value }))}
              className="h-full w-full resize-none rounded-md border bg-background p-2 font-mono text-[11px]"
            />
          )}
        </div>
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">
        SKILL.md must keep <span className="font-mono">name: {name}</span> and a description in its frontmatter. Save
        rebuilds the agents once.
      </p>
    </PanelShell>
  )
}

// ---------------------------------------------------------------------------
// Manifest import
// ---------------------------------------------------------------------------

interface ManifestPlan {
  format: string
  sources_to_add: { id: string; repo: string; ref: string }[]
  skills: { name: string; id: string; repo: string; repo_path: string }[]
  unsupported: { name: string; reason: string }[]
  results?: { id: string; status: string; message?: string }[]
  catalog?: unknown
}

export function ManifestPanel({ onDone, onCancel }: { onDone: (catalog: unknown) => void; onCancel: () => void }) {
  const [document, setDocument] = useState<Record<string, unknown> | null>(null)
  const [plan, setPlan] = useState<ManifestPlan | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const readFile = useCallback(async (file: File) => {
    setError(null)
    setPlan(null)
    try {
      const doc = JSON.parse(await file.text()) as Record<string, unknown>
      setDocument(doc)
      setBusy(true)
      const r = await api<ManifestPlan>('/api/skills/manifest/import', jsonInit('POST', { document: doc }))
      if (!r.ok) throw new Error(apiError(r.body, 'The manifest could not be read'))
      setPlan(r.body)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The manifest could not be read')
    } finally {
      setBusy(false)
    }
  }, [])

  const apply = useCallback(async () => {
    if (!document) return
    setBusy(true)
    setError(null)
    try {
      const r = await api<ManifestPlan>('/api/skills/manifest/import', jsonInit('POST', { document, apply: true }))
      if (!r.ok) throw new Error(apiError(r.body, 'The manifest could not be imported'))
      setPlan(r.body)
      onDone(r.body.catalog)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The manifest could not be imported')
    } finally {
      setBusy(false)
    }
  }, [document, onDone])

  const applied = plan?.results !== undefined
  return (
    <PanelShell
      title="Import skills from a manifest"
      testId="skills-manifest"
      footer={
        <>
          <Button variant="outline" size="sm" onClick={onCancel} disabled={busy}>
            {applied ? 'Close' : 'Cancel'}
          </Button>
          {!applied && (
            <Button size="sm" onClick={apply} disabled={busy || !plan || plan.skills.length === 0}>
              {busy && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
              Import
            </Button>
          )}
        </>
      }>
      <p className="mb-2 text-muted-foreground">
        A <span className="font-mono">skills-lock.json</span> (from the npx skills CLI) or a{' '}
        <span className="font-mono">chatwalaau-skills.json</span> exported from another deployment. New sources are
        added as Unverified.
      </p>
      <input
        type="file"
        accept=".json,application/json"
        aria-label="Manifest file"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) void readFile(file)
        }}
        className="text-xs"
      />
      {error && <p className="mt-2 text-[11px] text-destructive">{error}</p>}
      {plan && (
        <div className="mt-3 space-y-2" data-testid="skills-manifest-plan">
          <p className="font-medium">
            {plan.format === 'skills-lock' ? 'skills-lock.json' : 'ChatWalaau manifest'}: {plan.skills.length} skill(s),{' '}
            {plan.sources_to_add.length} new source(s)
          </p>
          <ul className="space-y-0.5">
            {plan.skills.map((s) => {
              const result = plan.results?.find((r) => r.id === s.id)
              return (
                <li key={s.id} className="font-mono text-[11px]">
                  {s.name}{' '}
                  <span className="text-muted-foreground">
                    ({s.repo}/{s.repo_path})
                  </span>
                  {result && (
                    <span className={result.status === 'installed' ? 'ml-2 text-emerald-600' : 'ml-2 text-destructive'}>
                      {result.status}
                      {result.message ? `: ${result.message}` : ''}
                    </span>
                  )}
                </li>
              )
            })}
          </ul>
          {plan.unsupported.length > 0 && (
            <div>
              <p className="font-medium text-amber-600 dark:text-amber-500">Not imported</p>
              <ul>
                {plan.unsupported.map((u) => (
                  <li key={u.name} className="text-[11px]">
                    <span className="font-mono">{u.name}</span>: {u.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </PanelShell>
  )
}
