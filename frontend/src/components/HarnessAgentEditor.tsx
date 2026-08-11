import Editor from '@monaco-editor/react'
import {
  applyNodeChanges,
  Background,
  BaseEdge,
  type EdgeProps,
  getStraightPath,
  Handle,
  type InternalNode,
  MarkerType,
  type Node,
  type NodeChange,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useInternalNode,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  FileText,
  FolderCog,
  Globe,
  Hammer,
  ListTodo,
  Loader2,
  Plug,
  Repeat2,
  Route,
  Sparkles,
  TerminalSquare,
  TriangleAlert,
  Wrench,
  X,
} from 'lucide-react'
import { memo, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import '@/lib/monaco-setup'
import { type ToolInventory, useAgentAuthoring } from '@/hooks/useAgentAuthoring'
import { type HarnessDocument, type HarnessValidationResult, useHarnessAuthoring } from '@/hooks/useHarnessAuthoring'
import { cn } from '@/lib/utils'

/**
 * Harness Agent Editor (CTR-0196, FEAT-0064, PRP-0135, UDR-0119).
 *
 * The CTR-0179 layout family, per operator requirement: LEFT property/form panels
 * own the scalar + text fields and the harness building-block switches; CENTER a
 * React Flow hub-and-spoke canvas whose toolbar owns MODEL (single-select -- a
 * harness agent binds ONE offering, UDR-0119 D2) and TOOL attachment (CTR-0178
 * function tools + whole MCP servers; coding tools and skills are not offered,
 * UDR-0119 D7); block nodes visualize the enabled building blocks. RIGHT a monaco
 * pane shows the backend-canonical YAML (live preview) with a raw-edit escape
 * hatch. Validation + serialization are the backend's (CTR-0195, the UDR-0100
 * D6/D8 pattern). The iterative checkpoint-save session model applies (UDR-0111).
 */

const CONTROL =
  'w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-ring'

// ChatWalaʻau coding tools are harness-internal territory (UDR-0119 D7); skills ride
// SKILLS_DIR. Neither is offered by this editor's tool picker.
const HIDDEN_FUNCTION_CATEGORY = 'coding'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create a new harness agent; otherwise the id being edited. */
  editId: string | null
  /** Called after a successful save (create/update) so the manager refreshes. */
  onSaved: (id?: string) => void
}

function emptyDocument(): HarnessDocument {
  return {
    name: '',
    displayName: '',
    description: '',
    model: { id: '' },
    instructions: { harness: '', agent: '' },
    tools: [],
    compaction: { disabled: false, maxContextWindowTokens: null, maxOutputTokens: null },
    todo: { disabled: false },
    mode: { disabled: false, initial: null },
    fileMemory: { disabled: false },
    fileAccess: { disableWriteTools: false, disableWriteToolApproval: false },
    webSearch: { disabled: false },
    loop: { maxIterations: null },
  }
}

// ---- React Flow custom nodes (hub-and-spoke) -------------------------------
interface PartData extends Record<string, unknown> {
  label: string
  sub?: string
  variant: 'hub' | 'model' | 'tool' | 'block'
  icon?: ReactNode
  onRemove?: () => void
}

const PartNode = memo(({ data }: NodeProps<Node<PartData>>) => {
  const isHub = data.variant === 'hub'
  const icon =
    data.icon ??
    (data.variant === 'hub' ? (
      <Hammer className="h-4 w-4" />
    ) : data.variant === 'model' ? (
      <Sparkles className="h-4 w-4" />
    ) : (
      <Wrench className="h-4 w-4" />
    ))
  return (
    <div
      className={cn(
        'flex items-center gap-2 rounded-md border px-3 py-2 text-xs shadow-sm',
        isHub ? 'border-primary bg-primary/10 font-semibold' : 'border-border bg-background',
      )}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
      <span className="text-muted-foreground">{icon}</span>
      <span className="min-w-0">
        <span className="block max-w-[160px] truncate font-medium">{data.label}</span>
        {data.sub && <span className="block max-w-[160px] truncate text-[10px] text-muted-foreground">{data.sub}</span>}
      </span>
      {data.onRemove && (
        <button
          type="button"
          onClick={data.onRemove}
          className="ml-1 rounded p-0.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          aria-label={`Remove ${data.label}`}>
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  )
})
PartNode.displayName = 'PartNode'

const nodeTypes = { part: PartNode }

// ---- Floating edges (the CTR-0179 technique) --------------------------------
function nodeCenter(node: InternalNode) {
  const w = node.measured?.width ?? 0
  const h = node.measured?.height ?? 0
  return { x: node.internals.positionAbsolute.x + w / 2, y: node.internals.positionAbsolute.y + h / 2, w, h }
}

function borderPoint(source: InternalNode, target: InternalNode) {
  const s = nodeCenter(source)
  const t = nodeCenter(target)
  const dx = t.x - s.x
  const dy = t.y - s.y
  if (dx === 0 && dy === 0) return { x: s.x, y: s.y }
  const halfW = s.w / 2 || 1
  const halfH = s.h / 2 || 1
  const scale = 1 / Math.max(Math.abs(dx) / halfW, Math.abs(dy) / halfH)
  return { x: s.x + dx * scale, y: s.y + dy * scale }
}

function FloatingEdge({ id, source, target, markerEnd, style }: EdgeProps) {
  const sourceNode = useInternalNode(source)
  const targetNode = useInternalNode(target)
  if (!sourceNode || !targetNode) return null
  const sp = borderPoint(sourceNode, targetNode)
  const tp = borderPoint(targetNode, sourceNode)
  const [path] = getStraightPath({ sourceX: sp.x, sourceY: sp.y, targetX: tp.x, targetY: tp.y })
  return <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
}

const edgeTypes = { floating: FloatingEdge }

// The harness building blocks visualized as canvas nodes (fixed-vs-configurable is
// stated in the sub label; the switches themselves live in the LEFT form panel).
function blockParts(doc: HarnessDocument): Array<{ id: string; label: string; sub: string; icon: ReactNode }> {
  const parts: Array<{ id: string; label: string; sub: string; icon: ReactNode }> = []
  if (!doc.todo.disabled)
    parts.push({
      id: 'b-todo',
      label: 'Todo list',
      sub: 'todos_remaining loop',
      icon: <ListTodo className="h-4 w-4" />,
    })
  if (!doc.mode.disabled)
    parts.push({
      id: 'b-mode',
      label: 'Plan / Execute',
      sub: doc.mode.initial ? `initial: ${doc.mode.initial}` : 'mode tracking',
      icon: <Route className="h-4 w-4" />,
    })
  if (!doc.fileMemory.disabled)
    parts.push({ id: 'b-fmem', label: 'File memory', sub: 'workspace-scoped', icon: <FileText className="h-4 w-4" /> })
  parts.push({
    id: 'b-facc',
    label: 'File access',
    sub: doc.fileAccess.disableWriteTools ? 'read-only' : 'read + write (approval)',
    icon: <FolderCog className="h-4 w-4" />,
  })
  parts.push({ id: 'b-shell', label: 'Shell', sub: 'workspace-scoped', icon: <TerminalSquare className="h-4 w-4" /> })
  if (!doc.webSearch.disabled)
    parts.push({ id: 'b-web', label: 'Web search', sub: 'offering-gated', icon: <Globe className="h-4 w-4" /> })
  if (!doc.compaction.disabled)
    parts.push({ id: 'b-compact', label: 'Compaction', sub: 'token budget', icon: <Repeat2 className="h-4 w-4" /> })
  return parts
}

function buildNodes(
  doc: HarnessDocument,
  rawMode: boolean,
  removeTool: (ident: string) => void,
): Array<Node<PartData>> {
  const parts: Array<Node<PartData>> = [
    {
      id: 'hub',
      type: 'part',
      position: { x: 320, y: 240 },
      data: { label: doc.name || 'New Harness Agent', sub: 'Harness agent', variant: 'hub' },
    },
  ]
  if (doc.model.id) {
    parts.push({
      id: 'model',
      type: 'part',
      position: { x: 40, y: 120 },
      data: { label: doc.model.id, sub: 'model (single)', variant: 'model' },
    })
  }
  blockParts(doc).forEach((b, i) => {
    parts.push({
      id: b.id,
      type: 'part',
      position: { x: 40 + (i % 2) * 160, y: 320 + Math.floor(i / 2) * 60 },
      data: { label: b.label, sub: b.sub, variant: 'block', icon: b.icon },
    })
  })
  doc.tools.forEach((ident, i) => {
    parts.push({
      id: `tool-${i}`,
      type: 'part',
      position: { x: 640, y: 60 + i * 70 },
      data: {
        label: ident.replace(/^(function|mcp):/, ''),
        sub: ident.startsWith('mcp:') ? 'mcp server' : 'function',
        variant: 'tool',
        onRemove: rawMode ? undefined : () => removeTool(ident),
      },
    })
  })
  return parts
}

function buildEdges(doc: HarnessDocument) {
  const marker = { type: MarkerType.ArrowClosed }
  const es: Array<{ id: string; source: string; target: string; type: string; markerEnd: { type: MarkerType } }> = []
  if (doc.model.id) es.push({ id: 'e-model', source: 'hub', target: 'model', type: 'floating', markerEnd: marker })
  for (const b of blockParts(doc))
    es.push({ id: `e-${b.id}`, source: 'hub', target: b.id, type: 'floating', markerEnd: marker })
  doc.tools.forEach((_, i) => {
    es.push({ id: `e-tool-${i}`, source: 'hub', target: `tool-${i}`, type: 'floating', markerEnd: marker })
  })
  return es
}

export function HarnessAgentEditor({ open, onOpenChange, editId, onSaved }: Props) {
  const api = useHarnessAuthoring()
  const agentApi = useAgentAuthoring() // tool inventory + model list (CTR-0178 / CTR-0069)
  const [doc, setDoc] = useState<HarnessDocument>(emptyDocument)
  const [rawMode, setRawMode] = useState(false)
  const [rawYaml, setRawYaml] = useState('')
  const [inventory, setInventory] = useState<ToolInventory | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [validation, setValidation] = useState<HarnessValidationResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [showAddTool, setShowAddTool] = useState(false)
  const [leaveConfirm, setLeaveConfirm] = useState(false)
  // UDR-0111 D2: create-mode adopts the id its first save assigns.
  const [savedId, setSavedId] = useState<string | null>(null)
  // UDR-0111 D1: inline save acknowledgement; the editor stays open.
  const [saveNotice, setSaveNotice] = useState<string | null>(null)

  // ---- initial load: inventory + models, plus source for edit ----
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setDirty(false)
    setRawMode(false)
    setSavedId(null)
    setSaveNotice(null)
    ;(async () => {
      try {
        const [inv, mi] = await Promise.all([agentApi.loadInventory(), agentApi.loadModels()])
        if (cancelled) return
        setInventory(inv)
        setModels(mi.models ?? [])
        if (editId) {
          const src = await api.loadSource(editId)
          if (cancelled) return
          setDoc({ ...emptyDocument(), ...src.document })
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
  }, [open, editId, api, agentApi])

  // ---- debounced validation (single source of truth: the backend, CTR-0195) ----
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

  const patch = useCallback((p: Partial<HarnessDocument>) => {
    setDoc((d) => ({ ...d, ...p }))
    setDirty(true)
  }, [])

  // ---- add / remove tools (flat CTR-0178 identifiers; whole MCP servers only) ----
  const addTool = useCallback((ident: string) => {
    setDoc((d) => (d.tools.includes(ident) ? d : { ...d, tools: [...d.tools, ident] }))
    setDirty(true)
    setShowAddTool(false)
  }, [])

  const removeTool = useCallback((ident: string) => {
    setDoc((d) => ({ ...d, tools: d.tools.filter((t) => t !== ident) }))
    setDirty(true)
  }, [])

  // ---- canvas nodes / edges ----
  const [nodes, setNodes] = useState<Array<Node<PartData>>>([])
  useEffect(() => {
    setNodes((prev) => {
      const posById = new Map(prev.map((n) => [n.id, n.position]))
      return buildNodes(doc, rawMode, removeTool).map((n) => ({ ...n, position: posById.get(n.id) ?? n.position }))
    })
  }, [doc, rawMode, removeTool])
  const onNodesChange = useCallback((changes: Array<NodeChange<Node<PartData>>>) => {
    setNodes((nds) => applyNodeChanges(changes, nds))
  }, [])
  const edges = useMemo(() => buildEdges(doc), [doc])

  // ---- save / close (UDR-0111 iterative session) ----
  const boundId = editId ?? savedId

  const handleSave = useCallback(async () => {
    setSaving(true)
    setError(null)
    setSaveNotice(null)
    try {
      const body = rawMode ? { yaml: rawYaml } : { document: doc, name: doc.name }
      const result = await api.save(body, boundId)
      const id = result.id ?? boundId ?? undefined
      setDirty(false)
      if (id) setSavedId(id)
      setSaveNotice('Saved. This editor stays open -- use Close when you are done.')
      onSaved(id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save harness agent')
    } finally {
      setSaving(false)
    }
  }, [rawMode, rawYaml, doc, boundId, api, onSaved])

  useEffect(() => {
    if (!saveNotice) return
    const t = setTimeout(() => setSaveNotice(null), 4000)
    return () => clearTimeout(t)
  }, [saveNotice])

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
        <DialogTitle className="sr-only">{editId ? 'Edit Harness Agent' : 'Create Harness Agent'}</DialogTitle>
        <DialogDescription className="sr-only">
          Compose a harness agent: model, instructions, tools, and harness building blocks.
        </DialogDescription>
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b px-5 py-3">
          <div className="flex items-center gap-2">
            <Hammer className="h-5 w-5 text-primary" />
            <div>
              <div className="text-sm font-semibold">Harness Agent</div>
              <div className="text-[11px] text-muted-foreground">
                MAF create_harness_agent() composition. Provider, credentials, and history/todo/loop policies are
                resolved by ChatWalaʻau.
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
              {boundId ? 'Save' : 'Create'}
            </Button>
          </div>
        </div>

        {loading ? (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
          </div>
        ) : (
          <PanelGroup direction="horizontal" className="min-h-0 flex-1">
            {/* LEFT: form */}
            <Panel defaultSize={30} minSize={18}>
              <div className={cn('h-full space-y-3 overflow-y-auto p-4', rawMode && 'pointer-events-none opacity-50')}>
                <Field label="Name (identifier)">
                  <input
                    className={CONTROL}
                    value={doc.name}
                    onChange={(e) => patch({ name: e.target.value })}
                    placeholder="repo-fixer"
                  />
                </Field>
                <Field label="Display name (optional)">
                  <input
                    className={CONTROL}
                    value={doc.displayName ?? ''}
                    onChange={(e) => patch({ displayName: e.target.value })}
                  />
                </Field>
                <Field label="Description">
                  <textarea
                    className={cn(CONTROL, 'h-14 resize-none')}
                    value={doc.description ?? ''}
                    onChange={(e) => patch({ description: e.target.value })}
                  />
                </Field>
                <Field label="Harness instructions (optional)">
                  <textarea
                    className={cn(CONTROL, 'h-20 resize-y font-mono text-xs')}
                    value={doc.instructions.harness ?? ''}
                    onChange={(e) => patch({ instructions: { ...doc.instructions, harness: e.target.value } })}
                    placeholder="Empty = the MAF default harness operating guidelines."
                  />
                </Field>
                <Field label="Agent instructions (optional)">
                  <textarea
                    className={cn(CONTROL, 'h-28 resize-y font-mono text-xs')}
                    value={doc.instructions.agent ?? ''}
                    onChange={(e) => patch({ instructions: { ...doc.instructions, agent: e.target.value } })}
                    placeholder={
                      'Empty = the software-engineering default.\n"=Identity" inherits the global agent identity.'
                    }
                  />
                </Field>

                <SwitchField
                  label="Todo list"
                  checked={!doc.todo.disabled}
                  onChange={(on) => patch({ todo: { disabled: !on } })}
                  note="Fixed: todos_remaining() drives the loop."
                />
                <SwitchField
                  label="Plan / Execute mode"
                  checked={!doc.mode.disabled}
                  onChange={(on) => patch({ mode: { ...doc.mode, disabled: !on } })}
                />
                {!doc.mode.disabled && (
                  <Field label="Initial mode">
                    <select
                      className={CONTROL}
                      value={doc.mode.initial ?? ''}
                      onChange={(e) => patch({ mode: { ...doc.mode, initial: e.target.value || null } })}>
                      <option value="">Default</option>
                      <option value="plan">plan</option>
                      <option value="execute">execute</option>
                    </select>
                  </Field>
                )}
                <SwitchField
                  label="File memory"
                  checked={!doc.fileMemory.disabled}
                  onChange={(on) => patch({ fileMemory: { disabled: !on } })}
                  note="Needs CODING_WORKSPACE_DIR."
                />
                <SwitchField
                  label="File write tools"
                  checked={!doc.fileAccess.disableWriteTools}
                  onChange={(on) => patch({ fileAccess: { ...doc.fileAccess, disableWriteTools: !on } })}
                />
                <SwitchField
                  label="Write approval required"
                  checked={!doc.fileAccess.disableWriteToolApproval}
                  onChange={(on) => patch({ fileAccess: { ...doc.fileAccess, disableWriteToolApproval: !on } })}
                  note="Turning this OFF lets writes run without the approval card (explicit opt-in)."
                />
                <SwitchField
                  label="Web search"
                  checked={!doc.webSearch.disabled}
                  onChange={(on) => patch({ webSearch: { disabled: !on } })}
                  note="Withheld automatically when the offering's gate is off."
                />
                <SwitchField
                  label="Context compaction"
                  checked={!doc.compaction.disabled}
                  onChange={(on) => patch({ compaction: { ...doc.compaction, disabled: !on } })}
                  note="Window defaults to the offering's context window."
                />
                {!doc.compaction.disabled && (
                  <div className="grid grid-cols-2 gap-2">
                    <Field label="Max window tokens">
                      <input
                        className={CONTROL}
                        type="number"
                        min={1}
                        value={doc.compaction.maxContextWindowTokens ?? ''}
                        onChange={(e) =>
                          patch({
                            compaction: {
                              ...doc.compaction,
                              maxContextWindowTokens: e.target.value ? Number(e.target.value) : null,
                            },
                          })
                        }
                        placeholder="catalog"
                      />
                    </Field>
                    <Field label="Max output tokens">
                      <input
                        className={CONTROL}
                        type="number"
                        min={1}
                        value={doc.compaction.maxOutputTokens ?? ''}
                        onChange={(e) =>
                          patch({
                            compaction: {
                              ...doc.compaction,
                              maxOutputTokens: e.target.value ? Number(e.target.value) : null,
                            },
                          })
                        }
                        placeholder="auto"
                      />
                    </Field>
                  </div>
                )}
                <Field label="Loop max iterations (cap 10)">
                  <input
                    className={CONTROL}
                    type="number"
                    min={1}
                    max={10}
                    value={doc.loop.maxIterations ?? ''}
                    onChange={(e) => patch({ loop: { maxIterations: e.target.value ? Number(e.target.value) : null } })}
                    placeholder="10"
                  />
                </Field>
              </div>
            </Panel>

            <PanelResizeHandle className="w-1 bg-border data-[resize-handle-state=drag]:bg-primary" />

            {/* CENTER: React Flow canvas + model/tool toolbar */}
            <Panel defaultSize={42} minSize={25}>
              <div className="flex h-full flex-col">
                <div
                  className={cn(
                    'flex flex-wrap items-center gap-2 border-b p-2',
                    rawMode && 'pointer-events-none opacity-50',
                  )}>
                  <Sparkles className="h-4 w-4 text-muted-foreground" />
                  <select
                    className={cn(CONTROL, 'w-44')}
                    value={doc.model.id ?? ''}
                    onChange={(e) => patch({ model: { id: e.target.value } })}>
                    <option value="">Select a model (required)</option>
                    {models.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                  <div className="relative">
                    <Button variant="outline" size="sm" onClick={() => setShowAddTool((s) => !s)}>
                      <Wrench className="mr-1 h-3.5 w-3.5" /> Add tool
                    </Button>
                    {showAddTool && inventory && (
                      <HarnessToolPicker
                        inventory={inventory}
                        isSelected={(ident) => doc.tools.includes(ident)}
                        onAdd={addTool}
                        onClose={() => setShowAddTool(false)}
                      />
                    )}
                  </div>
                  <span className="text-[10px] text-muted-foreground">
                    File / shell tools are harness-built-in; Skills load from SKILLS_DIR.
                  </span>
                </div>
                <div className="min-h-0 flex-1">
                  <ReactFlowProvider>
                    <ReactFlow
                      nodes={nodes}
                      edges={edges}
                      nodeTypes={nodeTypes}
                      edgeTypes={edgeTypes}
                      onNodesChange={onNodesChange}
                      nodesConnectable={false}
                      fitView
                      minZoom={0.2}
                      proOptions={{ hideAttribution: true }}>
                      <Background gap={16} />
                    </ReactFlow>
                  </ReactFlowProvider>
                </div>
              </div>
            </Panel>

            <PanelResizeHandle className="w-1 bg-border data-[resize-handle-state=drag]:bg-primary" />

            {/* RIGHT: monaco YAML (preview + raw-edit escape hatch) + warnings */}
            <Panel defaultSize={28} minSize={18}>
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
        {!error && saveNotice && (
          <output className="block shrink-0 border-t bg-primary/10 px-4 py-2 text-xs text-primary">{saveNotice}</output>
        )}

        {/* Unsaved-changes guard */}
        {leaveConfirm && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-background/80">
            <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
              <p className="text-sm font-medium">Discard unsaved changes?</p>
              <p className="mt-1 text-xs text-muted-foreground">
                Your edits to this harness agent have not been saved.
              </p>
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

function SwitchField({
  label,
  checked,
  onChange,
  note,
}: {
  label: string
  checked: boolean
  onChange: (on: boolean) => void
  note?: string
}) {
  return (
    <div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        {label}
      </label>
      {note && <p className="mt-0.5 pl-6 text-[10px] text-muted-foreground">{note}</p>}
    </div>
  )
}

function HarnessToolPicker({
  inventory,
  isSelected,
  onAdd,
  onClose,
}: {
  inventory: ToolInventory
  isSelected: (identifier: string) => boolean
  onAdd: (identifier: string) => void
  onClose: () => void
}) {
  // Coding tools are harness-internal (UDR-0119 D7); skills ride SKILLS_DIR; MCP is
  // whole-server only (per-tool narrowing is the MCP Tool Manager's job, CTR-0121).
  const functions = inventory.function_tools.filter((t) => t.category !== HIDDEN_FUNCTION_CATEGORY)
  return (
    <>
      <div className="fixed inset-0 z-10" onClick={onClose} aria-hidden />
      <div className="absolute left-0 top-full z-20 mt-1 max-h-80 w-80 overflow-y-auto rounded-md border bg-background p-2 shadow-lg">
        <div className="mb-1">
          <div className="flex items-center gap-1.5 px-1 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Wrench className="h-3.5 w-3.5" /> Built-in tools
          </div>
          {functions.map((t) => (
            <PickRow
              key={t.identifier}
              label={t.name}
              sub={t.available ? t.description : `${t.description} (disabled)`}
              selected={isSelected(t.identifier)}
              onAdd={() => onAdd(t.identifier)}
            />
          ))}
        </div>
        {inventory.mcp_servers.length > 0 && (
          <div className="mb-1">
            <div className="flex items-center gap-1.5 px-1 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              <Plug className="h-3.5 w-3.5" /> MCP servers (whole server)
            </div>
            {inventory.mcp_servers.map((s) => (
              <PickRow
                key={s.identifier}
                label={s.name}
                sub={s.available ? 'whole server' : 'not loaded'}
                selected={isSelected(s.identifier)}
                onAdd={() => onAdd(s.identifier)}
              />
            ))}
          </div>
        )}
      </div>
    </>
  )
}

function PickRow({
  label,
  sub,
  selected,
  onAdd,
}: {
  label: string
  sub?: string
  selected: boolean
  onAdd: () => void
}) {
  return (
    <button
      type="button"
      disabled={selected}
      onClick={onAdd}
      className={cn(
        'flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left text-xs hover:bg-accent',
        selected && 'opacity-50',
      )}>
      <span className="min-w-0">
        <span className="block truncate font-medium">{label}</span>
        {sub && <span className="block truncate text-[10px] text-muted-foreground">{sub}</span>}
      </span>
      <span className="shrink-0 text-[10px] text-muted-foreground">{selected ? 'added' : '+'}</span>
    </button>
  )
}
