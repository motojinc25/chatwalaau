import { BookOpen, Loader2, RefreshCw, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * Skills Management modal (CTR-0124, FEAT-0046, PRP-0087/PRP-0090, UDR-0065/UDR-0068).
 *
 * An icon in the chat input controls row opens a ~90% modal that lets the
 * operator enable/disable Agent Skills (organized by folder group) at runtime to
 * bound the per-turn advertise-block token cost. Saving updates the backend
 * in-memory override store (CTR-0123) and rebuilds the per-model agents
 * (CTR-0070); a blocking "rebuilding" indicator shows until the rebuild completes.
 *
 * PRP-0090 (UDR-0068): a Reload action re-reads SKILL.md from disk and rebuilds the
 * agents so out-of-band edits (a newly added skill folder) are picked up without a
 * restart; it is guarded by a confirmation + the same blocking indicator. A skill
 * discovered on disk but not yet in the live build (`loaded === false`) shows a
 * disabled toggle with a "Reload to apply" hint. The icon is ALWAYS shown when the
 * endpoint is reachable, and an empty state (the configured SKILLS_DIR + Reload) is
 * rendered when no skills exist yet (UDR-0068 D5).
 */

interface Skill {
  name: string
  description?: string
  enabled: boolean
  loaded?: boolean
}

interface SkillGroup {
  name: string
  skills: Skill[]
}

type ConfirmMode = 'save' | 'close' | 'reload' | null

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

export function SkillsManager() {
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [groups, setGroups] = useState<SkillGroup[]>([])
  const [collisions, setCollisions] = useState<string[]>([])
  const [skillsDir, setSkillsDir] = useState('')
  const [baseline, setBaseline] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null)

  const dirty = groups.length > 0 && selectionKey(groups) !== baseline

  const adopt = useCallback((data: { groups?: SkillGroup[]; collisions?: string[]; skills_dir?: string }) => {
    const next = (data.groups ?? []) as SkillGroup[]
    setGroups(next)
    setCollisions((data.collisions ?? []) as string[])
    setSkillsDir(data.skills_dir ?? '')
    setBaseline(selectionKey(next))
    setSelected((prev) => prev ?? next[0]?.name ?? null)
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

  const fetchInventory = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/skills')
      if (!res.ok) throw new Error('Failed to load skills')
      adopt(await res.json())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load skills')
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
      resetAndClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply skill changes')
    } finally {
      setSaving(false)
    }
  }, [groups, adopt, resetAndClose])

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
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload skills')
    } finally {
      setSaving(false)
    }
  }, [adopt])

  if (!available) return null

  const current = groups.find((g) => g.name === selected) ?? null

  return (
    <>
      <button
        type="button"
        onClick={openModal}
        title="Manage skills (enable/disable to control token usage)"
        className={cn(
          'flex items-center gap-0.5 rounded-md border px-1.5 h-6 text-xs transition-colors',
          'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
        )}>
        <BookOpen className="h-3 w-3 shrink-0" />
        <span className="hidden sm:inline">Skills</span>
      </button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle>Skills</DialogTitle>
            <DialogDescription>
              Enable or disable Agent Skills by group. Saving rebuilds the agents so the next message advertises only
              the selected skills. Use Reload to pick up skills you added or removed on disk.
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
            ) : groups.length === 0 ? (
              /* Empty state (UDR-0068 D5): no skills discovered yet. */
              <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
                <BookOpen className="h-8 w-8 text-muted-foreground" />
                <div className="text-sm font-medium">No skills found</div>
                <p className="max-w-md text-xs text-muted-foreground">
                  Add skill folders (each with a <code className="font-mono">SKILL.md</code>) under
                  {skillsDir ? <code className="mx-1 font-mono">{skillsDir}</code> : ' the skills directory '}
                  then click Reload to pick them up without restarting.
                </p>
                <Button variant="outline" size="sm" onClick={() => setConfirmMode('reload')}>
                  <RefreshCw className="mr-1 h-3.5 w-3.5" /> Reload
                </Button>
              </div>
            ) : (
              <>
                {/* Left: group list */}
                <div className="w-64 shrink-0 overflow-y-auto border-r">
                  {groups.map((g) => {
                    const enabledCount = g.skills.filter((s) => s.enabled).length
                    const toggleable = g.skills.filter(isToggleable)
                    const toggleableEnabled = toggleable.filter((s) => s.enabled).length
                    return (
                      <div
                        key={g.name}
                        className={cn(
                          'flex items-center justify-between gap-2 border-b px-3 py-2',
                          selected === g.name && 'bg-accent',
                        )}>
                        <button
                          type="button"
                          onClick={() => setSelected(g.name)}
                          className="flex min-w-0 flex-1 flex-col items-start text-left">
                          <span className="truncate text-sm font-medium">{groupLabel(g.name)}</span>
                          <span className="text-[11px] text-muted-foreground">
                            {enabledCount}/{g.skills.length} skills
                          </span>
                        </button>
                        <label className="flex shrink-0 cursor-pointer items-center" title="Enable all skills in group">
                          <input
                            type="checkbox"
                            checked={toggleable.length > 0 && toggleableEnabled === toggleable.length}
                            disabled={toggleable.length === 0}
                            ref={(el) => {
                              if (el) el.indeterminate = toggleableEnabled > 0 && toggleableEnabled < toggleable.length
                            }}
                            onChange={() => toggleGroup(g.name)}
                            className="h-4 w-4"
                          />
                        </label>
                      </div>
                    )
                  })}
                </div>

                {/* Right: selected group detail */}
                <div className="min-w-0 flex-1 overflow-y-auto p-4">
                  {current ? (
                    <>
                      <div className="mb-3 flex items-center justify-between">
                        <div className="min-w-0">
                          <h3 className="truncate text-sm font-semibold">{groupLabel(current.name)}</h3>
                          <p className="text-[11px] text-muted-foreground">
                            {current.skills.filter((s) => s.enabled).length}/{current.skills.length} skills enabled
                          </p>
                        </div>
                        <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs">
                          <input
                            type="checkbox"
                            checked={
                              current.skills.filter(isToggleable).length > 0 &&
                              current.skills.filter(isToggleable).every((s) => s.enabled)
                            }
                            disabled={current.skills.filter(isToggleable).length === 0}
                            ref={(el) => {
                              if (el) {
                                const t = current.skills.filter(isToggleable)
                                const c = t.filter((s) => s.enabled).length
                                el.indeterminate = c > 0 && c < t.length
                              }
                            }}
                            onChange={() => toggleGroup(current.name)}
                            className="h-4 w-4"
                          />
                          Enable all
                        </label>
                      </div>
                      {current.skills.length === 0 ? (
                        <p className="text-sm text-muted-foreground">No skills in this group.</p>
                      ) : (
                        <ul className="space-y-1">
                          {current.skills.map((s) => {
                            const toggleable = isToggleable(s)
                            return (
                              <li
                                key={s.name}
                                className={cn(
                                  'flex items-start gap-2 rounded-md border p-2',
                                  !toggleable && 'opacity-60',
                                )}>
                                <input
                                  type="checkbox"
                                  checked={s.enabled}
                                  disabled={!toggleable}
                                  onChange={() => toggleSkill(current.name, s.name)}
                                  className="mt-0.5 h-4 w-4 shrink-0"
                                />
                                <div className="min-w-0">
                                  <div className="font-mono text-sm">{s.name}</div>
                                  {!toggleable && (
                                    <div className="text-[11px] text-amber-600 dark:text-amber-500">
                                      Not loaded yet -- Reload to apply.
                                    </div>
                                  )}
                                  {s.description && (
                                    <div className="text-[11px] text-muted-foreground">{s.description}</div>
                                  )}
                                </div>
                              </li>
                            )
                          })}
                        </ul>
                      )}
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">Select a group on the left.</p>
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
