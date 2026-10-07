/**
 * Ontology history (CTR-0173 v7, PRP-0202 / UDR-0184 D8).
 *
 * HistoryPanel -- the right pane's History tab: the versions of the open ontology
 * (the current file and its backups), the statements a version added and removed
 * ("Changes in this version") or would change ("Compared with now"), and Restore /
 * Download. A restore is a normal save on the server (revision checked, itself
 * undoable); it is always confirmed with its counts first.
 *
 * DeletedOntologiesDialog -- the trash: deleted ontologies restored under their id.
 */

import { ArchiveRestore, Download, Loader2, RotateCcw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  CURRENT_VERSION,
  type DeletedOntology,
  DIFF_PAGE,
  type DiffStatement,
  describeVersion,
  formatBytes,
  type HistoryVersion,
  restoreSummary,
  type VersionDiff,
  versionTime,
} from '@/lib/ontologyHistory'
import type { Term } from '@/lib/ontologyModel'
import { cn } from '@/lib/utils'

function localTime(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

async function errorMessage(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null)
  const detail = body?.detail
  if (typeof detail?.message === 'string') return detail.message
  if (typeof detail === 'string') return detail
  return fallback
}

type Mode = 'previous' | 'current'

export function HistoryPanel(props: {
  ontologyId: string | null
  revision: string | null
  readOnly: boolean
  formatTerm: (term: Term) => string
  /** Run `action` now, or after the unsaved-changes prompt. */
  guardDirty: (action: () => void) => void
  onRestored: () => void
}) {
  const { ontologyId, revision } = props
  const [versions, setVersions] = useState<HistoryVersion[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('previous')
  const [diff, setDiff] = useState<VersionDiff | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [confirm, setConfirm] = useState<{ version: string; summary: string } | null>(null)
  const [restoring, setRestoring] = useState(false)

  // Reload the list whenever the open ontology or its revision changes (every save).
  useEffect(() => {
    if (!ontologyId) {
      setVersions([])
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    fetch(`/api/ontology/${ontologyId}/history`)
      .then(async (res) => {
        if (!res.ok) throw new Error(await errorMessage(res, 'Failed to load the history'))
        return res.json()
      })
      .then((body) => {
        if (!cancelled) setVersions((body.versions ?? []) as HistoryVersion[])
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load the history')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    // `revision` is a dependency on purpose: a save adds a version.
    void revision
    return () => {
      cancelled = true
    }
  }, [ontologyId, revision])

  const fetchDiff = useCallback(
    async (version: string, against: Mode, offset: number, limit = DIFF_PAGE): Promise<VersionDiff> => {
      const params = new URLSearchParams({ against, offset: String(offset), limit: String(limit) })
      const res = await fetch(`/api/ontology/${ontologyId}/history/${encodeURIComponent(version)}/diff?${params}`)
      if (!res.ok) throw new Error(await errorMessage(res, 'Failed to compare the versions'))
      return (await res.json()) as VersionDiff
    },
    [ontologyId],
  )

  // Selecting a version or switching the comparison loads the first page.
  useEffect(() => {
    if (!ontologyId || !selected) {
      setDiff(null)
      return
    }
    let cancelled = false
    setDiffLoading(true)
    setError(null)
    fetchDiff(selected, mode, 0)
      .then((d) => {
        if (!cancelled) setDiff(d)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to compare the versions')
      })
      .finally(() => {
        if (!cancelled) setDiffLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [ontologyId, selected, mode, fetchDiff])

  const showMore = async () => {
    if (!selected || !diff) return
    setDiffLoading(true)
    try {
      const next = await fetchDiff(selected, mode, diff.offset + diff.limit)
      setDiff({
        ...next,
        offset: next.offset,
        added: [...diff.added, ...next.added],
        removed: [...diff.removed, ...next.removed],
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to compare the versions')
    } finally {
      setDiffLoading(false)
    }
  }

  const askRestore = (version: string) => {
    props.guardDirty(() => {
      void (async () => {
        setError(null)
        try {
          const counts = await fetchDiff(version, 'current', 0, 1)
          setConfirm({ version, summary: restoreSummary(counts) })
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Failed to compare the versions')
        }
      })()
    })
  }

  const restore = async () => {
    if (!confirm || !ontologyId) return
    setRestoring(true)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/${ontologyId}/history/${encodeURIComponent(confirm.version)}/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision }),
      })
      if (!res.ok) throw new Error(await errorMessage(res, 'Failed to restore the version'))
      setConfirm(null)
      setSelected(null)
      props.onRestored()
    } catch (err) {
      setConfirm(null)
      setError(err instanceof Error ? err.message : 'Failed to restore the version')
    } finally {
      setRestoring(false)
    }
  }

  if (!ontologyId) return <p className="p-3 text-xs text-zinc-500">Open an ontology to see its history.</p>

  const selectedVersion = versions.find((v) => v.version === selected) ?? null

  return (
    <div className="flex min-h-0 flex-1 flex-col text-xs">
      {error && <div className="border-b bg-red-50 px-3 py-1.5 text-[11px] text-red-700">{error}</div>}
      <div className="max-h-[45%] min-h-0 shrink-0 overflow-y-auto border-b">
        {loading && versions.length === 0 ? (
          <div className="flex items-center gap-2 p-3 text-zinc-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading history...
          </div>
        ) : (
          versions.map((v) => (
            <button
              key={v.version}
              type="button"
              onClick={() => setSelected(v.version === selected ? null : v.version)}
              className={cn(
                'flex w-full items-start justify-between gap-2 border-b px-3 py-1.5 text-left',
                v.version === selected ? 'bg-blue-50' : 'hover:bg-zinc-50',
              )}>
              <span className="min-w-0">
                <span className="block truncate font-medium text-zinc-800">
                  {v.version === CURRENT_VERSION ? 'Now: ' : ''}
                  {describeVersion(v, localTime)}
                </span>
                <span className="block text-[10px] text-zinc-500">
                  {localTime(versionTime(v)) || 'time unknown'}
                  {v.legacy ? ' (before history was recorded)' : ''}
                </span>
              </span>
              <span className="shrink-0 text-[10px] text-zinc-400">{formatBytes(v.bytes)}</span>
            </button>
          ))
        )}
      </div>

      {selectedVersion ? (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex shrink-0 flex-wrap items-center gap-1 border-b px-3 py-1.5">
            {(['previous', 'current'] as const).map((m) => (
              <button
                key={m}
                type="button"
                disabled={m === 'current' && selectedVersion.version === CURRENT_VERSION}
                onClick={() => setMode(m)}
                className={cn(
                  'rounded px-2 py-0.5 disabled:opacity-40',
                  mode === m ? 'bg-blue-100 text-blue-700' : 'text-zinc-600 hover:bg-zinc-100',
                )}>
                {m === 'previous' ? 'Changes in this version' : 'Compared with now'}
              </button>
            ))}
            <span className="flex-1" />
            <a
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-zinc-600 hover:bg-zinc-100"
              href={`/api/ontology/${ontologyId}/history/${encodeURIComponent(selectedVersion.version)}/file`}
              download>
              <Download className="h-3 w-3" /> Download
            </a>
            {selectedVersion.version !== CURRENT_VERSION && (
              <Button
                size="sm"
                variant="outline"
                className="h-6 text-[11px]"
                disabled={props.readOnly || restoring}
                title={props.readOnly ? 'Disabled in demo mode' : 'Make this version the current one'}
                onClick={() => askRestore(selectedVersion.version)}>
                <RotateCcw className="mr-1 h-3 w-3" /> Restore this version
              </Button>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
            {diffLoading && !diff ? (
              <div className="flex items-center gap-2 text-zinc-500">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Comparing...
              </div>
            ) : diff ? (
              <DiffView
                diff={diff}
                mode={mode}
                formatTerm={props.formatTerm}
                loadingMore={diffLoading}
                onMore={() => void showMore()}
              />
            ) : null}
          </div>
        </div>
      ) : (
        <p className="p-3 text-zinc-500">
          Select a version to see what changed. Restoring a version saves it as a new version, so it can be undone.
        </p>
      )}

      <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && !restoring && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Restore this version?</AlertDialogTitle>
            <AlertDialogDescription>
              {confirm?.summary} The current version is kept in the history, so you can restore it again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={restoring}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={restoring}
              onClick={(e) => {
                e.preventDefault()
                void restore()
              }}>
              {restoring ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              Restore
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function DiffView(props: {
  diff: VersionDiff
  mode: Mode
  formatTerm: (term: Term) => string
  loadingMore: boolean
  onMore: () => void
}) {
  const { diff } = props
  const doc = diff.document
  const docChanges =
    doc.prefixes_added.length + doc.prefixes_removed.length + (doc.base ? 1 : 0) + (doc.version ? 1 : 0)
  const more = diff.added.length < diff.added_count || diff.removed.length < diff.removed_count
  const caption =
    props.mode === 'previous'
      ? diff.against
        ? 'What this version changed compared with the one before it.'
        : 'The first recorded version: everything in it is listed as added.'
      : 'What restoring this version would change in the current ontology.'
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-zinc-500">{caption}</p>
      <p className="font-medium text-zinc-700">
        <span className="text-green-700">+{diff.added_count}</span>{' '}
        <span className="text-red-700">-{diff.removed_count}</span> statements
        {docChanges > 0 ? `, ${docChanges} document change${docChanges === 1 ? '' : 's'}` : ''}
      </p>
      {docChanges > 0 && (
        <ul className="space-y-0.5 font-mono text-[11px]">
          {doc.prefixes_added.map((p) => (
            <li key={`+${p.prefix}`} className="text-green-700">
              + PREFIX {p.prefix}: &lt;{p.iri}&gt;
            </li>
          ))}
          {doc.prefixes_removed.map((p) => (
            <li key={`-${p.prefix}`} className="text-red-700">
              - PREFIX {p.prefix}: &lt;{p.iri}&gt;
            </li>
          ))}
          {doc.base && (
            <li className="text-zinc-700">
              BASE {doc.base[0] ?? '(none)'} -&gt; {doc.base[1] ?? '(none)'}
            </li>
          )}
          {doc.version && (
            <li className="text-zinc-700">
              VERSION {doc.version[0] ?? '(none)'} -&gt; {doc.version[1] ?? '(none)'}
            </li>
          )}
        </ul>
      )}
      <StatementList items={diff.added} sign="+" formatTerm={props.formatTerm} />
      <StatementList items={diff.removed} sign="-" formatTerm={props.formatTerm} />
      {more && (
        <Button
          size="sm"
          variant="ghost"
          className="h-6 text-[11px]"
          disabled={props.loadingMore}
          onClick={props.onMore}>
          {props.loadingMore ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
          Show more
        </Button>
      )}
    </div>
  )
}

function StatementList(props: { items: DiffStatement[]; sign: '+' | '-'; formatTerm: (term: Term) => string }) {
  if (props.items.length === 0) return null
  const color = props.sign === '+' ? 'text-green-700 bg-green-50' : 'text-red-700 bg-red-50'
  return (
    <ul className="space-y-0.5 font-mono text-[11px]">
      {props.items.map((st) => {
        const text = `${props.formatTerm(st.s)} ${props.formatTerm({ type: 'iri', value: st.p })} ${props.formatTerm(st.o)}${
          st.g ? `  (graph ${props.formatTerm(st.g)})` : ''
        }`
        return (
          <li key={`${props.sign}${text}`} className={cn('truncate rounded px-1', color)} title={text}>
            {props.sign} {text}
          </li>
        )
      })}
    </ul>
  )
}

export function DeletedOntologiesDialog(props: {
  open: boolean
  onOpenChange: (open: boolean) => void
  readOnly: boolean
  onRestored: (id: string) => void
}) {
  const [items, setItems] = useState<DeletedOntology[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [restoring, setRestoring] = useState<string | null>(null)

  useEffect(() => {
    if (!props.open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    fetch('/api/ontology/trash')
      .then(async (res) => {
        if (!res.ok) throw new Error(await errorMessage(res, 'Failed to load deleted ontologies'))
        return res.json()
      })
      .then((body) => {
        if (!cancelled) setItems((body.deleted ?? []) as DeletedOntology[])
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load deleted ontologies')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [props.open])

  const restore = async (item: DeletedOntology) => {
    setRestoring(item.id)
    setError(null)
    try {
      const res = await fetch(`/api/ontology/trash/${item.id}/restore`, { method: 'POST' })
      if (!res.ok) throw new Error(await errorMessage(res, 'Failed to restore the ontology'))
      props.onOpenChange(false)
      props.onRestored(item.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to restore the ontology')
    } finally {
      setRestoring(null)
    }
  }

  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Deleted ontologies</DialogTitle>
          <DialogDescription>
            Deleted ontologies can be restored with their id, so IRIs made in them stay valid. They are removed
            automatically after the time shown.
          </DialogDescription>
        </DialogHeader>
        {error && <p className="text-xs text-red-600">{error}</p>}
        <div className="max-h-80 overflow-y-auto text-xs">
          {loading ? (
            <div className="flex items-center gap-2 text-zinc-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading...
            </div>
          ) : items.length === 0 ? (
            <p className="text-zinc-500">No deleted ontologies.</p>
          ) : (
            items.map((item) => (
              <div key={item.id} className="flex items-center justify-between gap-2 border-b py-2">
                <div className="min-w-0">
                  <div className="truncate font-medium text-zinc-800">{item.name}</div>
                  <div className="text-[10px] text-zinc-500">
                    Deleted {localTime(item.deleted_at)} - {formatBytes(item.bytes)}
                    {item.expires_at
                      ? ` - removed after ${localTime(item.expires_at)}`
                      : ' - kept until removed by hand'}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 shrink-0 text-xs"
                  disabled={props.readOnly || restoring !== null}
                  title={props.readOnly ? 'Disabled in demo mode' : 'Restore this ontology'}
                  onClick={() => void restore(item)}>
                  {restoring === item.id ? (
                    <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <ArchiveRestore className="mr-1 h-3.5 w-3.5" />
                  )}
                  Restore
                </Button>
              </div>
            ))
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => props.onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
