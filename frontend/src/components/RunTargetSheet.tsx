import { Bot, Check, Hammer, Loader2, Search, TriangleAlert, Workflow as WorkflowIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import {
  isServerWide,
  type RunTargetInventory,
  type RunTargetKind,
  runTargetSelectability,
  useRunTargets,
} from '@/hooks/useRunTargets'
import { cn } from '@/lib/utils'

/**
 * Narrow run-target picker (CTR-0216, PRP-0176, UDR-0158 D1/D3/D4/D5/D6/D7).
 *
 * On a phone the Declarative Agents & Workflows modal has no layout, so its entries are
 * gated (UDR-0153 D3) and the run-target used to be a read-only label. This bottom sheet
 * is the narrow destination of the composer's run-target button: it lists the Built-in
 * agent, Prompt agents, Workflows and Harness agents, searches them, and applies a
 * choice through the ONE switch module (CTR-0217).
 *
 * Browse-and-switch only (D5): no create / edit / delete / reload / YAML -- those stay on
 * the wide surface. A Prompt activation is SERVER-WIDE, so it is confirmed here with its
 * reach spelled out (D3); a Workflow / Harness choice is this browser's own and applies
 * on tap (D4).
 */

/** Dispatched on the window to REQUEST the narrow picker (the CTR-0144 seam's shape).
 *
 * Carries NO payload: opening must not stage a selection (UDR-0111 D6 / UDR-0158 D7).
 * Its listener lives in this component, which ChatPage mounts outside every conditional
 * (UDR-0115 D1) so no layout state can unmount it and swallow the request. */
export const OPEN_RUN_TARGET_PICKER_EVENT = 'chatwalaau:open-run-target-picker'

/** Request the picker from anywhere. One helper so no caller has to know the seam. */
export function requestRunTargetPicker(): void {
  window.dispatchEvent(new Event(OPEN_RUN_TARGET_PICKER_EVENT))
}

const EMPTY: RunTargetInventory = { prompts: [], workflows: [], harnesses: [], activeId: 'core' }
const TOP_LEVEL_LABEL = 'Top level'

interface Row {
  kind: RunTargetKind
  id: string
  name: string
  description?: string
  group: string
  selectable: boolean
  reason?: string
}

interface Group {
  label: string
  rows: Row[]
}

function icon(kind: RunTargetKind) {
  if (kind === 'workflow') return <WorkflowIcon className="h-4 w-4 shrink-0" />
  if (kind === 'harness') return <Hammer className="h-4 w-4 shrink-0" />
  return <Bot className="h-4 w-4 shrink-0" />
}

function groupLabel(path: string[]): string {
  return path.length ? path.join(' / ') : TOP_LEVEL_LABEL
}

export function RunTargetSheet() {
  const runTargets = useRunTargets()
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [inventory, setInventory] = useState<RunTargetInventory>(EMPTY)
  // A Prompt row waits here for its confirmation (D3); the other kinds never do.
  const [pending, setPending] = useState<Row | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setInventory(await runTargets.loadInventory())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load agents')
    } finally {
      setLoading(false)
    }
  }, [runTargets])

  const openSheet = useCallback(() => {
    setOpen(true)
    setQuery('')
    setPending(null)
    setError(null)
    void load()
  }, [load])

  useEffect(() => {
    const onRequest = () => openSheet()
    window.addEventListener(OPEN_RUN_TARGET_PICKER_EVENT, onRequest)
    return () => window.removeEventListener(OPEN_RUN_TARGET_PICKER_EVENT, onRequest)
  }, [openSheet])

  const effective = runTargets.effectiveChoice(inventory.activeId)

  const groups: Group[] = useMemo(() => {
    const builtin: Row[] = []
    const prompts: Row[] = []
    for (const a of inventory.prompts) {
      const { selectable, reason } = runTargetSelectability('prompt', a)
      const row: Row = {
        kind: 'prompt',
        id: a.id,
        name: a.display_name || a.name,
        description: a.description,
        group: a.source === 'core' ? '' : groupLabel(a.group_path),
        selectable,
        reason,
      }
      if (a.source === 'core') builtin.push(row)
      else prompts.push(row)
    }
    const workflows: Row[] = inventory.workflows.map((w) => {
      const { selectable, reason } = runTargetSelectability('workflow', w)
      return {
        kind: 'workflow',
        id: w.id,
        name: w.display_name || w.name,
        description: w.description,
        group: groupLabel(w.group_path),
        selectable,
        reason,
      }
    })
    const harnesses: Row[] = inventory.harnesses.map((h) => {
      const { selectable, reason } = runTargetSelectability('harness', h)
      return {
        kind: 'harness',
        id: h.id,
        name: h.display_name || h.name,
        description: h.description,
        group: groupLabel(h.group_path),
        selectable,
        reason,
      }
    })
    const needle = query.trim().toLowerCase()
    const match = (r: Row) =>
      !needle ||
      r.name.toLowerCase().includes(needle) ||
      (r.description ?? '').toLowerCase().includes(needle) ||
      r.group.toLowerCase().includes(needle)
    return [
      { label: 'Built-in', rows: builtin.filter(match) },
      { label: 'Agents', rows: prompts.filter(match) },
      { label: 'Workflows', rows: workflows.filter(match) },
      { label: 'Harness', rows: harnesses.filter(match) },
    ].filter((g) => g.rows.length > 0)
  }, [inventory, query])

  const apply = useCallback(
    async (row: Row) => {
      setBusy(true)
      setError(null)
      try {
        await runTargets.applyChoice({ kind: row.kind, id: row.id, name: row.name })
        setOpen(false)
        setPending(null)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not switch the agent')
        setPending(null)
      } finally {
        setBusy(false)
      }
    },
    [runTargets],
  )

  // D3/D4: a server-wide switch waits for a confirmation; a device-local one applies now.
  const choose = useCallback(
    (row: Row) => {
      if (!row.selectable || busy) return
      if (isServerWide(row.kind)) setPending(row)
      else void apply(row)
    },
    [apply, busy],
  )

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (next) return
      if (busy) return
      setOpen(false)
      setPending(null)
    },
    [busy],
  )

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="bottom" title="Choose the agent" className="gap-0">
        <div className="mx-auto mt-2 h-1 w-10 shrink-0 rounded-full bg-muted-foreground/30" aria-hidden="true" />
        <div className="shrink-0 border-b px-4 pb-3 pt-2">
          <h2 className="mb-2 text-sm font-semibold">Choose the agent</h2>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            {/* Not auto-focused: the iOS keyboard would open over the list (UDR-0153 D13
                keeps the 16 px font so focusing never zooms). */}
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search agents"
              aria-label="Search agents"
              className="h-9 w-full rounded-md border bg-background pl-8 pr-2 text-base outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
        </div>

        {error && (
          <div className="flex shrink-0 items-start gap-2 border-b bg-destructive/10 px-4 py-2 text-xs text-destructive">
            <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {pending ? (
          <div className="flex-1 overflow-y-auto px-4 py-4 text-sm">
            <p className="font-medium">Switch to {pending.name}?</p>
            <p className="mt-2 text-muted-foreground">
              This changes the agent for the whole application -- every browser and every chat, not only this device. It
              takes a moment while the agents are rebuilt.
            </p>
            <div className="mt-4 flex gap-2">
              <Button size="sm" onClick={() => void apply(pending)} disabled={busy}>
                {busy && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
                {busy ? 'Switching...' : 'Switch'}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setPending(null)} disabled={busy}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto overscroll-contain pb-2">
            {loading && (
              <div className="flex items-center gap-2 px-4 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading agents...
              </div>
            )}
            {!loading && groups.length === 0 && (
              <p className="px-4 py-6 text-sm text-muted-foreground">No agent matches.</p>
            )}
            {!loading &&
              groups.map((group) => (
                <section key={group.label}>
                  <h3 className="sticky top-0 bg-background/95 px-4 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {group.label}
                  </h3>
                  <ul>
                    {group.rows.map((row) => {
                      const current = effective.kind === row.kind && effective.id === row.id
                      return (
                        <li key={`${row.kind}:${row.id}`}>
                          <button
                            type="button"
                            onClick={() => choose(row)}
                            disabled={!row.selectable || busy}
                            aria-current={current ? 'true' : undefined}
                            className={cn(
                              'flex w-full items-start gap-2 px-4 py-3 text-left text-sm',
                              row.selectable ? 'active:bg-accent' : 'cursor-not-allowed opacity-50',
                            )}>
                            <span className="mt-0.5 text-muted-foreground">{icon(row.kind)}</span>
                            <span className="min-w-0 flex-1">
                              <span className="flex items-center gap-2">
                                <span className="truncate font-medium">{row.name}</span>
                                {current && <Check className="h-4 w-4 shrink-0 text-primary" />}
                              </span>
                              {row.group && (
                                <span className="block truncate text-xs text-muted-foreground">{row.group}</span>
                              )}
                              {row.description && (
                                <span className="block truncate text-xs text-muted-foreground">{row.description}</span>
                              )}
                              {!row.selectable && row.reason && (
                                <span className="mt-0.5 block text-xs text-destructive">{row.reason}</span>
                              )}
                            </span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </section>
              ))}
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
