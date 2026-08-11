import {
  Bot,
  CircleCheck,
  Hammer,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  TriangleAlert,
  Workflow as WorkflowIcon,
} from 'lucide-react'
import { Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { type HarnessEntry, type HarnessPolicy, useHarnessAuthoring } from '@/hooks/useHarnessAuthoring'
import { useWorkflowAuthoring, type WorkflowEntry } from '@/hooks/useWorkflowAuthoring'
import { lazyWithReload } from '@/lib/lazy-with-reload'
import {
  getHarnessRunTarget,
  getWorkflowRunTarget,
  RUN_TARGET_CHANGED_EVENT,
  setHarnessRunTarget,
  setWorkflowRunTarget,
} from '@/lib/runTarget'
import { cn } from '@/lib/utils'

// Heavy editors (React Flow + monaco) are lazy so they stay out of the main bundle
// until the operator opens Create / Edit (CTR-0179 / CTR-0184 / CTR-0196).
const DeclarativeAgentEditor = lazyWithReload(() =>
  import('@/components/DeclarativeAgentEditor').then((m) => ({ default: m.DeclarativeAgentEditor })),
)
const DeclarativeWorkflowEditor = lazyWithReload(() =>
  import('@/components/DeclarativeWorkflowEditor').then((m) => ({ default: m.DeclarativeWorkflowEditor })),
)
const HarnessAgentEditor = lazyWithReload(() =>
  import('@/components/HarnessAgentEditor').then((m) => ({ default: m.HarnessAgentEditor })),
)

/**
 * Declarative Agents & Workflows management modal (CTR-0144 v4, FEAT-0051 /
 * FEAT-0062 / FEAT-0064, UDR-0072 / UDR-0101 D2 / UDR-0119 D1/D3).
 *
 * ONE modal manages declarative Prompt agents, Harness agents (PRP-0135), and
 * declarative Workflows, told apart by a Prompt / Harness / Workflow tag. Harness
 * agents live in the AGENTS section with a HARNESS tag (management grouping only,
 * UDR-0119 D3). A Prompt agent is ACTIVATED (server-side rebuild, the existing
 * flow); a Workflow OR a Harness agent is chosen as the chat RUN-TARGET
 * (client-side; at most one, mutually exclusive). The effective run-target drives
 * the next message, and the assistant message is labeled with its name. Create
 * opens the matching editor (Prompt vs Workflow vs Harness) on a separate
 * full-screen screen.
 */

interface AgentEntry {
  id: string
  name: string
  display_name?: string
  description?: string
  group_path: string[]
  source: 'core' | 'custom'
  active: boolean
  loaded: boolean
  error?: string | null
  warnings?: string[]
  editable?: boolean
  tool_allowlist?: string[] | null
}

/** Dispatched on the window after the active declarative agent changes, so the
 * model selector / options panels re-read /api/model (CTR-0144, PRP-0094). */
export const ACTIVE_AGENT_CHANGED_EVENT = 'chatwalaau:active-agent-changed'

/** Dispatched on the window to REQUEST that the Declarative Agents & Workflows modal
 * open (CTR-0144, PRP-0128, UDR-0111 D5/D6). Dispatchers: the chat composer's run-target
 * indicator (CTR-0185) and the sidebar-footer trigger below. Carries NO payload: opening
 * must not stage a selection, which in this modal is a pending activation.
 *
 * UDR-0115: this is a REQUEST, not a notification -- losing it loses the action, and a
 * window event has no failure signal. Its listener therefore lives in
 * `DeclarativeAgentManager`, which MUST be mounted somewhere no layout state can unmount
 * (see ChatPage), and MUST NOT be gated on an availability probe. */
export const OPEN_DECLARATIVE_MANAGER_EVENT = 'chatwalaau:open-declarative-manager'

/** Request the modal from anywhere. One helper so no caller has to know the seam. */
export function requestDeclarativeManager(): void {
  window.dispatchEvent(new Event(OPEN_DECLARATIVE_MANAGER_EVENT))
}

const BUILTIN_LABEL = 'Built-in'
const TOP_LEVEL_LABEL = 'Top level'

type Kind = 'Prompt' | 'Workflow' | 'Harness'
type ConfirmMode = 'activate' | 'reload' | 'delete' | null

interface Unified {
  kind: Kind
  id: string
  name: string
  display_name?: string
  description?: string
  group_path: string[]
  loaded: boolean
  error?: string | null
  warnings?: string[]
  editable?: boolean
  // Prompt
  source?: 'core' | 'custom'
  active?: boolean
  tool_allowlist?: string[] | null
  // Workflow
  referenced_agents?: string[]
  action_kinds?: string[]
  // Harness (CTR-0194, PRP-0135)
  runnable?: boolean
  policy?: HarnessPolicy | null
}

function agentGroup(entry: AgentEntry): string {
  if (entry.source === 'core') return BUILTIN_LABEL
  return entry.group_path.length ? entry.group_path.join(' / ') : TOP_LEVEL_LABEL
}

export function DeclarativeAgentManager() {
  const wfApi = useWorkflowAuthoring()
  const hApi = useHarnessAuthoring()
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [agents, setAgents] = useState<AgentEntry[]>([])
  const [workflows, setWorkflows] = useState<WorkflowEntry[]>([])
  const [harnesses, setHarnesses] = useState<HarnessEntry[]>([])
  const [activeId, setActiveId] = useState<string>('core')
  const [selected, setSelected] = useState<{ kind: Kind; id: string } | null>(null)
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null)
  const [canAuthor, setCanAuthor] = useState(false)
  const [wfCanAuthor, setWfCanAuthor] = useState(false)
  const [hCanAuthor, setHCanAuthor] = useState(false)
  const [createMenu, setCreateMenu] = useState(false)
  const [agentEditorOpen, setAgentEditorOpen] = useState(false)
  const [wfEditorOpen, setWfEditorOpen] = useState(false)
  const [hEditorOpen, setHEditorOpen] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [wfTarget, setWfTarget] = useState(() => getWorkflowRunTarget())
  const [hTarget, setHTarget] = useState(() => getHarnessRunTarget())

  useEffect(() => {
    const onRt = () => {
      setWfTarget(getWorkflowRunTarget())
      setHTarget(getHarnessRunTarget())
    }
    window.addEventListener(RUN_TARGET_CHANGED_EVENT, onRt)
    return () => window.removeEventListener(RUN_TARGET_CHANGED_EVENT, onRt)
  }, [])

  const adoptAgents = useCallback((data: { active?: string; agents?: AgentEntry[] }) => {
    setAgents((data.agents ?? []) as AgentEntry[])
    setActiveId(data.active ?? 'core')
  }, [])

  // Probe availability once on mount: GET /api/agents is always reachable.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/agents')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: management is simply unavailable.
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
      const [aRes, wRes, hRes] = await Promise.all([
        fetch('/api/agents').then((r) => (r.ok ? r.json() : { agents: [] })),
        fetch('/api/workflows').then((r) => (r.ok ? r.json() : { workflows: [] })),
        hApi.list(),
      ])
      adoptAgents(aRes)
      setWorkflows((wRes.workflows ?? []) as WorkflowEntry[])
      setHarnesses(hRes)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, [adoptAgents, hApi])

  const openModal = useCallback(() => {
    setOpen(true)
    setConfirmMode(null)
    setWfTarget(getWorkflowRunTarget())
    setHTarget(getHarnessRunTarget())
    void fetchInventory()
    void (async () => {
      try {
        const res = await fetch('/api/agents/authoring/status')
        if (res.ok) {
          const d = (await res.json()) as { available?: boolean; writable?: boolean }
          setCanAuthor(Boolean(d.available && d.writable))
        }
      } catch {
        setCanAuthor(false)
      }
      const s = await wfApi.authoringStatus()
      setWfCanAuthor(s.available && s.writable)
      const h = await hApi.authoringStatus()
      setHCanAuthor(h.available && h.writable)
    })()
  }, [fetchInventory, wfApi, hApi])

  // UDR-0111 D5/D6: the chat composer's run-target name opens THIS modal, and the
  // sidebar icon does too. The request travels on the same window seam
  // ACTIVE_AGENT_CHANGED_EVENT already uses in the opposite direction -- no state lift
  // through App. Open only: the event carries NO preselection, because a selected row
  // here is a PENDING ACTIVATION with a discard guard, and an outside surface must not
  // stage one on the operator's behalf.
  //
  // UDR-0115 D1/D2 (PRP-0134): this listener is NOT gated on `available`, and this
  // component is mounted where nothing conditional can unmount it. Both were wrong
  // before: the listener lived inside the collapsible sidebar, so closing the sidebar
  // deleted it and the composer's click was delivered to nobody -- silently, since a
  // window event has no failure signal. `available` gates the sidebar ICON (do not
  // advertise a feature that cannot be served); it must not gate RECEIPT, because the
  // composer has already shown a button and "nothing happened" teaches the operator
  // nothing.
  useEffect(() => {
    const onOpenRequest = () => openModal()
    window.addEventListener(OPEN_DECLARATIVE_MANAGER_EVENT, onOpenRequest)
    return () => window.removeEventListener(OPEN_DECLARATIVE_MANAGER_EVENT, onOpenRequest)
  }, [openModal])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (next) {
        setOpen(true)
        return
      }
      if (busy) return
      setOpen(false)
      setConfirmMode(null)
      setCreateMenu(false)
      setError(null)
    },
    [busy],
  )

  // Activation is ONE flow for both kinds (v0.112.1): confirm -> blocking "Rebuilding"
  // indicator -> apply. A Prompt agent rebuilds the per-model agents server-side; a
  // Workflow is compiled/validated (the equivalent build step) and then becomes the chat
  // run-target. Exactly one of the two is the effective run-target.
  const doActivate = useCallback(async () => {
    if (!selected) return
    setConfirmMode(null)
    setBusy(true)
    setError(null)
    try {
      if (selected.kind === 'Prompt') {
        const res = await fetch('/api/agents/active', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: selected.id }),
        })
        if (!res.ok) {
          const d = await res.json().catch(() => null)
          throw new Error(d?.detail?.message || d?.detail?.error || 'Failed to activate agent')
        }
        adoptAgents(await res.json())
        // Activating a Prompt agent makes it the effective run-target -- clear any
        // workflow / harness run-target so chat runs the agent (UDR-0101 D5, UDR-0119 D3).
        setWorkflowRunTarget(null)
        setWfTarget(null)
        setHarnessRunTarget(null)
        setHTarget(null)
        window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
      } else if (selected.kind === 'Harness') {
        // Re-validate the STORED harness agent (map + factory preflight) before it
        // becomes the run-target, so a non-runnable spec can never be selected
        // (UDR-0119 D8; the workflow precedent).
        const result = await hApi.validateStored(selected.id)
        if (!result.valid) throw new Error(result.error || 'Harness agent failed validation')
        if (result.warnings.length) throw new Error(`Resolve the warnings first: ${result.warnings[0]}`)
        const h = harnesses.find((x) => x.id === selected.id)
        if (h && h.runnable === false) throw new Error('This harness agent is not runnable (demo mode).')
        const target = { id: selected.id, name: h?.name ?? selected.id }
        // The store clears any workflow run-target (one effective axis, UDR-0119 D3).
        setHarnessRunTarget(target)
        setHTarget(target)
        setWfTarget(getWorkflowRunTarget())
      } else {
        // Compile/validate the STORED workflow (the "build" step) before it goes live,
        // so an unrunnable workflow can never become the run-target.
        const res = await fetch(`/api/workflows/${encodeURI(selected.id)}/validate`, { method: 'POST' })
        if (!res.ok) {
          const d = await res.json().catch(() => null)
          throw new Error(d?.detail?.message || d?.detail?.error || 'Workflow failed to compile')
        }
        const result = (await res.json()) as { valid: boolean; error: string | null; warnings: string[] }
        if (!result.valid) throw new Error(result.error || 'Workflow failed to compile')
        if (result.warnings.length) throw new Error(`Resolve the warnings first: ${result.warnings[0]}`)
        const w = workflows.find((x) => x.id === selected.id)
        const target = { id: selected.id, name: w?.name ?? selected.id }
        // The store clears any harness run-target (one effective axis, UDR-0119 D3).
        setWorkflowRunTarget(target)
        setWfTarget(target)
        setHTarget(null)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to activate')
    } finally {
      setBusy(false)
    }
  }, [selected, adoptAgents, workflows, harnesses, hApi])

  const doReload = useCallback(async () => {
    setConfirmMode(null)
    setBusy(true)
    setError(null)
    try {
      const res = await fetch('/api/agents/reload', { method: 'POST' })
      if (res.ok) adoptAgents(await res.json())
      await fetchInventory()
      window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload')
    } finally {
      setBusy(false)
    }
  }, [adoptAgents, fetchInventory])

  const doDelete = useCallback(async () => {
    if (!selected) return
    setConfirmMode(null)
    setBusy(true)
    setError(null)
    try {
      if (selected.kind === 'Prompt') {
        const res = await fetch(`/api/agents/authoring/${encodeURI(selected.id)}`, { method: 'DELETE' })
        if (!res.ok) {
          const d = await res.json().catch(() => null)
          throw new Error(d?.detail?.message || d?.detail?.error || 'Failed to delete agent')
        }
        adoptAgents(await res.json())
        window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
      } else if (selected.kind === 'Harness') {
        await hApi.remove(selected.id)
        if (hTarget?.id === selected.id) setHarnessRunTarget(null)
      } else {
        await wfApi.remove(selected.id)
        if (wfTarget?.id === selected.id) setWorkflowRunTarget(null)
      }
      setSelected(null)
      await fetchInventory()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete')
    } finally {
      setBusy(false)
    }
  }, [selected, adoptAgents, wfApi, wfTarget, hApi, hTarget, fetchInventory])

  const openAgentEditor = useCallback((id: string | null) => {
    setCreateMenu(false)
    setEditId(id)
    setAgentEditorOpen(true)
  }, [])
  const openWfEditor = useCallback((id: string | null) => {
    setCreateMenu(false)
    setEditId(id)
    setWfEditorOpen(true)
  }, [])
  const openHarnessEditor = useCallback((id: string | null) => {
    setCreateMenu(false)
    setEditId(id)
    setHEditorOpen(true)
  }, [])

  const onSaved = useCallback(() => {
    void fetchInventory()
    window.dispatchEvent(new Event(ACTIVE_AGENT_CHANGED_EVENT))
  }, [fetchInventory])

  // ---- unified grouping: Built-in, Agents (+ nested folders), Workflows (+ nested) ----
  // The "Built-in", "Agents" and "Workflows" sections are ALWAYS rendered (even when
  // empty) so the kinds are discoverable before anything is authored (v0.112.1).
  // Harness agents (PRP-0135, UDR-0119 D3) live in the AGENTS section, told apart by
  // their HARNESS tag -- management grouping only, not an execution-semantics change.
  const sections = useMemo(() => {
    const out: Array<{ header: string; kind: Kind; items: Unified[] }> = []
    const agentGroups = new Map<string, Unified[]>()
    for (const a of agents) {
      const label = agentGroup(a)
      const arr = agentGroups.get(label) ?? []
      arr.push({ kind: 'Prompt', ...a })
      agentGroups.set(label, arr)
    }
    for (const h of harnesses) {
      const label = h.group_path.length ? h.group_path.join(' / ') : TOP_LEVEL_LABEL
      const arr = agentGroups.get(label) ?? []
      arr.push({ ...h, kind: 'Harness' })
      agentGroups.set(label, arr)
    }
    out.push({ header: 'Built-in', kind: 'Prompt', items: agentGroups.get(BUILTIN_LABEL) ?? [] })
    out.push({ header: 'Agents', kind: 'Prompt', items: agentGroups.get(TOP_LEVEL_LABEL) ?? [] })
    for (const [label, arr] of agentGroups) {
      if (label !== BUILTIN_LABEL && label !== TOP_LEVEL_LABEL) {
        out.push({ header: `Agents · ${label}`, kind: 'Prompt', items: arr })
      }
    }
    const wfGroups = new Map<string, Unified[]>()
    for (const w of workflows) {
      const label = w.group_path.length ? w.group_path.join(' / ') : TOP_LEVEL_LABEL
      const arr = wfGroups.get(label) ?? []
      arr.push({ kind: 'Workflow', ...w })
      wfGroups.set(label, arr)
    }
    out.push({ header: 'Workflows', kind: 'Workflow', items: wfGroups.get(TOP_LEVEL_LABEL) ?? [] })
    for (const [label, arr] of wfGroups) {
      if (label !== TOP_LEVEL_LABEL) out.push({ header: `Workflows · ${label}`, kind: 'Workflow', items: arr })
    }
    return out
  }, [agents, workflows, harnesses])

  // UDR-0115 D2: render nothing only while the modal is CLOSED and the feature is
  // unavailable. Once a request has arrived the dialog must render, even if the
  // availability probe has not resolved or failed -- an open modal that reports the
  // truth beats a click that vanishes.
  if (!available && !open) return null

  const current: Unified | null =
    sections.flatMap((s) => s.items).find((e) => selected && e.kind === selected.kind && e.id === selected.id) ?? null

  const hasWarnings = (current?.warnings?.length ?? 0) > 0
  // The single effective run-target: a selected Workflow OR Harness agent wins, else
  // the active Prompt agent (one axis, UDR-0119 D3). Drives "Active" identically.
  const anyTarget = Boolean(wfTarget) || Boolean(hTarget)
  const isCurrentActive =
    current?.kind === 'Workflow'
      ? wfTarget?.id === current.id
      : current?.kind === 'Harness'
        ? hTarget?.id === current.id
        : current?.id === activeId && !anyTarget
  const canActivate =
    current !== null &&
    current.loaded === true &&
    !current.error &&
    !hasWarnings &&
    !isCurrentActive &&
    // A harness agent must be runnable (blocking warnings already excluded above;
    // this is the DEMO_MODE read-only case, UDR-0119 D7).
    (current.kind !== 'Harness' || current.runnable !== false)

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle>Declarative Agents &amp; Workflows</DialogTitle>
            <DialogDescription>
              Manage Prompt agents and Workflows in one place. Activate a Prompt agent, or select a Workflow to run in
              chat -- the assistant message shows which one produced the answer.
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <>
                {/* Left: unified list (Agents then Workflows) */}
                <div className="w-72 shrink-0 overflow-y-auto border-r">
                  {sections.map((section) => (
                    <div key={section.header}>
                      <div className="flex items-center gap-1.5 bg-muted/50 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {section.kind === 'Workflow' ? (
                          <WorkflowIcon className="h-3 w-3" />
                        ) : (
                          <Bot className="h-3 w-3" />
                        )}
                        {section.header}
                      </div>
                      {section.items.map((e) => {
                        const isSel = selected?.kind === e.kind && selected.id === e.id
                        const clean = e.loaded && (e.warnings?.length ?? 0) === 0
                        const isActive =
                          e.kind === 'Prompt'
                            ? e.active && !wfTarget && !hTarget
                            : e.kind === 'Harness'
                              ? hTarget?.id === e.id
                              : wfTarget?.id === e.id
                        return (
                          <button
                            key={`${e.kind}:${e.id}`}
                            type="button"
                            onClick={() => setSelected({ kind: e.kind, id: e.id })}
                            className={cn(
                              'flex w-full items-center gap-2 border-b px-3 py-2 text-left',
                              isSel && 'bg-accent',
                            )}>
                            <span className="flex h-4 w-4 shrink-0 items-center justify-center">
                              {isActive ? (
                                <CircleCheck className="h-4 w-4 text-primary" />
                              ) : clean ? (
                                <span className="h-2.5 w-2.5 rounded-full border" />
                              ) : (
                                <TriangleAlert className="h-3.5 w-3.5 text-amber-600 dark:text-amber-500" />
                              )}
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm font-medium">{e.name}</span>
                              {/* Line 2 is the Display name (v0.112.1); it is NOT swapped
                                  for an "Active" label -- the check icon shows active state.
                                  A load error / blocking warning still takes precedence. */}
                              <span className="block truncate text-[11px] text-muted-foreground">
                                {!e.loaded
                                  ? 'Error'
                                  : (e.warnings?.length ?? 0) > 0
                                    ? 'Needs fixing'
                                    : e.display_name || e.id}
                              </span>
                            </span>
                            <span className="shrink-0 rounded bg-muted px-1 py-0.5 text-[9px] font-medium uppercase text-muted-foreground">
                              {e.kind}
                            </span>
                          </button>
                        )
                      })}
                    </div>
                  ))}
                </div>

                {/* Right: selected detail */}
                <div className="min-w-0 flex-1 overflow-y-auto p-5">
                  {current ? (
                    <>
                      <div className="mb-2 flex items-center gap-2">
                        <h3 className="truncate text-base font-semibold">{current.name}</h3>
                        <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground">
                          {current.kind}
                        </span>
                      </div>
                      {/* Line 2 is the Display name (v0.112.1); falls back to the id. */}
                      <p className={cn('mb-1 text-[11px] text-muted-foreground', !current.display_name && 'font-mono')}>
                        {current.display_name || current.id}
                      </p>
                      {current.description && (
                        <p className="mb-3 text-sm text-muted-foreground">{current.description}</p>
                      )}

                      {current.error && (
                        <div className="mb-3 flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[12px] text-amber-700 dark:text-amber-400">
                          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                          <div className="whitespace-pre-wrap">{current.error}</div>
                        </div>
                      )}
                      {hasWarnings && (
                        <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
                          <div className="mb-1 flex items-center gap-1.5 font-medium">
                            <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
                            {current.kind === 'Prompt' ? 'Fix before activating:' : 'Resolve before running:'}
                          </div>
                          <ul className="space-y-1 pl-5">
                            {current.warnings?.map((w) => (
                              <li key={w} className="list-disc">
                                {w}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {current.kind === 'Prompt' && current.loaded && (current.tool_allowlist?.length ?? 0) > 0 && (
                        <p className="mb-3 text-[11px] text-muted-foreground">
                          Tool surface restricted to {current.tool_allowlist?.length} tool(s):{' '}
                          <span className="font-mono">{current.tool_allowlist?.join(', ')}</span>
                        </p>
                      )}
                      {current.kind === 'Workflow' && (current.referenced_agents?.length ?? 0) > 0 && (
                        <p className="mb-2 text-[11px] text-muted-foreground">
                          Invokes agent(s): <span className="font-mono">{current.referenced_agents?.join(', ')}</span>
                        </p>
                      )}
                      {current.kind === 'Workflow' && (current.action_kinds?.length ?? 0) > 0 && (
                        <p className="mb-3 text-[11px] text-muted-foreground">
                          Steps: <span className="font-mono">{current.action_kinds?.join(' -> ')}</span>
                        </p>
                      )}
                      {/* Harness resolved-policy summary (CTR-0194, UDR-0119 D5/D7). */}
                      {current.kind === 'Harness' && current.policy && (
                        <div className="mb-3 space-y-0.5 text-[11px] text-muted-foreground">
                          <p>
                            Model: <span className="font-mono">{current.policy.model || '-'}</span> · Web search:{' '}
                            {current.policy.web_search} · Loop cap: {current.policy.loop_max_iterations}
                          </p>
                          <p>
                            Workspace: file memory {current.policy.file_memory ? 'on' : 'off'} · file access{' '}
                            {current.policy.file_access ? 'on' : 'off'} · shell {current.policy.shell ? 'on' : 'off'} ·
                            skills {current.policy.skills ? 'on' : 'off'}
                          </p>
                          <p>
                            Todo {current.policy.todo ? 'on' : 'off'} · Mode{' '}
                            {current.policy.mode ? (current.policy.mode_initial ?? 'on') : 'off'} · Write approval{' '}
                            {current.policy.write_tool_approval ? 'required' : 'OFF (opt-in)'}
                          </p>
                        </div>
                      )}
                      {current.kind === 'Harness' && current.runnable === false && !hasWarnings && current.loaded && (
                        <p className="mb-3 text-[11px] text-amber-700 dark:text-amber-400">
                          Read-only here: harness agents are not runnable in demo mode.
                        </p>
                      )}

                      <div className="flex flex-wrap items-center gap-2">
                        {/* ONE activation affordance for both kinds (v0.112.1): same label,
                            same confirmation, same blocking "Rebuilding" indicator. */}
                        <Button size="sm" disabled={!canActivate} onClick={() => setConfirmMode('activate')}>
                          {isCurrentActive ? 'Active' : hasWarnings ? 'Resolve warnings to activate' : 'Activate'}
                        </Button>
                        {current.editable &&
                          (current.kind === 'Workflow'
                            ? wfCanAuthor
                            : current.kind === 'Harness'
                              ? hCanAuthor
                              : canAuthor) && (
                            <>
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() =>
                                  current.kind === 'Workflow'
                                    ? openWfEditor(current.id)
                                    : current.kind === 'Harness'
                                      ? openHarnessEditor(current.id)
                                      : openAgentEditor(current.id)
                                }>
                                <Pencil className="mr-1 h-3.5 w-3.5" /> Edit
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-destructive hover:text-destructive"
                                onClick={() => setConfirmMode('delete')}>
                                <Trash2 className="mr-1 h-3.5 w-3.5" /> Delete
                              </Button>
                            </>
                          )}
                      </div>
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">Select an agent or workflow on the left.</p>
                  )}
                </div>
              </>
            )}

            {(busy || confirmMode) && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80">
                {busy ? (
                  <div className="flex items-center gap-2 text-sm">
                    <Loader2 className="h-5 w-5 animate-spin" />
                    {confirmMode === 'delete'
                      ? 'Working...'
                      : current?.kind === 'Workflow'
                        ? 'Rebuilding workflows...'
                        : current?.kind === 'Harness'
                          ? 'Validating harness agent...'
                          : 'Rebuilding agents...'}
                  </div>
                ) : (
                  <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
                    <p className="text-sm font-medium">
                      {confirmMode === 'activate'
                        ? `Activate this ${current?.kind === 'Workflow' ? 'workflow' : current?.kind === 'Harness' ? 'harness agent' : 'agent'}?`
                        : confirmMode === 'delete'
                          ? `Delete this ${current?.kind === 'Workflow' ? 'workflow' : current?.kind === 'Harness' ? 'harness agent' : 'agent'}?`
                          : 'Reload from disk?'}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {confirmMode === 'activate'
                        ? current?.kind === 'Workflow'
                          ? 'This builds the workflow and makes it the run-target; your next message runs it instead of the active agent.'
                          : current?.kind === 'Harness'
                            ? 'This validates the harness agent and makes it the run-target; your next message runs it instead of the active agent. It can execute shell commands and write files in the workspace (with approval).'
                            : 'This rebuilds the agents; the next message, the API, and Teams use the selected agent.'
                        : confirmMode === 'delete'
                          ? 'This permanently deletes the YAML file.'
                          : 'Re-scans the declarative directory.'}
                    </p>
                    <div className="mt-3 flex justify-end gap-2">
                      <Button variant="outline" size="sm" onClick={() => setConfirmMode(null)}>
                        Cancel
                      </Button>
                      {confirmMode === 'activate' ? (
                        <Button size="sm" onClick={doActivate}>
                          Activate
                        </Button>
                      ) : confirmMode === 'delete' ? (
                        <Button variant="destructive" size="sm" onClick={doDelete}>
                          Delete
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
              {(canAuthor || wfCanAuthor || hCanAuthor) && (
                <div className="relative">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setCreateMenu((s) => !s)}
                    disabled={busy || loading}>
                    <Plus className="mr-1 h-3.5 w-3.5" /> Create
                  </Button>
                  {createMenu && (
                    <>
                      <div className="fixed inset-0 z-10" onClick={() => setCreateMenu(false)} aria-hidden />
                      <div className="absolute bottom-full right-0 z-20 mb-1 w-52 rounded-md border bg-background p-1 shadow-lg">
                        {canAuthor && (
                          <button
                            type="button"
                            onClick={() => openAgentEditor(null)}
                            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent">
                            <Bot className="h-3.5 w-3.5 text-muted-foreground" /> New Prompt agent
                          </button>
                        )}
                        {hCanAuthor && (
                          <button
                            type="button"
                            onClick={() => openHarnessEditor(null)}
                            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent">
                            <Hammer className="h-3.5 w-3.5 text-muted-foreground" /> New Harness agent
                          </button>
                        )}
                        {wfCanAuthor && (
                          <button
                            type="button"
                            onClick={() => openWfEditor(null)}
                            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent">
                            <WorkflowIcon className="h-3.5 w-3.5 text-muted-foreground" /> New Workflow
                          </button>
                        )}
                      </div>
                    </>
                  )}
                </div>
              )}
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

      {agentEditorOpen && (
        <Suspense fallback={null}>
          <DeclarativeAgentEditor
            open={agentEditorOpen}
            onOpenChange={setAgentEditorOpen}
            editId={editId}
            onSaved={onSaved}
          />
        </Suspense>
      )}
      {wfEditorOpen && (
        <Suspense fallback={null}>
          <DeclarativeWorkflowEditor
            open={wfEditorOpen}
            onOpenChange={setWfEditorOpen}
            editId={editId}
            onSaved={onSaved}
          />
        </Suspense>
      )}
      {hEditorOpen && (
        <Suspense fallback={null}>
          <HarnessAgentEditor open={hEditorOpen} onOpenChange={setHEditorOpen} editId={editId} onSaved={onSaved} />
        </Suspense>
      )}
    </>
  )
}

/**
 * Sidebar-footer trigger for the Declarative Agents & Workflows modal (CTR-0144).
 *
 * Split out from the manager in PRP-0134 (UDR-0115 D1). The manager owns the modal AND
 * the open-request listener, so it must be mounted where nothing conditional can unmount
 * it -- and the sidebar is collapsible, which is exactly what deleted the listener and
 * made the composer's button silently do nothing. What belongs in the sidebar is only
 * the icon; it now asks on the same seam every other caller uses.
 *
 * The availability probe stays HERE, because UDR-0115 D2 puts it on the ADVERTISEMENT:
 * an unconfigured deployment should not show an icon for a feature it cannot serve. It
 * must not travel with the listener.
 */
export function DeclarativeAgentManagerTrigger() {
  const [available, setAvailable] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/agents')
        if (!cancelled && res.ok) setAvailable(true)
      } catch {
        // Silent: management is simply unavailable, so no icon is shown.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (!available) return null

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-6 w-6 text-muted-foreground"
      onClick={requestDeclarativeManager}
      aria-label="Declarative agents and workflows"
      title="Declarative agents & workflows">
      <Bot className="h-4 w-4" />
    </Button>
  )
}
