import { Loader2, Plug, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * MCP Tool Management modal (CTR-0122, FEAT-0045, PRP-0086/PRP-0090, UDR-0064/UDR-0068).
 *
 * An icon in the chat input controls row opens a ~90% modal that lets the
 * operator enable/disable MCP servers and individual MCP tools at runtime to
 * bound per-turn tool-schema token cost. Saving updates the backend in-memory
 * override store (CTR-0121) and rebuilds the per-model agents (CTR-0070); a
 * blocking "rebuilding" indicator shows until the rebuild completes.
 *
 * PRP-0090 (UDR-0068): a Reload action re-parses mcp_servers.jsonc, reconnects the
 * servers (new-before-teardown), and rebuilds the agents so out-of-band config edits
 * are picked up without a restart; it is guarded by a confirmation + the same
 * blocking indicator. A server/tool that is configured but not currently connected
 * (`loaded === false`) shows a disabled toggle with a "Reload to apply" hint. The
 * icon is ALWAYS shown when the endpoint is reachable, and an empty state (the MCP
 * config path + Reload) is rendered when no servers are configured (UDR-0068 D5).
 */

interface McpTool {
  name: string
  description?: string
  enabled: boolean
  loaded?: boolean
}

interface McpServer {
  name: string
  transport: string
  status: string
  enabled: boolean
  loaded?: boolean
  tools: McpTool[]
}

type ConfirmMode = 'save' | 'close' | 'reload' | null

// A server is toggleable only when it is actually connected (UDR-0068 D4). `loaded`
// is optional for back-compat; treat absent as loaded.
function isLoaded(s: { loaded?: boolean }): boolean {
  return s.loaded !== false
}

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
  const [configPath, setConfigPath] = useState('')
  const [baseline, setBaseline] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null)

  const dirty = servers.length > 0 && selectionKey(servers) !== baseline

  const adopt = useCallback((data: { servers?: McpServer[]; config_path?: string }) => {
    const next = (data.servers ?? []) as McpServer[]
    setServers(next)
    setConfigPath(data.config_path ?? '')
    setBaseline(selectionKey(next))
    setSelected((prev) => prev ?? next[0]?.name ?? null)
  }, [])

  // Probe availability once on mount: show the icon whenever the endpoint is
  // reachable (UDR-0068 D5), even with zero servers, so Reload is reachable in the
  // bootstrap case. Hidden only when unreachable (e.g. unauthenticated on LAN).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/mcp/tools')
        if (!cancelled && res.ok) setAvailable(true)
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
      adopt(await res.json())
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
      adopt(await res.json())
      resetAndClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply MCP tool changes')
    } finally {
      setSaving(false)
    }
  }, [servers, adopt, resetAndClose])

  // Reload: re-parse the config, reconnect servers, and rebuild the agents
  // (UDR-0068 D1/D3). Keeps the modal open and refreshes the inventory.
  const doReload = useCallback(async () => {
    setConfirmMode(null)
    setSaving(true)
    setError(null)
    try {
      const res = await fetch('/api/mcp/reload', { method: 'POST' })
      if (!res.ok) throw new Error('Failed to reload MCP tools')
      adopt(await res.json())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload MCP tools')
    } finally {
      setSaving(false)
    }
  }, [adopt])

  if (!available) return null

  const current = servers.find((s) => s.name === selected) ?? null
  const currentLoaded = current ? isLoaded(current) : false

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
              only the selected tools. Use Reload to reconnect servers after editing the config.
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : servers.length === 0 ? (
              /* Empty state (UDR-0068 D5): no MCP servers configured/connected yet. */
              <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
                <Plug className="h-8 w-8 text-muted-foreground" />
                <div className="text-sm font-medium">No MCP servers</div>
                <p className="max-w-md text-xs text-muted-foreground">
                  Add servers to
                  {configPath ? <code className="mx-1 font-mono">{configPath}</code> : ' the MCP config '}
                  then click Reload to connect them without restarting.
                </p>
                <Button variant="outline" size="sm" onClick={() => setConfirmMode('reload')}>
                  <RefreshCw className="mr-1 h-3.5 w-3.5" /> Reload
                </Button>
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
                        <label
                          className="flex shrink-0 cursor-pointer items-center"
                          title={isLoaded(s) ? 'Enable server' : 'Not connected -- Reload to apply'}>
                          <input
                            type="checkbox"
                            checked={s.enabled && isLoaded(s)}
                            disabled={!isLoaded(s)}
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
                            checked={current.enabled && currentLoaded}
                            disabled={!currentLoaded}
                            onChange={() => toggleServer(current.name)}
                            className="h-4 w-4"
                          />
                          Server enabled
                        </label>
                      </div>
                      {!currentLoaded ? (
                        <p className="text-sm text-amber-600 dark:text-amber-500">
                          This server is configured but not connected. Click Reload to connect it.
                        </p>
                      ) : current.tools.length === 0 ? (
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
                                checked={t.enabled && current.enabled && isLoaded(t)}
                                disabled={!current.enabled || !isLoaded(t)}
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

            {/* Save / reload / rebuild overlay + confirmation */}
            {(saving || confirmMode) && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
                {saving ? (
                  <div className="flex items-center gap-2 text-sm">
                    <Loader2 className="h-5 w-5 animate-spin" />
                    Rebuilding agents...
                  </div>
                ) : (
                  <div className="w-[340px] rounded-lg border bg-background p-4 shadow-lg">
                    <p className="text-sm font-medium">
                      {confirmMode === 'save'
                        ? 'Apply MCP tool changes?'
                        : confirmMode === 'reload'
                          ? 'Reload MCP servers?'
                          : 'Discard unsaved changes?'}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {confirmMode === 'save'
                        ? 'This rebuilds the agents; the next message uses the selected tools.'
                        : confirmMode === 'reload'
                          ? 'Re-parses the config, reconnects servers, and rebuilds the agents. Unsaved changes are discarded.'
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
                      ) : confirmMode === 'reload' ? (
                        <Button size="sm" onClick={doReload}>
                          Reload
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
              <Button variant="ghost" size="sm" onClick={() => setConfirmMode('reload')} disabled={saving || loading}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" /> Reload
              </Button>
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
