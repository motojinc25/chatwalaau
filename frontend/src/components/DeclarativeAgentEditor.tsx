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
import { Bot, Boxes, Loader2, Plug, Sparkles, TriangleAlert, Wrench, X } from 'lucide-react'
import { memo, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import '@/lib/monaco-setup'
import {
  type AgentDocument,
  type ToolEntry,
  type ToolInventory,
  useAgentAuthoring,
  type ValidationResult,
} from '@/hooks/useAgentAuthoring'
import { cn } from '@/lib/utils'

/**
 * Declarative Agent Editor (CTR-0179, FEAT-0061, PRP-0117, UDR-0100 D7).
 *
 * A full-screen hybrid authoring surface launched from the Declarative Agents modal
 * (CTR-0144 v2). LEFT: property/form panels for the scalar + text fields. CENTER: a
 * React Flow hub-and-spoke canvas whose toolbar owns MODEL + TOOL attachment (the
 * canvas visualizes the agent, its model, its tools, and its output schema). RIGHT:
 * a monaco pane showing the backend-canonical YAML (live preview) with a raw-edit
 * escape hatch. Validation + serialization are the backend's (UDR-0100 D6/D8): the
 * editor sends a structured `document` (form mode) or raw `yaml` (raw mode) to
 * /api/agents/authoring/validate and renders the returned canonical YAML + warnings.
 */

const CONTROL =
  'w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-ring'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create a new agent; otherwise the id of the agent being edited. */
  editId: string | null
  /** Called after a successful save (create/update) so the manager refreshes. */
  onSaved: (id?: string) => void
}

function emptyDocument(): AgentDocument {
  return {
    name: '',
    displayName: '',
    description: '',
    instructions: '',
    model: { id: '', options: { effort: '', verbosity: '' } },
    tools: [],
    outputSchema: null,
  }
}

// ---- React Flow custom nodes (hub-and-spoke) -------------------------------
interface PartData extends Record<string, unknown> {
  label: string
  sub?: string
  variant: 'hub' | 'model' | 'tool' | 'schema'
  onRemove?: () => void
}

const PartNode = memo(({ data }: NodeProps<Node<PartData>>) => {
  const isHub = data.variant === 'hub'
  const icon =
    data.variant === 'hub' ? (
      <Bot className="h-4 w-4" />
    ) : data.variant === 'model' ? (
      <Sparkles className="h-4 w-4" />
    ) : data.variant === 'schema' ? (
      <Boxes className="h-4 w-4" />
    ) : (
      <Wrench className="h-4 w-4" />
    )
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

// ---- Floating edges: attach at the nearest point on each node's border (360deg) ----
function nodeCenter(node: InternalNode) {
  const w = node.measured?.width ?? 0
  const h = node.measured?.height ?? 0
  return { x: node.internals.positionAbsolute.x + w / 2, y: node.internals.positionAbsolute.y + h / 2, w, h }
}

// Intersection of the center->center line with the source node's rectangular border,
// so an edge leaves/enters at the point on the perimeter facing the other node.
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

// Build the hub-and-spoke nodes from the document (positions are seeded here and then
// owned by React Flow state so the user can drag them; edges are always derived).
function buildNodes(
  doc: AgentDocument,
  rawMode: boolean,
  removeTool: (identifier: string) => void,
): Array<Node<PartData>> {
  const parts: Array<Node<PartData>> = [
    {
      id: 'hub',
      type: 'part',
      position: { x: 320, y: 220 },
      data: { label: doc.name || 'New Agent', sub: 'Prompt agent', variant: 'hub' },
    },
  ]
  if (doc.model.id) {
    parts.push({
      id: 'model',
      type: 'part',
      position: { x: 40, y: 120 },
      data: { label: doc.model.id, sub: 'model', variant: 'model' },
    })
  }
  if (doc.outputSchema?.properties && Object.keys(doc.outputSchema.properties).length > 0) {
    parts.push({
      id: 'schema',
      type: 'part',
      position: { x: 40, y: 320 },
      data: {
        label: 'Output schema',
        sub: `${Object.keys(doc.outputSchema.properties).length} field(s)`,
        variant: 'schema',
      },
    })
  }
  doc.tools.forEach((t, i) => {
    const ident = toolIdentifier(t)
    parts.push({
      id: `tool-${i}`,
      type: 'part',
      position: { x: 620, y: 40 + i * 70 },
      data: { label: t.name, sub: t.kind, variant: 'tool', onRemove: rawMode ? undefined : () => removeTool(ident) },
    })
  })
  return parts
}

function buildEdges(
  doc: AgentDocument,
): Array<{ id: string; source: string; target: string; type: string; markerEnd: { type: MarkerType } }> {
  const marker = { type: MarkerType.ArrowClosed }
  const es: Array<{ id: string; source: string; target: string; type: string; markerEnd: { type: MarkerType } }> = []
  if (doc.model.id) es.push({ id: 'e-model', source: 'hub', target: 'model', type: 'floating', markerEnd: marker })
  if (doc.outputSchema?.properties && Object.keys(doc.outputSchema.properties).length > 0)
    es.push({ id: 'e-schema', source: 'hub', target: 'schema', type: 'floating', markerEnd: marker })
  doc.tools.forEach((_, i) => {
    es.push({ id: `e-tool-${i}`, source: 'hub', target: `tool-${i}`, type: 'floating', markerEnd: marker })
  })
  return es
}

export function DeclarativeAgentEditor({ open, onOpenChange, editId, onSaved }: Props) {
  const api = useAgentAuthoring()
  const [doc, setDoc] = useState<AgentDocument>(emptyDocument)
  const [rawMode, setRawMode] = useState(false)
  const [rawYaml, setRawYaml] = useState('')
  const [inventory, setInventory] = useState<ToolInventory | null>(null)
  const [models, setModels] = useState<{
    ids: string[]
    options: Record<string, Array<{ key: string; allowed?: string[] }>>
  }>({
    ids: [],
    options: {},
  })
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [showAddTool, setShowAddTool] = useState(false)
  const [leaveConfirm, setLeaveConfirm] = useState(false)

  // ---- initial load: inventory + models, plus source for edit ----
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setDirty(false)
    setRawMode(false)
    ;(async () => {
      try {
        const [inv, mi] = await Promise.all([api.loadInventory(), api.loadModels()])
        if (cancelled) return
        setInventory(inv)
        const options: Record<string, Array<{ key: string; allowed?: string[] }>> = {}
        for (const [id, entry] of Object.entries(mi.model_options ?? {})) {
          options[id] = entry.options.filter((o) => o.key === 'effort' || o.key === 'verbosity')
        }
        setModels({ ids: mi.models ?? [], options })
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
  }, [open, editId, api])

  // ---- debounced validation (single source of truth: the backend) ----
  const validateTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!open || loading) return
    if (validateTimer.current) clearTimeout(validateTimer.current)
    validateTimer.current = setTimeout(async () => {
      try {
        const body = rawMode ? { yaml: rawYaml } : { document: doc }
        const result = await api.validate(body)
        setValidation(result)
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

  const patch = useCallback((p: Partial<AgentDocument>) => {
    setDoc((d) => ({ ...d, ...p }))
    setDirty(true)
  }, [])

  const modelOptions = doc.model.id ? (models.options[doc.model.id] ?? []) : []
  const effortAllowed = modelOptions.find((o) => o.key === 'effort')?.allowed ?? []
  const verbosityAllowed = modelOptions.find((o) => o.key === 'verbosity')?.allowed ?? []

  // ---- add / remove tools ----
  const toolSelected = useCallback(
    (identifier: string) => {
      return doc.tools.some((t) => toolIdentifier(t) === identifier)
    },
    [doc.tools],
  )

  const addTool = useCallback((entry: ToolEntry) => {
    setDoc((d) => {
      const id = toolIdentifier(entry)
      if (d.tools.some((t) => toolIdentifier(t) === id)) return d
      return { ...d, tools: [...d.tools, entry] }
    })
    setDirty(true)
    setShowAddTool(false)
  }, [])

  const removeTool = useCallback((identifier: string) => {
    setDoc((d) => ({ ...d, tools: d.tools.filter((t) => toolIdentifier(t) !== identifier) }))
    setDirty(true)
  }, [])

  // ---- canvas nodes / edges (hub-and-spoke) ----
  // Nodes are held in React Flow state so they are DRAGGABLE; when the document
  // changes we rebuild the node set but PRESERVE any position the user has dragged
  // to (keyed by node id). Edges are always derived and use the floating edge type
  // so they attach at the nearest point on each node's border.
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

  // ---- save / delete / close ----
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
      setError(err instanceof Error ? err.message : 'Failed to save agent')
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
        {/* [&>button]:hidden removes the shadcn built-in X close button (a direct child
            of DialogContent); the header Close button is the single close affordance. */}
        {/* Radix requires an accessible title/description; the visible header below is
            custom, so these are screen-reader-only. */}
        <DialogTitle className="sr-only">{editId ? 'Edit declarative agent' : 'Create declarative agent'}</DialogTitle>
        <DialogDescription className="sr-only">
          Compose a single Prompt declarative agent: persona, model, tools, and output schema.
        </DialogDescription>
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b px-5 py-3">
          <div className="flex items-center gap-2">
            <Bot className="h-5 w-5 text-primary" />
            <div>
              <div className="text-sm font-semibold">
                {editId ? 'Edit declarative agent' : 'Create declarative agent'}
              </div>
              <div className="text-[11px] text-muted-foreground">
                Compose a single Prompt agent. Provider, credentials, and sampling are resolved by ChatWalaʻau.
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
            {/* LEFT: form */}
            <Panel defaultSize={30} minSize={18}>
              <div className={cn('h-full space-y-3 overflow-y-auto p-4', rawMode && 'pointer-events-none opacity-50')}>
                <Field label="Name (identifier)">
                  <input
                    className={CONTROL}
                    value={doc.name}
                    onChange={(e) => patch({ name: e.target.value })}
                    placeholder="TechnicalSupportAgent"
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
                    className={cn(CONTROL, 'h-16 resize-none')}
                    value={doc.description ?? ''}
                    onChange={(e) => patch({ description: e.target.value })}
                  />
                </Field>
                <Field label="Instructions">
                  <textarea
                    className={cn(CONTROL, 'h-40 resize-y font-mono text-xs')}
                    value={doc.instructions}
                    onChange={(e) => patch({ instructions: e.target.value })}
                    placeholder={'You are a helpful assistant.\nReply concisely in the user’s language.'}
                  />
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    Leave empty (or "=Identity") to inherit the global agent identity.
                  </p>
                </Field>
                <OutputSchemaEditor doc={doc} patch={patch} />
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
                    className={cn(CONTROL, 'w-40')}
                    value={doc.model.id ?? ''}
                    onChange={(e) => patch({ model: { ...doc.model, id: e.target.value } })}>
                    <option value="">Default model</option>
                    {models.ids.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                  {effortAllowed.length > 0 && (
                    <select
                      className={cn(CONTROL, 'w-28')}
                      value={doc.model.options?.effort ?? ''}
                      onChange={(e) =>
                        patch({ model: { ...doc.model, options: { ...doc.model.options, effort: e.target.value } } })
                      }>
                      <option value="">effort</option>
                      {effortAllowed.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  )}
                  {verbosityAllowed.length > 0 && (
                    <select
                      className={cn(CONTROL, 'w-28')}
                      value={doc.model.options?.verbosity ?? ''}
                      onChange={(e) =>
                        patch({ model: { ...doc.model, options: { ...doc.model.options, verbosity: e.target.value } } })
                      }>
                      <option value="">verbosity</option>
                      {verbosityAllowed.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  )}
                  <div className="relative">
                    <Button variant="outline" size="sm" onClick={() => setShowAddTool((s) => !s)}>
                      <Wrench className="mr-1 h-3.5 w-3.5" /> Add tool
                    </Button>
                    {showAddTool && inventory && (
                      <ToolPicker
                        inventory={inventory}
                        isSelected={toolSelected}
                        onAdd={addTool}
                        onClose={() => setShowAddTool(false)}
                      />
                    )}
                  </div>
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
                          <TriangleAlert className="h-3.5 w-3.5 shrink-0" /> Resolve before activating:
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

        {/* Unsaved-changes guard */}
        {leaveConfirm && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-background/80">
            <div className="w-[360px] rounded-lg border bg-background p-4 shadow-lg">
              <p className="text-sm font-medium">Discard unsaved changes?</p>
              <p className="mt-1 text-xs text-muted-foreground">Your edits to this agent have not been saved.</p>
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

function toolIdentifier(t: ToolEntry): string {
  if (t.kind === 'function') return `function:${t.name}`
  if (t.kind === 'skill') return `skill:${t.name}`
  // mcp: a whole server, or the first allowed tool defines identity for de-dup.
  if (t.allowedTools?.length) return `mcp:${t.name}/${t.allowedTools.join(',')}`
  return `mcp:${t.name}`
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <span className="mb-1 block text-[11px] font-medium text-muted-foreground">{label}</span>
      {children}
    </div>
  )
}

type SchemaProp = { type: string; description?: string; required?: boolean }

function OutputSchemaEditor({ doc, patch }: { doc: AgentDocument; patch: (p: Partial<AgentDocument>) => void }) {
  const props = (doc.outputSchema?.properties ?? {}) as Record<string, SchemaProp>
  const entries = Object.entries(props)
  const setProps = (next: Record<string, SchemaProp>) =>
    patch({ outputSchema: Object.keys(next).length ? { properties: next } : null })
  return (
    <Field label="Output schema (structured output, optional)">
      <div className="space-y-2">
        {entries.map(([name, desc]) => (
          <div key={name} className="space-y-1 rounded-md border p-2">
            <div className="flex items-center gap-1.5">
              <input
                className={cn(CONTROL, 'flex-1')}
                value={name}
                onChange={(e) => {
                  const next: Record<string, SchemaProp> = {}
                  // Rename in place while preserving key order.
                  for (const [k, v] of Object.entries(props)) next[k === name ? e.target.value : k] = v
                  setProps(next)
                }}
                placeholder="field"
              />
              <select
                className={cn(CONTROL, 'w-24')}
                value={desc.type}
                onChange={(e) => setProps({ ...props, [name]: { ...desc, type: e.target.value } })}>
                {['string', 'number', 'integer', 'boolean', 'array', 'object'].map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="rounded p-1 text-muted-foreground hover:text-destructive"
                onClick={() => {
                  const next = { ...props }
                  delete next[name]
                  setProps(next)
                }}
                aria-label={`Remove ${name}`}>
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <input
              className={cn(CONTROL, 'text-xs')}
              value={desc.description ?? ''}
              onChange={(e) => setProps({ ...props, [name]: { ...desc, description: e.target.value } })}
              placeholder="description"
            />
            <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <input
                type="checkbox"
                checked={desc.required ?? false}
                onChange={(e) => setProps({ ...props, [name]: { ...desc, required: e.target.checked } })}
              />
              required
            </label>
          </div>
        ))}
        <Button
          variant="outline"
          size="sm"
          onClick={() => setProps({ ...props, [`field${entries.length + 1}`]: { type: 'string' } })}>
          + Add field
        </Button>
      </div>
    </Field>
  )
}

function ToolPicker({
  inventory,
  isSelected,
  onAdd,
  onClose,
}: {
  inventory: ToolInventory
  isSelected: (identifier: string) => boolean
  onAdd: (entry: ToolEntry) => void
  onClose: () => void
}) {
  return (
    <>
      <div className="fixed inset-0 z-10" onClick={onClose} aria-hidden />
      <div className="absolute left-0 top-full z-20 mt-1 max-h-80 w-80 overflow-y-auto rounded-md border bg-background p-2 shadow-lg">
        <ToolGroup title="Built-in tools" icon={<Wrench className="h-3.5 w-3.5" />}>
          {inventory.function_tools.map((t) => (
            <ToolRow
              key={t.identifier}
              label={t.name}
              sub={t.available ? t.description : `${t.description} (disabled)`}
              selected={isSelected(t.identifier)}
              onAdd={() => onAdd({ kind: 'function', name: t.name })}
            />
          ))}
        </ToolGroup>
        {inventory.mcp_servers.length > 0 && (
          <ToolGroup title="MCP servers" icon={<Plug className="h-3.5 w-3.5" />}>
            {inventory.mcp_servers.map((s) => (
              <div key={s.identifier}>
                <ToolRow
                  label={s.name}
                  sub={s.available ? 'whole server' : 'not loaded'}
                  selected={isSelected(s.identifier)}
                  onAdd={() => onAdd({ kind: 'mcp', name: s.name })}
                />
                {s.tools.map((t) => (
                  <div key={t.identifier} className="pl-4">
                    <ToolRow
                      label={t.name}
                      sub={t.description}
                      selected={isSelected(t.identifier)}
                      onAdd={() => onAdd({ kind: 'mcp', name: s.name, allowedTools: [t.name] })}
                    />
                  </div>
                ))}
              </div>
            ))}
          </ToolGroup>
        )}
        {inventory.skills.length > 0 && (
          <ToolGroup title="Skills" icon={<Sparkles className="h-3.5 w-3.5" />}>
            {inventory.skills.map((s) => (
              <ToolRow
                key={s.identifier}
                label={s.name}
                sub={s.available ? s.description : `${s.description} (disabled)`}
                selected={isSelected(s.identifier)}
                onAdd={() => onAdd({ kind: 'skill', name: s.name })}
              />
            ))}
          </ToolGroup>
        )}
      </div>
    </>
  )
}

function ToolGroup({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div className="mb-1">
      <div className="flex items-center gap-1.5 px-1 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        {icon} {title}
      </div>
      {children}
    </div>
  )
}

function ToolRow({
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
