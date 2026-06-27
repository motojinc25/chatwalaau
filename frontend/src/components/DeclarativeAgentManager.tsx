import { Bot, CircleCheck, Loader2, RefreshCw, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * Declarative Agent Management modal (CTR-0144, FEAT-0051, PRP-0094, UDR-0072).
 *
 * A sidebar-footer icon (next to the File Explorer icon) opens a ~90% modal -- the
 * Skills-management shape -- that lists the bundled CORE agent plus custom
 * declarative agents discovered from DECLARATIVE_AGENTS_DIR as a nested-folder tree.
 * Exactly one agent is active (single-select); CORE is always present and is the
 * default. Activating an agent shows a confirmation, PUTs to CTR-0143, and shows a
 * blocking "rebuilding" indicator until the per-model agents are rebuilt (CTR-0070).
 * The YAML is a SPECIFICATION; ChatWalaʻau owns construction -- a YAML with a mapping
 * error (loaded=false / error) is visibly flagged and CANNOT be activated. The
 * selection is not persisted client-side (the server store is the source of truth).
 * Switching is SPA-only; the OpenAI API and Teams follow the active agent.
 */

interface AgentEntry {
  id: string
  name: string
  description?: string
  group_path: string[]
  source: 'core' | 'custom'
  active: boolean
  loaded: boolean
  error?: string | null
  warnings?: string[]
}

/** Dispatched on the window after the active declarative agent changes, so the
 * model selector / options panels re-read /api/model (CTR-0144, PRP-0094). */
export const ACTIVE_AGENT_CHANGED_EVENT = 'chatwalaau:active-agent-changed'

const TOP_LEVEL_LABEL = 'Top level'
const BUILTIN_LABEL = 'Built-in'

type ConfirmMode = 'activate' | 'reload' | null

function groupLabel(entry: AgentEntry): string {
  if (entry.source === 'core') return BUILTIN_LABEL
  return entry.group_path.length ? entry.group_path.join(' / ') : TOP_LEVEL_LABEL
}

export function DeclarativeAgentManager() {
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [agents, setAgents] = useState<AgentEntry[]>([])
  const [activeId, setActiveId] = useState<string>('core')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null)

  const adopt = useCallback((data: { active?: string; agents?: AgentEntry[] }) => {
    const next = (data.agents ?? []) as AgentEntry[]
    setAgents(next)
    setActiveId(data.active ?? 'core')
    setSelectedId((prev) => prev ?? data.active ?? next[0]?.id ?? null)
  }, [])

  // Probe availability once on mount: GET /api/agents is always reachable (the CORE
  // agent always exists), so the icon shows whenever the endpoint is reachable.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/agents')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: declarative agent management is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const fetchInventory = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/agents')
      if (!res.ok) throw new Error('Failed to load agents')
      adopt(await res.json())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load agents')
    } finally {
      setLoading(false)
    }
  }, [adopt])

  const openModal = useCallback(() => {
    setOpen(true)
    setConfirmMode(null)
    void fetchInventory()
  }, [fetchInventory])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (next) {
        setOpen(true)
        return
      }
      if (busy) return // block close while rebuilding
      setOpen(false)
      setConfirmMode(null)
      setError(null)
    },
    [busy],
  )

  const doActivate = useCallback(async () => {
    if (!selectedId) return
    setConfirmMode(null)
    setBusy(true)
    setError(null)
    try {
      const res = await fetch('/api/agents/active', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: selectedId }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detail?.detail?.message || detail?.detail?.error || 'Failed to activate agent')
      }
      adopt(await res.json())
      // Tell the model selector / options panels to re-read /api/model so they reflect
      // the new agent's preferred model + option defaults immediately (CTR-0144).
      window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to activate agent')
    } finally {
      setBusy(false)
    }
  }, [selectedId, adopt])

  const doReload = useCallback(async () => {
    setConfirmMode(null)
    setBusy(true)
    setError(null)
    try {
      const res = await fetch('/api/agents/reload', { method: 'POST' })
      if (!res.ok) throw new Error('Failed to reload agents')
      adopt(await res.json())
      window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload agents')
    } finally {
      setBusy(false)
    }
  }, [adopt])

  // Group agents for the left tree (CORE first, then nested-folder groups).
  const groups = useMemo(() => {
    const map = new Map<string, AgentEntry[]>()
    for (const a of agents) {
      const label = groupLabel(a)
      const arr = map.get(label) ?? []
      arr.push(a)
      map.set(label, arr)
    }
    const ordered: Array<[string, AgentEntry[]]> = []
    const builtin = map.get(BUILTIN_LABEL)
    if (builtin) ordered.push([BUILTIN_LABEL, builtin])
    for (const [label, arr] of map) {
      if (label !== BUILTIN_LABEL) ordered.push([label, arr])
    }
    return ordered
  }, [agents])

  if (!available) return null

  const current = agents.find((a) => a.id === selectedId) ?? null
  // An agent can be activated only when it is loaded, not already active, has no
  // mapping error, AND has no warnings (a warning means the YAML must be fixed first,
  // PRP-0094 / UDR-0072 D9).
  const hasWarnings = (current?.warnings?.length ?? 0) > 0
  const canActivate =
    current?.loaded === true && !current.active && current.id !== activeId && !current.error && !hasWarnings

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6 text-muted-foreground"
        onClick={openModal}
        aria-label="Declarative agents"
        title="Declarative agents (switch the active agent)">
        <Bot className="h-4 w-4" />
      </Button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle>Declarative Agents</DialogTitle>
            <DialogDescription>
              Switch the active agent. The YAML is a specification; ChatWalaʻau owns construction (credentials and
              sampling params like temperature are ignored or rejected). Activating rebuilds the agents, so the next
              message -- and the API and Teams -- use the selected agent.
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <>
                {/* Left: nested-folder grouped agent list */}
                <div className="w-72 shrink-0 overflow-y-auto border-r">
                  {groups.map(([label, items]) => (
                    <div key={label}>
                      <div className="bg-muted/50 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {label}
                      </div>
                      {items.map((a) => (
                        <button
                          key={a.id}
                          type="button"
                          onClick={() => setSelectedId(a.id)}
                          className={cn(
                            'flex w-full items-center gap-2 border-b px-3 py-2 text-left',
                            selectedId === a.id && 'bg-accent',
                          )}>
                          <span className="flex h-4 w-4 shrink-0 items-center justify-center">
                            {a.active ? (
                              <CircleCheck className="h-4 w-4 text-primary" />
                            ) : a.loaded ? (
                              <span className="h-2.5 w-2.5 rounded-full border" />
                            ) : (
                              <TriangleAlert className="h-3.5 w-3.5 text-amber-600 dark:text-amber-500" />
                            )}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm font-medium">{a.name}</span>
                            <span className="block truncate text-[11px] text-muted-foreground">
                              {a.active ? 'Active' : a.loaded ? a.id : 'Error'}
                            </span>
                          </span>
                        </button>
                      ))}
                    </div>
                  ))}
                </div>

                {/* Right: selected agent detail */}
                <div className="min-w-0 flex-1 overflow-y-auto p-5">
                  {current ? (
                    <>
                      <div className="mb-2 flex items-center gap-2">
                        <h3 className="truncate text-base font-semibold">{current.name}</h3>
                        {current.active && (
                          <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary">
                            Active
                          </span>
                        )}
                        {current.source === 'core' && (
                          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                            Built-in
                          </span>
                        )}
                      </div>
                      <p className="mb-1 font-mono text-[11px] text-muted-foreground">{current.id}</p>
                      {current.description && (
                        <p className="mb-3 text-sm text-muted-foreground">{current.description}</p>
                      )}

                      {current.error && (
                        <div className="mb-3 flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[12px] text-amber-700 dark:text-amber-400">
                          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                          <div>
                            <div className="font-medium">This agent cannot be activated.</div>
                            <div className="mt-0.5 whitespace-pre-wrap">{current.error}</div>
                          </div>
                        </div>
                      )}

                      {hasWarnings && (
                        <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
                          <div className="mb-1 flex items-center gap-1.5 font-medium">
                            <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
                            Fix these warnings in the YAML before activating:
                          </div>
                          <ul className="space-y-1 pl-5">
                            {current?.warnings?.map((w) => (
                              <li key={w} className="list-disc">
                                {w}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      <Button size="sm" disabled={!canActivate} onClick={() => setConfirmMode('activate')}>
                        {current.active
                          ? 'Currently active'
                          : hasWarnings
                            ? 'Resolve warnings to activate'
                            : 'Activate this agent'}
                      </Button>
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">Select an agent on the left.</p>
                  )}
                </div>
              </>
            )}

            {/* Activate / reload / rebuild overlay + confirmation */}
            {(busy || confirmMode) && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
                {busy ? (
                  <div className="flex items-center gap-2 text-sm">
                    <Loader2 className="h-5 w-5 animate-spin" />
                    Rebuilding agents...
                  </div>
                ) : (
                  <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
                    <p className="text-sm font-medium">
                      {confirmMode === 'activate' ? 'Activate this agent?' : 'Reload agents from disk?'}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {confirmMode === 'activate'
                        ? 'This rebuilds the agents; the next message, the API, and Teams will use the selected agent.'
                        : 'Re-scans the declarative agents directory and rebuilds the agents.'}
                    </p>
                    <div className="mt-3 flex justify-end gap-2">
                      <Button variant="outline" size="sm" onClick={() => setConfirmMode(null)}>
                        Cancel
                      </Button>
                      {confirmMode === 'activate' ? (
                        <Button size="sm" onClick={doActivate}>
                          Activate
                        </Button>
                      ) : (
                        <Button size="sm" onClick={doReload}>
                          Reload
                        </Button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t px-6 py-3">
            <span className="truncate text-xs text-destructive">{error}</span>
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" onClick={() => setConfirmMode('reload')} disabled={busy || loading}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" /> Reload
              </Button>
              <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)} disabled={busy}>
                Close
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
