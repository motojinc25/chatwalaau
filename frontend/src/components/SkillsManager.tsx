import { BookOpen, CircleAlert, Download, Loader2, PackageCheck, RefreshCw, Trash2, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * Skills modal (CTR-0124, FEAT-0046 + FEAT-0067, PRP-0087/PRP-0090/PRP-0165).
 *
 * The screen carries TWO orthogonal axes and renders them differently on purpose
 * (UDR-0146 D4):
 *
 *   install / uninstall  -> buttons, durable, changes what is on disk
 *   enable / disable     -> a toggle, durable since UDR-0148, changes what is
 *                           advertised to the agent
 *
 * The left pane is the SOURCE list: "Installed" (what is on this server) followed
 * by each catalog source. Selecting "Installed" shows the folder groups with the
 * per-skill enable toggles and the group bulk toggle that PRP-0087 introduced;
 * selecting a source shows what that source publishes, with install controls.
 *
 * State chips COMBINE rather than exclude -- a row can be installed, out of date
 * and locally modified at once. Two of them are worded with care: a ledger entry
 * whose folder is gone says "Missing on disk" (an ephemeral SKILLS_DIR, UDR-0143),
 * and one with no catalog row says "Not in the current catalog", never "deleted
 * upstream", because a snapshot cannot tell those apart (UDR-0147 D5).
 */

interface Skill {
  name: string
  description?: string
  enabled: boolean
  loaded?: boolean
  installed?: boolean
  source_id?: string
  catalog_id?: string
}

interface SkillGroup {
  name: string
  skills: Skill[]
}

interface LicenseInfo {
  spdx?: string
  name?: string
  url?: string
  inherited_from?: string
}

interface CatalogSkill {
  id: string
  group: string
  name: string
  description?: string
  source_id: string
  repo?: string
  repo_path?: string
  license?: LicenseInfo | null
  file_count?: number
  total_bytes?: number
  has_scripts?: boolean
  warnings?: string[]
  in_catalog: boolean
  installed: boolean
  update_available: boolean
  locally_modified: boolean
  missing: boolean
  enabled: boolean
}

interface CatalogSource {
  id: string
  display_name: string
  repo?: string
  error?: string | null
  stale?: boolean
  skill_count?: number
}

interface CatalogView {
  generated_at?: string
  install_enabled?: boolean
  demo_mode?: boolean
  available?: boolean
  skills_dir?: string
  sources?: CatalogSource[]
  skills?: CatalogSkill[]
}

type ConfirmMode = 'save' | 'close' | 'reload' | null

interface PendingAction {
  kind: 'install' | 'overwrite' | 'uninstall'
  skill: CatalogSkill
}

const INSTALLED_SOURCE_ID = 'installed'
const UNGROUPED_LABEL = 'Ungrouped'

function groupLabel(name: string): string {
  return name || UNGROUPED_LABEL
}

function selectionKey(groups: SkillGroup[]): string {
  // Stable signature of the enabled state for dirty detection.
  return JSON.stringify(
    groups.map((g) => ({
      n: g.name,
      s: g.skills.map((s) => [s.name, s.enabled] as const),
    })),
  )
}

// A skill is toggleable only when it is actually loaded into the current build
// (UDR-0068 D4). `loaded` is optional for back-compat; treat absent as loaded.
function isToggleable(s: Skill): boolean {
  return s.loaded !== false
}

function licenseLabel(license?: LicenseInfo | null): string {
  const spdx = license?.spdx?.trim()
  if (!spdx) return 'License unknown'
  return license?.inherited_from === 'skill' ? spdx : `${spdx} (repo)`
}

function formatStamp(value?: string): string {
  if (!value) return 'never'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

function errorFrom(body: unknown, fallback: string): string {
  const detail = (body as { detail?: { message?: string; error?: string } } | undefined)?.detail
  return detail?.message || detail?.error || fallback
}

function Chip({ tone, children }: { tone: 'ok' | 'warn' | 'info' | 'muted'; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        'rounded-full border px-1.5 py-px text-[10px] leading-4',
        tone === 'ok' && 'border-emerald-500/40 text-emerald-600 dark:text-emerald-500',
        tone === 'warn' && 'border-amber-500/40 text-amber-600 dark:text-amber-500',
        tone === 'info' && 'border-sky-500/40 text-sky-600 dark:text-sky-500',
        tone === 'muted' && 'border-muted-foreground/30 text-muted-foreground',
      )}>
      {children}
    </span>
  )
}

export function SkillsManager() {
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [groups, setGroups] = useState<SkillGroup[]>([])
  const [collisions, setCollisions] = useState<string[]>([])
  const [skillsDir, setSkillsDir] = useState('')
  const [baseline, setBaseline] = useState('')
  const [selected, setSelected] = useState<string>(INSTALLED_SOURCE_ID)
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [catalog, setCatalog] = useState<CatalogView | null>(null)

  const dirty = groups.length > 0 && selectionKey(groups) !== baseline
  const catalogSources = catalog?.sources ?? []
  const catalogSkills = useMemo(() => catalog?.skills ?? [], [catalog])
  const installEnabled = catalog?.available === true

  const byName = useMemo(() => {
    const map = new Map<string, CatalogSkill>()
    for (const row of catalogSkills) {
      if (row.installed) map.set(row.name, row)
    }
    return map
  }, [catalogSkills])

  // Installed-but-absent rows never appear in the on-disk inventory, so they are
  // surfaced separately at the top of the Installed pane with a Reinstall action.
  const missingRows = useMemo(() => catalogSkills.filter((s) => s.installed && s.missing), [catalogSkills])

  const adopt = useCallback((data: { groups?: SkillGroup[]; collisions?: string[]; skills_dir?: string }) => {
    const next = (data.groups ?? []) as SkillGroup[]
    setGroups(next)
    setCollisions((data.collisions ?? []) as string[])
    setSkillsDir(data.skills_dir ?? '')
    setBaseline(selectionKey(next))
  }, [])

  // Probe availability once on mount: show the icon whenever the endpoint is
  // reachable (UDR-0068 D5), even with zero skills, so Reload is reachable in the
  // bootstrap case. Hidden only when unreachable (e.g. unauthenticated on LAN).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/skills')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: Skills management is simply unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const fetchCatalog = useCallback(async () => {
    try {
      const res = await fetch('/api/skills/catalog')
      // A backend without CTR-0205 answers 404; the catalog pane then stays
      // hidden and the modal behaves exactly as it did before PRP-0165.
      if (!res.ok) {
        setCatalog(null)
        return
      }
      setCatalog((await res.json()) as CatalogView)
    } catch {
      setCatalog(null)
    }
  }, [])

  const fetchInventory = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/skills')
      if (!res.ok) throw new Error('Failed to load skills')
      adopt(await res.json())
      await fetchCatalog()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load skills')
    } finally {
      setLoading(false)
    }
  }, [adopt, fetchCatalog])

  const openModal = useCallback(() => {
    setOpen(true)
    setConfirmMode(null)
    setPending(null)
    void fetchInventory()
  }, [fetchInventory])

  const resetAndClose = useCallback(() => {
    setOpen(false)
    setConfirmMode(null)
    setPending(null)
    setError(null)
  }, [])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (next) {
        setOpen(true)
        return
      }
      // Closing: block while rebuilding; prompt when there are unsaved changes.
      if (saving || refreshing || busyId) return
      if (dirty) {
        setConfirmMode('close')
        return
      }
      resetAndClose()
    },
    [saving, refreshing, busyId, dirty, resetAndClose],
  )

  // Bulk-toggle every TOGGLEABLE skill in a group (UDR-0065 D2): not-loaded skills
  // are left untouched (they are uncheckable until Reload). If all toggleable skills
  // are enabled, disable them; otherwise enable them.
  const toggleGroup = useCallback((name: string) => {
    setGroups((prev) =>
      prev.map((g) => {
        if (g.name !== name) return g
        const toggleable = g.skills.filter(isToggleable)
        if (toggleable.length === 0) return g
        const allEnabled = toggleable.every((s) => s.enabled)
        return {
          ...g,
          skills: g.skills.map((s) => (isToggleable(s) ? { ...s, enabled: !allEnabled } : s)),
        }
      }),
    )
  }, [])

  const toggleSkill = useCallback((group: string, skill: string) => {
    setGroups((prev) =>
      prev.map((g) =>
        g.name === group
          ? { ...g, skills: g.skills.map((s) => (s.name === skill ? { ...s, enabled: !s.enabled } : s)) }
          : g,
      ),
    )
  }, [])

  const doSave = useCallback(async () => {
    setConfirmMode(null)
    setSaving(true)
    setError(null)
    try {
      const res = await fetch('/api/skills', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          groups: groups.map((g) => ({
            name: g.name,
            skills: g.skills.map((s) => ({ name: s.name, enabled: s.enabled })),
          })),
        }),
      })
      if (!res.ok) throw new Error('Failed to apply skill changes')
      adopt(await res.json())
      await fetchCatalog()
      resetAndClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply skill changes')
    } finally {
      setSaving(false)
    }
  }, [groups, adopt, fetchCatalog, resetAndClose])

  // Reload: re-read SKILL.md from disk and rebuild the agents (UDR-0068 D1/D2). Keeps
  // the modal open and refreshes the inventory so a just-added skill becomes loaded.
  const doReload = useCallback(async () => {
    setConfirmMode(null)
    setSaving(true)
    setError(null)
    try {
      const res = await fetch('/api/skills/reload', { method: 'POST' })
      if (!res.ok) throw new Error('Failed to reload skills')
      adopt(await res.json())
      await fetchCatalog()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload skills')
    } finally {
      setSaving(false)
    }
  }, [adopt, fetchCatalog])

  // Catalog refresh reaches out to every configured source, so it gets the same
  // blocking indicator a rebuild does rather than a per-row spinner.
  const doRefreshCatalog = useCallback(async () => {
    setRefreshing(true)
    setError(null)
    try {
      const res = await fetch('/api/skills/catalog/refresh', { method: 'POST' })
      if (!res.ok) throw new Error(errorFrom(await res.json().catch(() => null), 'Failed to refresh the catalog'))
      setCatalog((await res.json()) as CatalogView)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to refresh the catalog')
    } finally {
      setRefreshing(false)
    }
  }, [])

  const runInstall = useCallback(
    async (skill: CatalogSkill, force: boolean) => {
      setPending(null)
      setBusyId(skill.id)
      setError(null)
      try {
        const res = await fetch('/api/skills/install', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: skill.id, force }),
        })
        const body = await res.json().catch(() => null)
        if (!res.ok) {
          const code = (body as { detail?: { error?: string } } | null)?.detail?.error
          if (code === 'local_modifications' && !force) {
            setPending({ kind: 'overwrite', skill })
            return
          }
          throw new Error(errorFrom(body, `Failed to install ${skill.name}`))
        }
        setCatalog(body as CatalogView)
        // The install changed what is on disk, so the inventory (and therefore the
        // enable toggles) has to be re-read, not patched.
        const inv = await fetch('/api/skills')
        if (inv.ok) adopt(await inv.json())
      } catch (err) {
        setError(err instanceof Error ? err.message : `Failed to install ${skill.name}`)
      } finally {
        setBusyId(null)
      }
    },
    [adopt],
  )

  const runUninstall = useCallback(
    async (skill: CatalogSkill) => {
      setPending(null)
      setBusyId(skill.id)
      setError(null)
      try {
        const res = await fetch(`/api/skills/install/${skill.id}`, { method: 'DELETE' })
        const body = await res.json().catch(() => null)
        if (!res.ok) throw new Error(errorFrom(body, `Failed to remove ${skill.name}`))
        setCatalog(body as CatalogView)
        const inv = await fetch('/api/skills')
        if (inv.ok) adopt(await inv.json())
      } catch (err) {
        setError(err instanceof Error ? err.message : `Failed to remove ${skill.name}`)
      } finally {
        setBusyId(null)
      }
    },
    [adopt],
  )

  if (!available) return null

  const busy = saving || refreshing || busyId !== null
  const sourceRows = catalogSkills.filter((s) => s.source_id === selected && (s.in_catalog || s.installed))

  const renderChips = (row: CatalogSkill | undefined, enabled: boolean) => (
    <>
      {row?.missing && <Chip tone="warn">Missing on disk</Chip>}
      {row?.installed && !row.in_catalog && <Chip tone="warn">Not in the current catalog</Chip>}
      {row?.installed && !row.missing && <Chip tone="ok">Installed</Chip>}
      {row?.update_available && <Chip tone="info">Update available</Chip>}
      {row?.locally_modified && <Chip tone="warn">Locally modified</Chip>}
      {!enabled && <Chip tone="muted">Disabled</Chip>}
    </>
  )

  return (
    <>
      {/* Sidebar-footer launcher (CTR-0124 / CTR-0220, PRP-0185): ICON ONLY, sized and
          styled like every other entry in that row. It used to sit in the chat composer
          as an icon plus a VISIBLE "Skills" label; that is dropped -- the row is a strip
          of bare icons, and one entry carrying text would read as a different kind of
          control.

          The NAME is carried three ways, all the same string (v0.166.0, CTR-0220):
          `title` is the hover tooltip every other entry in the row has, `aria-label` is
          the button's only accessible name, and the caller repeats it as the label the
          "..." overflow menu shows. An earlier revision dropped the tooltip here while
          the neighbouring entries kept theirs, which made this one entry the only icon
          in the row that could not be identified by hovering. */}
      <button
        type="button"
        onClick={openModal}
        aria-label="Agent Skills"
        title="Agent Skills"
        className={cn(
          'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md transition-colors',
          'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
        )}>
        <BookOpen className="h-4 w-4 shrink-0" />
      </button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle>Skills</DialogTitle>
            <DialogDescription>
              Install Agent Skills from a catalog source, and enable or disable what is installed. Installing writes
              into the skills directory; enabling controls what each message advertises to the agent.
            </DialogDescription>
            {collisions.length > 0 && (
              <p className="mt-1 flex items-center gap-1.5 text-[11px] text-amber-600 dark:text-amber-500">
                <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
                Duplicate skill name(s) across groups: {collisions.join(', ')}. Only one is loaded; disabling the name
                gates all of them.
              </p>
            )}
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <>
                {/* Left: source list (Installed + each catalog source) */}
                <div className="w-64 shrink-0 overflow-y-auto border-r">
                  <button
                    type="button"
                    onClick={() => setSelected(INSTALLED_SOURCE_ID)}
                    className={cn(
                      'flex w-full flex-col items-start border-b px-3 py-2 text-left',
                      selected === INSTALLED_SOURCE_ID && 'bg-accent',
                    )}>
                    <span className="flex items-center gap-1.5 truncate text-sm font-medium">
                      <PackageCheck className="h-3.5 w-3.5 shrink-0" /> Installed
                    </span>
                    <span className="text-[11px] text-muted-foreground">
                      {groups.reduce((n, g) => n + g.skills.length, 0)} on disk
                      {missingRows.length > 0 && `, ${missingRows.length} missing`}
                    </span>
                  </button>

                  {catalogSources
                    .filter((s) => s.id !== INSTALLED_SOURCE_ID)
                    .map((s) => (
                      <button
                        key={s.id}
                        type="button"
                        onClick={() => setSelected(s.id)}
                        className={cn(
                          'flex w-full flex-col items-start border-b px-3 py-2 text-left',
                          selected === s.id && 'bg-accent',
                        )}>
                        <span className="truncate text-sm font-medium">{s.display_name || s.id}</span>
                        <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
                          {s.skill_count ?? 0} skills
                          {s.stale && (
                            <span className="text-amber-600 dark:text-amber-500" title={s.error ?? undefined}>
                              (stale)
                            </span>
                          )}
                        </span>
                      </button>
                    ))}

                  {catalogSources.length <= 1 && (
                    <p className="px-3 py-3 text-[11px] text-muted-foreground">
                      No catalog yet. Use "Refresh catalog" to fetch what the configured sources publish.
                    </p>
                  )}
                </div>

                {/* Right: the selected source */}
                <div className="min-w-0 flex-1 overflow-y-auto p-4">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold">
                        {selected === INSTALLED_SOURCE_ID
                          ? 'Installed'
                          : (catalogSources.find((s) => s.id === selected)?.display_name ?? selected)}
                      </h3>
                      <p className="text-[11px] text-muted-foreground">
                        {selected === INSTALLED_SOURCE_ID
                          ? skillsDir
                            ? `Skills directory: ${skillsDir}`
                            : 'Skills directory not configured'
                          : `Catalog as of ${formatStamp(catalog?.generated_at)}`}
                      </p>
                    </div>
                    {catalog && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={doRefreshCatalog}
                        disabled={busy || !installEnabled}
                        title={
                          installEnabled
                            ? 'Fetch what each configured source publishes'
                            : 'Installing skills is disabled in this deployment'
                        }>
                        <RefreshCw className="mr-1 h-3.5 w-3.5" /> Refresh catalog
                      </Button>
                    )}
                  </div>

                  {catalog && !installEnabled && (
                    <p className="mb-3 flex items-center gap-1.5 rounded-md border border-amber-500/40 px-2 py-1.5 text-[11px] text-amber-600 dark:text-amber-500">
                      <CircleAlert className="h-3.5 w-3.5 shrink-0" />
                      {catalog.demo_mode
                        ? 'Demo mode: installing skills is disabled.'
                        : 'Installing skills is switched off. Turn on "Skill installation" in App Settings to enable it.'}
                    </p>
                  )}

                  {selected === INSTALLED_SOURCE_ID ? (
                    <InstalledPane
                      groups={groups}
                      byName={byName}
                      missingRows={missingRows}
                      skillsDir={skillsDir}
                      busyId={busyId}
                      installEnabled={installEnabled}
                      renderChips={renderChips}
                      onToggleGroup={toggleGroup}
                      onToggleSkill={toggleSkill}
                      onReload={() => setConfirmMode('reload')}
                      onReinstall={(row) => setPending({ kind: 'install', skill: row })}
                      onUninstall={(row) => setPending({ kind: 'uninstall', skill: row })}
                    />
                  ) : (
                    <SourcePane
                      rows={sourceRows}
                      source={catalogSources.find((s) => s.id === selected)}
                      busyId={busyId}
                      installEnabled={installEnabled}
                      renderChips={renderChips}
                      onInstall={(row) => setPending({ kind: 'install', skill: row })}
                      onUninstall={(row) => setPending({ kind: 'uninstall', skill: row })}
                    />
                  )}
                </div>
              </>
            )}

            {/* Blocking indicators + confirmations */}
            {(saving || refreshing || confirmMode || pending) && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
                {saving || refreshing ? (
                  <div className="flex items-center gap-2 text-sm">
                    <Loader2 className="h-5 w-5 animate-spin" />
                    {refreshing ? 'Refreshing the catalog...' : 'Rebuilding agents...'}
                  </div>
                ) : pending ? (
                  <div className="w-[380px] rounded-lg border bg-background p-4 shadow-lg">
                    <p className="text-sm font-medium">
                      {pending.kind === 'uninstall'
                        ? `Remove ${pending.skill.name}?`
                        : pending.kind === 'overwrite'
                          ? `${pending.skill.name} has local changes. Overwrite?`
                          : `Install ${pending.skill.name}?`}
                    </p>
                    {pending.kind === 'uninstall' ? (
                      <p className="mt-1 text-xs text-muted-foreground">
                        The skill directory is deleted and the agents are rebuilt.
                      </p>
                    ) : pending.kind === 'overwrite' ? (
                      <p className="mt-1 text-xs text-muted-foreground">
                        Your local edits to this skill will be discarded and replaced with the upstream copy.
                      </p>
                    ) : (
                      <div className="mt-1 space-y-1 text-xs text-muted-foreground">
                        <p>
                          License: <span className="font-medium">{licenseLabel(pending.skill.license)}</span>
                          {pending.skill.repo ? ` from ${pending.skill.repo}` : ''}
                        </p>
                        {!pending.skill.license?.spdx && (
                          <p className="text-amber-600 dark:text-amber-500">
                            This source declares no license. Check the repository before using it.
                          </p>
                        )}
                        {pending.skill.has_scripts && (
                          <p className="text-amber-600 dark:text-amber-500">
                            This skill ships executable scripts, which the agent can run once installed.
                          </p>
                        )}
                      </div>
                    )}
                    <div className="mt-3 flex justify-end gap-2">
                      <Button variant="outline" size="sm" onClick={() => setPending(null)}>
                        Cancel
                      </Button>
                      {pending.kind === 'uninstall' ? (
                        <Button variant="destructive" size="sm" onClick={() => runUninstall(pending.skill)}>
                          Remove
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant={pending.kind === 'overwrite' ? 'destructive' : 'default'}
                          onClick={() => runInstall(pending.skill, pending.kind === 'overwrite')}>
                          {pending.kind === 'overwrite' ? 'Overwrite' : 'Install'}
                        </Button>
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="w-[340px] rounded-lg border bg-background p-4 shadow-lg">
                    <p className="text-sm font-medium">
                      {confirmMode === 'save'
                        ? 'Apply skill changes?'
                        : confirmMode === 'reload'
                          ? 'Reload skills from disk?'
                          : 'Discard unsaved changes?'}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {confirmMode === 'save'
                        ? 'This rebuilds the agents; the next message advertises the selected skills.'
                        : confirmMode === 'reload'
                          ? 'Re-reads SKILL.md from disk and rebuilds the agents. Unsaved changes are discarded.'
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
              <Button variant="ghost" size="sm" onClick={() => setConfirmMode('reload')} disabled={busy || loading}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" /> Reload
              </Button>
              <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)} disabled={busy}>
                Close
              </Button>
              <Button size="sm" onClick={() => setConfirmMode('save')} disabled={!dirty || busy}>
                Save
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}

interface PaneCommon {
  busyId: string | null
  installEnabled: boolean
  renderChips: (row: CatalogSkill | undefined, enabled: boolean) => React.ReactNode
}

function InstalledPane({
  groups,
  byName,
  missingRows,
  skillsDir,
  busyId,
  installEnabled,
  renderChips,
  onToggleGroup,
  onToggleSkill,
  onReload,
  onReinstall,
  onUninstall,
}: PaneCommon & {
  groups: SkillGroup[]
  byName: Map<string, CatalogSkill>
  missingRows: CatalogSkill[]
  skillsDir: string
  onToggleGroup: (group: string) => void
  onToggleSkill: (group: string, skill: string) => void
  onReload: () => void
  onReinstall: (row: CatalogSkill) => void
  onUninstall: (row: CatalogSkill) => void
}) {
  if (groups.length === 0 && missingRows.length === 0) {
    /* Empty state (UDR-0068 D5): nothing installed and nothing on disk yet. */
    return (
      <div className="flex flex-col items-center justify-center gap-3 p-6 text-center">
        <BookOpen className="h-8 w-8 text-muted-foreground" />
        <div className="text-sm font-medium">No skills found</div>
        <p className="max-w-md text-xs text-muted-foreground">
          Install skills from a catalog source on the left, or add skill folders (each with a{' '}
          <code className="font-mono">SKILL.md</code>) under
          {skillsDir ? <code className="mx-1 font-mono">{skillsDir}</code> : ' the skills directory '}
          and click Reload.
        </p>
        <Button variant="outline" size="sm" onClick={onReload}>
          <RefreshCw className="mr-1 h-3.5 w-3.5" /> Reload
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      {missingRows.length > 0 && (
        <section>
          <h4 className="mb-1 text-xs font-semibold text-amber-600 dark:text-amber-500">Missing on disk</h4>
          <p className="mb-2 text-[11px] text-muted-foreground">
            These skills are recorded as installed but their folders are gone. This is what an ephemeral skills
            directory looks like after a restart; reinstall them, or mount persistent storage there.
          </p>
          <ul className="space-y-1">
            {missingRows.map((row) => (
              <li key={row.id} className="flex items-start justify-between gap-2 rounded-md border p-2">
                <div className="min-w-0">
                  <div className="font-mono text-sm">{row.name}</div>
                  <div className="mt-0.5 flex flex-wrap gap-1">{renderChips(row, row.enabled)}</div>
                </div>
                <div className="flex shrink-0 gap-1">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!installEnabled || busyId !== null || !row.in_catalog}
                    onClick={() => onReinstall(row)}>
                    {busyId === row.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Download className="h-3.5 w-3.5" />
                    )}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={!installEnabled || busyId !== null}
                    onClick={() => onUninstall(row)}
                    title="Forget this skill">
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {groups.map((group) => {
        const toggleable = group.skills.filter(isToggleable)
        const toggleableEnabled = toggleable.filter((s) => s.enabled).length
        // Skills that no longer appear in the catalog come first: they are the ones
        // that will not receive updates (UDR-0147 D5).
        const ordered = [...group.skills].sort((a, b) => {
          const orphanA = byName.get(a.name)?.in_catalog === false ? 0 : 1
          const orphanB = byName.get(b.name)?.in_catalog === false ? 0 : 1
          return orphanA - orphanB || a.name.localeCompare(b.name)
        })
        return (
          <section key={group.name}>
            <div className="mb-1 flex items-center justify-between">
              <h4 className="truncate text-xs font-semibold">{groupLabel(group.name)}</h4>
              <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs">
                <input
                  type="checkbox"
                  checked={toggleable.length > 0 && toggleableEnabled === toggleable.length}
                  disabled={toggleable.length === 0}
                  ref={(el) => {
                    if (el) el.indeterminate = toggleableEnabled > 0 && toggleableEnabled < toggleable.length
                  }}
                  onChange={() => onToggleGroup(group.name)}
                  className="h-4 w-4"
                />
                Enable all
              </label>
            </div>
            <ul className="space-y-1">
              {ordered.map((s) => {
                const row = byName.get(s.name)
                const toggle = isToggleable(s)
                return (
                  <li
                    key={s.name}
                    className={cn('flex items-start gap-2 rounded-md border p-2', !toggle && 'opacity-60')}>
                    <input
                      type="checkbox"
                      checked={s.enabled}
                      disabled={!toggle}
                      onChange={() => onToggleSkill(group.name, s.name)}
                      className="mt-0.5 h-4 w-4 shrink-0"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-mono text-sm">{s.name}</span>
                        {renderChips(row, s.enabled)}
                      </div>
                      {!toggle && (
                        <div className="text-[11px] text-amber-600 dark:text-amber-500">
                          Not loaded yet -- Reload to apply.
                        </div>
                      )}
                      {row?.installed && !row.in_catalog && (
                        <div className="text-[11px] text-muted-foreground">
                          Not in the current catalog, so it will not receive updates.
                        </div>
                      )}
                      {s.description && <div className="text-[11px] text-muted-foreground">{s.description}</div>}
                    </div>
                    {row?.installed && (
                      <div className="flex shrink-0 gap-1">
                        {row.in_catalog && (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!installEnabled || busyId !== null}
                            onClick={() => onReinstall(row)}
                            title={row.update_available ? 'Update to the catalog revision' : 'Download again'}>
                            {busyId === row.id ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              <Download className="h-3.5 w-3.5" />
                            )}
                          </Button>
                        )}
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!installEnabled || busyId !== null}
                          onClick={() => onUninstall(row)}
                          title="Remove this skill">
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          </section>
        )
      })}
    </div>
  )
}

function SourcePane({
  rows,
  source,
  busyId,
  installEnabled,
  renderChips,
  onInstall,
  onUninstall,
}: PaneCommon & {
  rows: CatalogSkill[]
  source?: CatalogSource
  onInstall: (row: CatalogSkill) => void
  onUninstall: (row: CatalogSkill) => void
}) {
  if (source?.error) {
    return (
      <div className="space-y-2">
        <p className="flex items-start gap-1.5 rounded-md border border-amber-500/40 p-2 text-[11px] text-amber-600 dark:text-amber-500">
          <TriangleAlert className="mt-px h-3.5 w-3.5 shrink-0" />
          {source.error} The entries below are from the previous successful refresh.
        </p>
        <SourceRows
          rows={rows}
          busyId={busyId}
          installEnabled={installEnabled}
          renderChips={renderChips}
          onInstall={onInstall}
          onUninstall={onUninstall}
        />
      </div>
    )
  }
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing published by this source in the current catalog.</p>
  }
  return (
    <SourceRows
      rows={rows}
      busyId={busyId}
      installEnabled={installEnabled}
      renderChips={renderChips}
      onInstall={onInstall}
      onUninstall={onUninstall}
    />
  )
}

function SourceRows({
  rows,
  busyId,
  installEnabled,
  renderChips,
  onInstall,
  onUninstall,
}: PaneCommon & {
  rows: CatalogSkill[]
  onInstall: (row: CatalogSkill) => void
  onUninstall: (row: CatalogSkill) => void
}) {
  return (
    <ul className="space-y-1">
      {rows.map((row) => (
        <li key={row.id} className="flex items-start gap-2 rounded-md border p-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-sm">{row.name}</span>
              <span className="text-[10px] text-muted-foreground">{licenseLabel(row.license)}</span>
              {renderChips(row, row.enabled)}
            </div>
            {row.description && <div className="text-[11px] text-muted-foreground">{row.description}</div>}
            {row.warnings?.includes('name_mismatch') && (
              <div className="text-[11px] text-amber-600 dark:text-amber-500">
                Its declared name does not match its folder, so it may fail to load.
              </div>
            )}
          </div>
          <div className="flex shrink-0 gap-1">
            <Button
              size="sm"
              variant={row.installed ? 'outline' : 'default'}
              disabled={!installEnabled || busyId !== null}
              onClick={() => onInstall(row)}
              title={row.installed ? 'Download again' : 'Install this skill'}>
              {busyId === row.id ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <>
                  <Download className="mr-1 h-3.5 w-3.5" />
                  {row.update_available ? 'Update' : row.installed ? 'Reinstall' : 'Install'}
                </>
              )}
            </Button>
            {row.installed && (
              <Button
                size="sm"
                variant="ghost"
                disabled={!installEnabled || busyId !== null}
                onClick={() => onUninstall(row)}
                title="Remove this skill">
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        </li>
      ))}
    </ul>
  )
}
