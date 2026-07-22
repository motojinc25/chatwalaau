import Editor from '@monaco-editor/react'
import {
  applyNodeChanges,
  Background,
  Handle,
  MarkerType,
  type Node,
  type NodeChange,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  ArrowDown,
  ArrowUp,
  GitBranch,
  Loader2,
  MessageSquare,
  Play,
  Plus,
  Repeat,
  Square,
  TriangleAlert,
  Variable,
  Workflow as WorkflowIcon,
  X,
} from 'lucide-react'
import { memo, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import '@/lib/monaco-setup'
import {
  useWorkflowAuthoring,
  type WorkflowAction,
  type WorkflowDocument,
  type WorkflowValidationResult,
} from '@/hooks/useWorkflowAuthoring'
import { cn } from '@/lib/utils'

/**
 * Declarative Workflow Editor (CTR-0184, FEAT-0062, PRP-0118, UDR-0101 D9).
 *
 * A full-screen React-Flow-PRIMARY DAG editor. CENTER: a React Flow canvas rendering
 * the workflow's actions as a top-to-bottom chain (nodes = actions / agent
 * invocations, edges = sequential flow). LEFT: workflow-level fields + an ordered
 * action list where each action is configured in a form (an InvokeAzureAgent node
 * picks a kind:Prompt agent). RIGHT: a monaco pane showing the backend-canonical YAML
 * (live preview) with a raw-edit escape hatch. Validation + serialization are the
 * backend's (UDR-0101 D9): the editor sends a structured `document` (or raw `yaml`)
 * to /api/workflows/authoring/validate and renders the returned canonical YAML +
 * warnings (a warning blocks activation).
 */

const CONTROL =
  'w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-ring'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create a new workflow; otherwise the id of the workflow being edited. */
  editId: string | null
  onSaved: (id?: string) => void
}

const ACTION_KINDS = [
  { kind: 'SendActivity', label: 'Send message', icon: MessageSquare },
  { kind: 'InvokeAzureAgent', label: 'Invoke agent', icon: Play },
  { kind: 'SetValue', label: 'Set value', icon: Variable },
  { kind: 'If', label: 'Condition (If)', icon: GitBranch },
  { kind: 'Foreach', label: 'Foreach', icon: Repeat },
  { kind: 'EndWorkflow', label: 'End workflow', icon: Square },
] as const

function emptyDocument(): WorkflowDocument {
  return { name: '', displayName: '', description: '', maxTurns: null, actions: [] }
}

function newAction(kind: string, index: number): WorkflowAction {
  const id = `${kind.toLowerCase()}_${index}`
  switch (kind) {
    case 'SendActivity':
      return { kind, id, activity: { text: '' } }
    case 'InvokeAzureAgent':
      return { kind, id, agentName: '' }
    case 'SetValue':
      return { kind, id, path: '', value: '' }
    case 'If':
      return { kind, id, condition: '', actions: [] }
    case 'Foreach':
      return { kind, id, source: '', actions: [] }
    default:
      return { kind, id }
  }
}

// ---- React Flow node (one per action) --------------------------------------
interface StepData extends Record<string, unknown> {
  label: string
  sub?: string
  index: number
  selected: boolean
  onSelect: () => void
}

const StepNode = memo(({ data }: NodeProps<Node<StepData>>) => (
  <button
    type="button"
    onClick={data.onSelect}
    className={cn(
      'flex min-w-[160px] items-center gap-2 rounded-md border px-3 py-2 text-left text-xs shadow-sm',
      data.selected ? 'border-primary bg-primary/10' : 'border-border bg-background',
    )}>
    <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
    <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded bg-muted text-[10px] font-semibold">
      {data.index + 1}
    </span>
    <span className="min-w-0">
      <span className="block max-w-[180px] truncate font-medium">{data.label}</span>
      {data.sub && <span className="block max-w-[180px] truncate text-[10px] text-muted-foreground">{data.sub}</span>}
    </span>
  </button>
))
StepNode.displayName = 'StepNode'

const nodeTypes = { step: StepNode }

function actionSummary(a: WorkflowAction): string {
  if (a.kind === 'SendActivity') return a.activity?.text ? String(a.activity.text).slice(0, 40) : 'message'
  if (a.kind === 'InvokeAzureAgent') return a.agentName ? String(a.agentName) : 'no agent'
  if (a.kind === 'SetValue') return a.path ? String(a.path) : 'variable'
  if (a.kind === 'If') return a.condition ? String(a.condition).slice(0, 40) : 'condition'
  if (a.kind === 'Foreach') return a.source ? String(a.source) : 'source'
  return a.id ? String(a.id) : ''
}

export function DeclarativeWorkflowEditor({ open, onOpenChange, editId, onSaved }: Props) {
  const api = useWorkflowAuthoring()
  const [doc, setDoc] = useState<WorkflowDocument>(emptyDocument)
  const [rawMode, setRawMode] = useState(false)
  const [rawYaml, setRawYaml] = useState('')
  const [agentNames, setAgentNames] = useState<string[]>([])
  const [validation, setValidation] = useState<WorkflowValidationResult | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [leaveConfirm, setLeaveConfirm] = useState(false)

  // ---- initial load: Prompt agent names + source for edit ----
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setDirty(false)
    setRawMode(false)
    setSelected(null)
    ;(async () => {
      try {
        const agentsRes = await fetch('/api/agents').then((r) => (r.ok ? r.json() : { agents: [] }))
        if (!cancelled) {
          const names = (agentsRes.agents ?? [])
            .filter((a: { source?: string }) => a.source !== 'core')
            .map((a: { name?: string }) => a.name)
            .filter(Boolean) as string[]
          // CORE is invokable too (by its display name).
          const core = (agentsRes.agents ?? []).find((a: { source?: string }) => a.source === 'core') as
            | { name?: string }
            | undefined
          setAgentNames(core?.name ? [core.name, ...names] : names)
        }
        if (editId) {
          const src = await api.loadSource(editId)
          if (cancelled) return
          setDoc({ ...emptyDocument(), ...src.document, actions: src.document.actions ?? [] })
          setRawYaml(src.yaml)
        } else {
          setDoc(emptyDocument())
          setRawYaml('')
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load editor')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, editId, api])

  // ---- debounced validation (single source of truth: the backend) ----
  const validateTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!open || loading) return
    if (validateTimer.current) clearTimeout(validateTimer.current)
    validateTimer.current = setTimeout(async () => {
      try {
        const body = rawMode ? { yaml: rawYaml } : { document: doc }
        setValidation(await api.validate(body))
      } catch (err) {
        setValidation({
          valid: false,
          error: err instanceof Error ? err.message : 'Validation failed',
          warnings: [],
          yaml: null,
        })
      }
    }, 400)
    return () => {
      if (validateTimer.current) clearTimeout(validateTimer.current)
    }
  }, [doc, rawYaml, rawMode, open, loading, api])

  const patch = useCallback((p: Partial<WorkflowDocument>) => {
    setDoc((d) => ({ ...d, ...p }))
    setDirty(true)
  }, [])

  const patchAction = useCallback((index: number, p: Partial<WorkflowAction>) => {
    setDoc((d) => ({ ...d, actions: d.actions.map((a, i) => (i === index ? { ...a, ...p } : a)) }))
    setDirty(true)
  }, [])

  const addAction = useCallback(
    (kind: string) => {
      setDoc((d) => {
        const next = [...d.actions, newAction(kind, d.actions.length + 1)]
        return { ...d, actions: next }
      })
      setDirty(true)
      setShowAdd(false)
      setSelected(doc.actions.length)
    },
    [doc.actions.length],
  )

  const removeAction = useCallback((index: number) => {
    setDoc((d) => ({ ...d, actions: d.actions.filter((_, i) => i !== index) }))
    setSelected(null)
    setDirty(true)
  }, [])

  const moveAction = useCallback((index: number, dir: -1 | 1) => {
    setDoc((d) => {
      const next = [...d.actions]
      const j = index + dir
      if (j < 0 || j >= next.length) return d
      ;[next[index], next[j]] = [next[j], next[index]]
      return { ...d, actions: next }
    })
    setDirty(true)
  }, [])

  // ---- canvas nodes / edges (vertical chain) ----
  const [nodes, setNodes] = useState<Array<Node<StepData>>>([])
  useEffect(() => {
    setNodes((prev) => {
      const posById = new Map(prev.map((n) => [n.id, n.position]))
      return doc.actions.map((a, i) => {
        const id = `n-${i}`
        return {
          id,
          type: 'step',
          position: posById.get(id) ?? { x: 120, y: 20 + i * 90 },
          data: {
            label: a.kind,
            sub: actionSummary(a),
            index: i,
            selected: selected === i,
            onSelect: () => setSelected(i),
          },
        }
      })
    })
  }, [doc.actions, selected])

  const onNodesChange = useCallback((changes: Array<NodeChange<Node<StepData>>>) => {
    setNodes((nds) => applyNodeChanges(changes, nds))
  }, [])

  const edges = useMemo(
    () =>
      doc.actions.slice(0, -1).map((_, i) => ({
        id: `e-${i}`,
        source: `n-${i}`,
        target: `n-${i + 1}`,
        markerEnd: { type: MarkerType.ArrowClosed },
      })),
    [doc.actions],
  )

  // ---- save / close ----
  const handleSave = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      const body = rawMode ? { yaml: rawYaml } : { document: doc, name: doc.name }
      const result = await api.save(body, editId)
      setDirty(false)
      onSaved(result.id ?? editId ?? undefined)
      onOpenChange(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save workflow')
    } finally {
      setSaving(false)
    }
  }, [rawMode, rawYaml, doc, editId, api, onSaved, onOpenChange])

  const requestClose = useCallback(() => {
    if (dirty) {
      setLeaveConfirm(true)
      return
    }
    onOpenChange(false)
  }, [dirty, onOpenChange])

  const warnings = validation?.warnings ?? []
  const validationError = validation?.error ?? null
  const canSave =
    !saving && !loading && (rawMode ? rawYaml.trim().length > 0 : doc.name.trim().length > 0) && !validationError

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(true) : requestClose())}>
      <DialogContent className="flex h-screen w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-background p-0 sm:rounded-none [&>button]:hidden">
        <DialogTitle className="sr-only">
          {editId ? 'Edit declarative workflow' : 'Create declarative workflow'}
        </DialogTitle>
        <DialogDescription className="sr-only">
          Compose a declarative workflow graph: sequential actions and agent invocations.
        </DialogDescription>
        <div className="flex shrink-0 items-center justify-between border-b px-5 py-3">
          <div className="flex items-center gap-2">
            <WorkflowIcon className="h-5 w-5 text-primary" />
            <div>
              <div className="text-sm font-semibold">{editId ? 'Edit workflow' : 'Create workflow'}</div>
              <div className="text-[11px] text-muted-foreground">
                A workflow orchestrates declarative Prompt agents. Credentials and sampling are resolved by ChatWalaʻau.
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <input type="checkbox" checked={rawMode} onChange={(e) => setRawMode(e.target.checked)} />
              Edit raw YAML
            </label>
            <Button variant="outline" size="sm" onClick={requestClose} disabled={saving}>
              Close
            </Button>
            <Button size="sm" onClick={handleSave} disabled={!canSave}>
              {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              {editId ? 'Save' : 'Create'}
            </Button>
          </div>
        </div>

        {loading ? (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
          </div>
        ) : (
          <PanelGroup direction="horizontal" className="min-h-0 flex-1">
            {/* LEFT: workflow fields + action list + selected-action form */}
            <Panel defaultSize={34} minSize={22}>
              <div className={cn('h-full space-y-3 overflow-y-auto p-4', rawMode && 'pointer-events-none opacity-50')}>
                <Field label="Name (identifier)">
                  <input
                    className={CONTROL}
                    value={doc.name}
                    onChange={(e) => patch({ name: e.target.value })}
                    placeholder="TriageFlow"
                  />
                </Field>
                <Field label="Display name (optional)">
                  <input
                    className={CONTROL}
                    value={doc.displayName ?? ''}
                    onChange={(e) => patch({ displayName: e.target.value })}
                    placeholder="Triage Flow"
                  />
                </Field>
                <Field label="Description">
                  <textarea
                    className={cn(CONTROL, 'h-14 resize-none')}
                    value={doc.description ?? ''}
                    onChange={(e) => patch({ description: e.target.value })}
                  />
                </Field>
                <Field label="Max turns (optional)">
                  <input
                    className={CONTROL}
                    type="number"
                    min={1}
                    value={doc.maxTurns ?? ''}
                    onChange={(e) => patch({ maxTurns: e.target.value ? Number(e.target.value) : null })}
                  />
                </Field>

                <div className="flex items-center justify-between pt-1">
                  <span className="text-[11px] font-medium text-muted-foreground">Actions</span>
                  <div className="relative">
                    <Button variant="outline" size="sm" onClick={() => setShowAdd((s) => !s)}>
                      <Plus className="mr-1 h-3.5 w-3.5" /> Add
                    </Button>
                    {showAdd && (
                      <>
                        <div className="fixed inset-0 z-10" onClick={() => setShowAdd(false)} aria-hidden />
                        <div className="absolute right-0 top-full z-20 mt-1 w-48 rounded-md border bg-background p-1 shadow-lg">
                          {ACTION_KINDS.map(({ kind, label, icon: Icon }) => (
                            <button
                              key={kind}
                              type="button"
                              onClick={() => addAction(kind)}
                              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent">
                              <Icon className="h-3.5 w-3.5 text-muted-foreground" /> {label}
                            </button>
                          ))}
                        </div>
                      </>
                    )}
                  </div>
                </div>

                {doc.actions.length === 0 && (
                  <p className="text-[11px] text-muted-foreground">No actions yet. Add a step to begin.</p>
                )}
                <div className="space-y-1">
                  {doc.actions.map((a, i) => (
                    <div
                      key={a.id ?? i}
                      className={cn('rounded-md border', selected === i ? 'border-primary' : 'border-border')}>
                      <div className="flex items-center gap-2 px-2 py-1.5 text-xs">
                        <button
                          type="button"
                          onClick={() => setSelected(selected === i ? null : i)}
                          className="flex min-w-0 flex-1 items-center gap-2 text-left">
                          <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-muted text-[10px]">
                            {i + 1}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate font-medium">{a.kind}</span>
                            <span className="block truncate text-[10px] text-muted-foreground">{actionSummary(a)}</span>
                          </span>
                        </button>
                        <div className="flex shrink-0 items-center gap-0.5">
                          <button
                            type="button"
                            aria-label="Move up"
                            className="rounded p-0.5 text-muted-foreground hover:bg-accent"
                            onClick={() => moveAction(i, -1)}>
                            <ArrowUp className="h-3 w-3" />
                          </button>
                          <button
                            type="button"
                            aria-label="Move down"
                            className="rounded p-0.5 text-muted-foreground hover:bg-accent"
                            onClick={() => moveAction(i, 1)}>
                            <ArrowDown className="h-3 w-3" />
                          </button>
                          <button
                            type="button"
                            aria-label="Remove action"
                            className="rounded p-0.5 text-muted-foreground hover:text-destructive"
                            onClick={() => removeAction(i)}>
                            <X className="h-3 w-3" />
                          </button>
                        </div>
                      </div>
                      {selected === i && (
                        <div className="space-y-2 border-t p-2">
                          <ActionForm action={a} agentNames={agentNames} onChange={(p) => patchAction(i, p)} />
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </Panel>

            <PanelResizeHandle className="w-1 bg-border data-[resize-handle-state=drag]:bg-primary" />

            {/* CENTER: React Flow chain */}
            <Panel defaultSize={40} minSize={24}>
              <div className="h-full">
                <ReactFlowProvider>
                  <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    nodeTypes={nodeTypes}
                    onNodesChange={onNodesChange}
                    nodesConnectable={false}
                    fitView
                    minZoom={0.2}
                    proOptions={{ hideAttribution: true }}>
                    <Background gap={16} />
                  </ReactFlow>
                </ReactFlowProvider>
              </div>
            </Panel>

            <PanelResizeHandle className="w-1 bg-border data-[resize-handle-state=drag]:bg-primary" />

            {/* RIGHT: monaco YAML + warnings */}
            <Panel defaultSize={26} minSize={16}>
              <div className="flex h-full flex-col">
                <div className="flex items-center justify-between border-b px-3 py-1.5 text-[11px] text-muted-foreground">
                  <span>{rawMode ? 'YAML (editing)' : 'YAML (canonical preview)'}</span>
                </div>
                <div className="min-h-0 flex-1">
                  <Editor
                    language="yaml"
                    theme="vs"
                    value={rawMode ? rawYaml : (validation?.yaml ?? '')}
                    onChange={(v) => {
                      if (rawMode) {
                        setRawYaml(v ?? '')
                        setDirty(true)
                      }
                    }}
                    options={{
                      readOnly: !rawMode,
                      minimap: { enabled: false },
                      fontSize: 12,
                      automaticLayout: true,
                      scrollBeyondLastLine: false,
                      tabSize: 2,
                      wordWrap: 'on',
                    }}
                  />
                </div>
                {(validationError || warnings.length > 0) && (
                  <div className="max-h-48 shrink-0 overflow-y-auto border-t bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
                    {validationError && (
                      <div className="mb-1 flex items-start gap-1.5 font-medium">
                        <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {validationError}
                      </div>
                    )}
                    {warnings.length > 0 && (
                      <>
                        <div className="mb-1 flex items-center gap-1.5 font-medium">
                          <TriangleAlert className="h-3.5 w-3.5 shrink-0" /> Resolve before running:
                        </div>
                        <ul className="space-y-0.5 pl-5">
                          {warnings.map((w) => (
                            <li key={w} className="list-disc">
                              {w}
                            </li>
                          ))}
                        </ul>
                      </>
                    )}
                  </div>
                )}
              </div>
            </Panel>
          </PanelGroup>
        )}

        {error && <div className="shrink-0 border-t bg-destructive/10 px-4 py-2 text-xs text-destructive">{error}</div>}

        {leaveConfirm && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-background/80">
            <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
              <p className="text-sm font-medium">Discard unsaved changes?</p>
              <p className="mt-1 text-xs text-muted-foreground">Your edits to this workflow have not been saved.</p>
              <div className="mt-3 flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setLeaveConfirm(false)}>
                  Keep editing
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => {
                    setLeaveConfirm(false)
                    onOpenChange(false)
                  }}>
                  Discard
                </Button>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <span className="mb-1 block text-[11px] font-medium text-muted-foreground">{label}</span>
      {children}
    </div>
  )
}

function ActionForm({
  action,
  agentNames,
  onChange,
}: {
  action: WorkflowAction
  agentNames: string[]
  onChange: (p: Partial<WorkflowAction>) => void
}) {
  if (action.kind === 'SendActivity') {
    return (
      <Field label="Message text">
        <textarea
          className={cn(CONTROL, 'h-16 resize-none text-xs')}
          value={action.activity?.text ?? ''}
          onChange={(e) => onChange({ activity: { text: e.target.value } })}
        />
      </Field>
    )
  }
  if (action.kind === 'InvokeAzureAgent') {
    return (
      <Field label="Agent (declarative Prompt agent)">
        <select
          className={CONTROL}
          value={action.agentName ?? ''}
          onChange={(e) => onChange({ agentName: e.target.value })}>
          <option value="">Select an agent...</option>
          {agentNames.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </Field>
    )
  }
  if (action.kind === 'SetValue') {
    return (
      <div className="space-y-2">
        <Field label="Path">
          <input
            className={CONTROL}
            value={action.path ?? ''}
            onChange={(e) => onChange({ path: e.target.value })}
            placeholder="turn.count"
          />
        </Field>
        <Field label="Value">
          <input
            className={CONTROL}
            value={typeof action.value === 'string' ? action.value : String(action.value ?? '')}
            onChange={(e) => onChange({ value: e.target.value })}
          />
        </Field>
      </div>
    )
  }
  if (action.kind === 'If') {
    return (
      <Field label="Condition (PowerFx expression)">
        <input
          className={CONTROL}
          value={action.condition ?? ''}
          onChange={(e) => onChange({ condition: e.target.value })}
          placeholder="=turn.count > 0"
        />
        <p className="mt-1 text-[10px] text-muted-foreground">Edit branch actions in the raw YAML pane.</p>
      </Field>
    )
  }
  if (action.kind === 'Foreach') {
    return (
      <Field label="Source (collection expression)">
        <input
          className={CONTROL}
          value={action.source ?? ''}
          onChange={(e) => onChange({ source: e.target.value })}
          placeholder="=items"
        />
        <p className="mt-1 text-[10px] text-muted-foreground">Edit loop-body actions in the raw YAML pane.</p>
      </Field>
    )
  }
  return <p className="text-[11px] text-muted-foreground">This action has no configurable fields.</p>
}
