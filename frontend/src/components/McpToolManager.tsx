import { Loader2, Plug } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * MCP Tool Management modal (CTR-0122, FEAT-0045, PRP-0086, UDR-0064).
 *
 * An icon in the chat input controls row opens a ~90% modal that lets the
 * operator enable/disable MCP servers and individual MCP tools at runtime to
 * bound per-turn tool-schema token cost. Saving updates the backend in-memory
 * override store (CTR-0121) and rebuilds the per-model agents (CTR-0070); a
 * blocking "rebuilding" indicator shows until the rebuild completes.
 *
 * Closing: the top-right X / Esc / overlay and a formal Close button. If there
 * are unsaved changes either path prompts to Save or Discard. The selection is
 * NOT persisted client-side (the backend store is the source of truth, reloaded
 * on open). The icon is hidden when no MCP servers are configured (UDR-0064 D4).
 */

interface McpTool {
  name: string
  description?: string
  enabled: boolean
}

interface McpServer {
  name: string
  transport: string
  status: string
  enabled: boolean
  tools: McpTool[]
}

type ConfirmMode = 'save' | 'close' | null

function selectionKey(servers: McpServer[]): string {
  // Stable signature of the enabled state for dirty detection.
  return JSON.stringify(
    servers.map((s) => ({
      n: s.name,
      e: s.enabled,
      t: s.tools.map((t) => [t.name, t.enabled] as const),
    })),
  )
}

export function McpToolManager() {
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [servers, setServers] = useState<McpServer[]>([])
  const [baseline, setBaseline] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null)

  const dirty = servers.length > 0 && selectionKey(servers) !== baseline

  const adopt = useCallback((next: McpServer[]) => {
    setServers(next)
    setBaseline(selectionKey(next))
    setSelected((prev) => prev ?? next[0]?.name ?? null)
  }, [])

  // Probe availability once on mount: hide the icon when there are no MCP
  // servers (or the inventory is not reachable, e.g. unauthenticated on LAN).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/mcp/tools')
        if (!res.ok) return
        const data = await res.json()
        if (!cancelled && Array.isArray(data.servers) && data.servers.length > 0) {
          setAvailable(true)
        }
      } catch {
        // Silent: MCP management is simply unavailable.
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
      const res = await fetch('/api/mcp/tools')
      if (!res.ok) throw new Error('Failed to load MCP tools')
      const data = await res.json()
      adopt((data.servers ?? []) as McpServer[])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load MCP tools')
    } finally {
      setLoading(false)
    }
  }, [adopt])

  const openModal = useCallback(() => {
    setOpen(true)
    setConfirmMode(null)
    void fetchInventory()
  }, [fetchInventory])

  const resetAndClose = useCallback(() => {
    setOpen(false)
    setConfirmMode(null)
    setError(null)
  }, [])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (next) {
        setOpen(true)
        return
      }
      // Closing: block while rebuilding; prompt when there are unsaved changes.
      if (saving) return
      if (dirty) {
        setConfirmMode('close')
        return
      }
      resetAndClose()
    },
    [saving, dirty, resetAndClose],
  )

  const toggleServer = useCallback((name: string) => {
    setServers((prev) => prev.map((s) => (s.name === name ? { ...s, enabled: !s.enabled } : s)))
  }, [])

  const toggleTool = useCallback((server: string, tool: string) => {
    setServers((prev) =>
      prev.map((s) =>
        s.name === server
          ? { ...s, tools: s.tools.map((t) => (t.name === tool ? { ...t, enabled: !t.enabled } : t)) }
          : s,
      ),
    )
  }, [])

  const doSave = useCallback(async () => {
    setConfirmMode(null)
    setSaving(true)
    setError(null)
    try {
      const res = await fetch('/api/mcp/tools', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          servers: servers.map((s) => ({
            name: s.name,
            enabled: s.enabled,
            tools: s.tools.map((t) => ({ name: t.name, enabled: t.enabled })),
          })),
        }),
      })
      if (!res.ok) throw new Error('Failed to apply MCP tool changes')
      const data = await res.json()
      adopt((data.servers ?? []) as McpServer[])
      resetAndClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply MCP tool changes')
    } finally {
      setSaving(false)
    }
  }, [servers, adopt, resetAndClose])

  if (!available) return null

  const current = servers.find((s) => s.name === selected) ?? null

  return (
    <>
      <button
        type="button"
        onClick={openModal}
        title="Manage MCP tools (enable/disable to control token usage)"
        className={cn(
          'flex items-center gap-0.5 rounded-md border px-1.5 h-6 text-xs transition-colors',
          'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
        )}>
        <Plug className="h-3 w-3 shrink-0" />
        <span className="hidden sm:inline">MCP</span>
      </button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle>MCP Tools</DialogTitle>
            <DialogDescription>
              Enable or disable MCP servers and individual tools. Saving rebuilds the agents so the next message uses
              only the selected tools.
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <>
                {/* Left: server list */}
                <div className="w-64 shrink-0 overflow-y-auto border-r">
                  {servers.map((s) => {
                    const enabledCount = s.tools.filter((t) => t.enabled).length
                    return (
                      <div
                        key={s.name}
                        className={cn(
                          'flex items-center justify-between gap-2 border-b px-3 py-2',
                          selected === s.name && 'bg-accent',
                        )}>
                        <button
                          type="button"
                          onClick={() => setSelected(s.name)}
                          className="flex min-w-0 flex-1 flex-col items-start text-left">
                          <span className="truncate text-sm font-medium">{s.name}</span>
                          <span className="text-[11px] text-muted-foreground">
                            {s.status} - {enabledCount}/{s.tools.length} tools
                          </span>
                        </button>
                        <label className="flex shrink-0 cursor-pointer items-center" title="Enable server">
                          <input
                            type="checkbox"
                            checked={s.enabled}
                            onChange={() => toggleServer(s.name)}
                            className="h-4 w-4"
                          />
                        </label>
                      </div>
                    )
                  })}
                </div>

                {/* Right: selected server detail */}
                <div className="min-w-0 flex-1 overflow-y-auto p-4">
                  {current ? (
                    <>
                      <div className="mb-3 flex items-center justify-between">
                        <div className="min-w-0">
                          <h3 className="truncate text-sm font-semibold">{current.name}</h3>
                          <p className="text-[11px] text-muted-foreground">
                            {current.transport} - {current.status}
                          </p>
                        </div>
                        <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs">
                          <input
                            type="checkbox"
                            checked={current.enabled}
                            onChange={() => toggleServer(current.name)}
                            className="h-4 w-4"
                          />
                          Server enabled
                        </label>
                      </div>
                      {current.tools.length === 0 ? (
                        <p className="text-sm text-muted-foreground">
                          No tools reported (the server may not be connected).
                        </p>
                      ) : (
                        <ul className="space-y-1">
                          {current.tools.map((t) => (
                            <li
                              key={t.name}
                              className={cn(
                                'flex items-start gap-2 rounded-md border p-2',
                                !current.enabled && 'opacity-50',
                              )}>
                              <input
                                type="checkbox"
                                checked={t.enabled && current.enabled}
                                disabled={!current.enabled}
                                onChange={() => toggleTool(current.name, t.name)}
                                className="mt-0.5 h-4 w-4 shrink-0"
                              />
                              <div className="min-w-0">
                                <div className="font-mono text-sm">{t.name}</div>
                                {t.description && (
                                  <div className="text-[11px] text-muted-foreground">{t.description}</div>
                                )}
                              </div>
                            </li>
                          ))}
                        </ul>
                      )}
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">Select a server on the left.</p>
                  )}
                </div>
              </>
            )}

            {/* Save / rebuild overlay + confirmation */}
            {(saving || confirmMode) && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
                {saving ? (
                  <div className="flex items-center gap-2 text-sm">
                    <Loader2 className="h-5 w-5 animate-spin" />
                    Rebuilding agents...
                  </div>
                ) : (
                  <div className="w-[320px] rounded-lg border bg-background p-4 shadow-lg">
                    <p className="text-sm font-medium">
                      {confirmMode === 'save' ? 'Apply MCP tool changes?' : 'Discard unsaved changes?'}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {confirmMode === 'save'
                        ? 'This rebuilds the agents; the next message uses the selected tools.'
                        : 'Your changes have not been saved.'}
                    </p>
                    <div className="mt-3 flex justify-end gap-2">
                      <Button variant="outline" size="sm" onClick={() => setConfirmMode(null)}>
                        Cancel
                      </Button>
                      {confirmMode === 'save' ? (
                        <Button size="sm" onClick={doSave}>
                          Apply
                        </Button>
                      ) : (
                        <Button variant="destructive" size="sm" onClick={resetAndClose}>
                          Discard
                        </Button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t px-6 py-3">
            <span className="text-xs text-destructive">{error}</span>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)} disabled={saving}>
                Close
              </Button>
              <Button size="sm" onClick={() => setConfirmMode('save')} disabled={!dirty || saving}>
                Save
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
