/**
 * Paint connector object, anchor binding, and scene identity
 * (CTR-0191, PRP-0125 Part B / UDR-0108 D11-D20).
 *
 * Arrow and Line are ONE persisted class (D11): the Line tool is a connector
 * with both heads set to `none`. Two toolbar entries, one implementation, one
 * serialization, one snapping behavior.
 *
 * CRITICAL -- the class MUST stay registered in Fabric's classRegistry with a
 * matching toObject / fromObject round-trip (D12). The Paint editor's undo
 * history is `JSON.stringify(canvas.toJSON())` and its persistence format
 * (CTR-0161) is the same serialization, so an unregistered class is not a
 * rendering bug: the first Undo after drawing an arrow silently deletes every
 * arrow, and the next Attach writes that deletion to the scene sidecar.
 * `paint/connector.round-trip` in the invariant suite pins this.
 */
import { type Canvas, Control, classRegistry, FabricObject, Point, type TMat2D, type Transform } from 'fabric'
import { SNAP_RADIUS_SCREEN_PX } from './constants'

export type ArrowHead = 'none' | 'arrow' | 'triangle' | 'diamond' | 'circle' | 'bar'
export type ConnectorRouting = 'straight' | 'elbow' | 'curved'
export type AnchorSide = 'auto' | 'n' | 'e' | 's' | 'w' | 'center'

export interface ConnectorPoint {
  x: number
  y: number
}

/** A bound endpoint stores a REFERENCE, never a copied coordinate (D14). */
export interface ConnectorBinding {
  pid: string
  anchor: AnchorSide
}

export const ARROW_HEADS: ArrowHead[] = ['none', 'arrow', 'triangle', 'diamond', 'circle', 'bar']
export const CONNECTOR_ROUTINGS: ConnectorRouting[] = ['straight', 'elbow', 'curved']

/**
 * Every custom field MUST appear here, or it is dropped by toObject and lost on
 * the next undo (D12). The invariant test asserts this list against the class.
 */
export const CONNECTOR_PROPS = ['points', 'routing', 'headStart', 'headEnd', 'bindStart', 'bindEnd', 'bend'] as const

interface ConnectorOptions {
  points?: ConnectorPoint[]
  routing?: ConnectorRouting
  headStart?: ArrowHead
  headEnd?: ArrowHead
  bindStart?: ConnectorBinding | null
  bindEnd?: ConnectorBinding | null
  bend?: ConnectorPoint | null
  [key: string]: unknown
}

function headSize(strokeWidth: number): number {
  return Math.max(8, strokeWidth * 3)
}

/** Draw one arrow head at `tip`, pointing along `angle` (radians). */
function drawHead(
  ctx: CanvasRenderingContext2D,
  tip: ConnectorPoint,
  angle: number,
  style: ArrowHead,
  size: number,
  color: string,
): void {
  if (style === 'none') return
  ctx.save()
  ctx.translate(tip.x, tip.y)
  ctx.rotate(angle)
  ctx.fillStyle = color
  ctx.strokeStyle = color
  ctx.lineWidth = Math.max(1, size / 6)
  ctx.lineCap = 'butt'
  ctx.beginPath()
  switch (style) {
    case 'arrow':
      // Open V, drawn as a stroke so it reads as a line-art arrow.
      ctx.moveTo(-size, -size * 0.5)
      ctx.lineTo(0, 0)
      ctx.lineTo(-size, size * 0.5)
      ctx.lineWidth = Math.max(1, size / 3)
      ctx.lineJoin = 'round'
      ctx.lineCap = 'round'
      ctx.stroke()
      ctx.restore()
      return
    case 'triangle':
      ctx.moveTo(0, 0)
      ctx.lineTo(-size, -size * 0.5)
      ctx.lineTo(-size, size * 0.5)
      ctx.closePath()
      break
    case 'diamond':
      ctx.moveTo(0, 0)
      ctx.lineTo(-size * 0.6, -size * 0.5)
      ctx.lineTo(-size * 1.2, 0)
      ctx.lineTo(-size * 0.6, size * 0.5)
      ctx.closePath()
      break
    case 'circle':
      ctx.arc(-size * 0.5, 0, size * 0.5, 0, Math.PI * 2)
      break
    case 'bar':
      ctx.moveTo(0, -size * 0.6)
      ctx.lineTo(0, size * 0.6)
      ctx.lineWidth = Math.max(1, size / 3)
      ctx.stroke()
      ctx.restore()
      return
  }
  ctx.fill()
  ctx.restore()
}

/**
 * A two-endpoint connector: straight / elbow / curved, with an independently
 * selectable head at each end, optionally BOUND to another object (D11 / D14).
 *
 * Points are stored in ABSOLUTE scene coordinates. `left` / `top` / `width` /
 * `height` are derived from them, and scaling / rotation are locked: a
 * bounding-box resize applies non-uniform scaleX/scaleY, which distorts stroke
 * width and heads on a diagonal (D16).
 */
export class Connector extends FabricObject {
  static type = 'Connector'

  static customProperties: string[] = [...CONNECTOR_PROPS]

  declare points: ConnectorPoint[]
  declare routing: ConnectorRouting
  declare headStart: ArrowHead
  declare headEnd: ArrowHead
  declare bindStart: ConnectorBinding | null
  declare bindEnd: ConnectorBinding | null
  declare bend: ConnectorPoint | null

  /**
   * Set while an endpoint drag is hovering a snap target; committed to
   * bindStart / bindEnd on mouse:up by the editor (D15). Never serialized.
   */
  pendingBind: { end: 'start' | 'end'; pid: string; anchor: AnchorSide } | null = null
  /** Snap target highlight, drawn by the editor's after:render hook. */
  snapHighlight: { x: number; y: number } | null = null

  static ownDefaults: Record<string, unknown> = {
    points: [],
    routing: 'straight' as ConnectorRouting,
    headStart: 'none' as ArrowHead,
    headEnd: 'arrow' as ArrowHead,
    bindStart: null,
    bindEnd: null,
    bend: null,
    stroke: '#111827',
    strokeWidth: 4,
    fill: '',
    objectCaching: false,
    originX: 'left',
    originY: 'top',
    lockScalingX: true,
    lockScalingY: true,
    lockRotation: true,
    hasBorders: false,
    perPixelTargetFind: true,
    padding: 6,
  }

  static getDefaults(): Record<string, unknown> {
    return { ...FabricObject.getDefaults(), ...Connector.ownDefaults }
  }

  constructor(options: ConnectorOptions = {}) {
    super()
    Object.assign(this, Connector.getDefaults())
    this.setOptions(options as Record<string, unknown>)
    if (!Array.isArray(this.points) || this.points.length < 2) {
      this.points = [
        { x: 0, y: 0 },
        { x: 1, y: 1 },
      ]
    }
    // Endpoint handles instead of the 8-handle bounding box (D16).
    this.controls = buildConnectorControls()
    this.updateGeometry()
  }

  /** The polyline actually drawn, derived from the endpoints and routing (D17). */
  renderPoints(): ConnectorPoint[] {
    const [a, b] = [this.points[0], this.points[this.points.length - 1]]
    if (this.routing === 'elbow') {
      const dx = Math.abs(b.x - a.x)
      const dy = Math.abs(b.y - a.y)
      if (dx >= dy) {
        const mx = this.bend?.x ?? (a.x + b.x) / 2
        return [a, { x: mx, y: a.y }, { x: mx, y: b.y }, b]
      }
      const my = this.bend?.y ?? (a.y + b.y) / 2
      return [a, { x: a.x, y: my }, { x: b.x, y: my }, b]
    }
    if (this.routing === 'curved') {
      return [a, this.controlPoint(), b]
    }
    return [a, b]
  }

  /** Quadratic control point for `curved` routing. */
  controlPoint(): ConnectorPoint {
    if (this.bend) return this.bend
    const [a, b] = [this.points[0], this.points[this.points.length - 1]]
    const mx = (a.x + b.x) / 2
    const my = (a.y + b.y) / 2
    const dx = b.x - a.x
    const dy = b.y - a.y
    const len = Math.hypot(dx, dy) || 1
    // Bow out perpendicular to the chord by 20% of its length.
    return { x: mx - (dy / len) * len * 0.2, y: my + (dx / len) * len * 0.2 }
  }

  /** Default position of the bend handle, for `elbow` / `curved`. */
  bendHandle(): ConnectorPoint {
    if (this.routing === 'curved') return this.controlPoint()
    const pts = this.renderPoints()
    const mid = pts[Math.floor(pts.length / 2)] ?? pts[0]
    return mid
  }

  translatePoints(dx: number, dy: number): void {
    this.points = this.points.map((p) => ({ x: p.x + dx, y: p.y + dy }))
    if (this.bend) this.bend = { x: this.bend.x + dx, y: this.bend.y + dy }
  }

  /** Recompute left/top/width/height from the rendered polyline. */
  updateGeometry(): void {
    const pts = this.renderPoints()
    let minX = Number.POSITIVE_INFINITY
    let minY = Number.POSITIVE_INFINITY
    let maxX = Number.NEGATIVE_INFINITY
    let maxY = Number.NEGATIVE_INFINITY
    for (const p of pts) {
      minX = Math.min(minX, p.x)
      minY = Math.min(minY, p.y)
      maxX = Math.max(maxX, p.x)
      maxY = Math.max(maxY, p.y)
    }
    if (!Number.isFinite(minX)) return
    this.set({
      left: minX,
      top: minY,
      width: Math.max(1, maxX - minX),
      height: Math.max(1, maxY - minY),
      scaleX: 1,
      scaleY: 1,
      angle: 0,
    })
    this.setCoords()
  }

  /**
   * Fabric moves an object by mutating left/top. Translate the absolute points
   * by however far left/top drifted from the geometry they imply, so a body
   * drag moves both endpoints.
   */
  applyMoveDelta(): boolean {
    const pts = this.renderPoints()
    let minX = Number.POSITIVE_INFINITY
    let minY = Number.POSITIVE_INFINITY
    for (const p of pts) {
      minX = Math.min(minX, p.x)
      minY = Math.min(minY, p.y)
    }
    const dx = (this.left ?? 0) - minX
    const dy = (this.top ?? 0) - minY
    if (Math.abs(dx) < 1e-6 && Math.abs(dy) < 1e-6) return false
    this.translatePoints(dx, dy)
    this.updateGeometry()
    return true
  }

  _render(ctx: CanvasRenderingContext2D): void {
    const pts = this.renderPoints()
    const cx = (this.left ?? 0) + (this.width ?? 0) / 2
    const cy = (this.top ?? 0) + (this.height ?? 0) / 2
    const rel = (p: ConnectorPoint): ConnectorPoint => ({ x: p.x - cx, y: p.y - cy })
    const color = typeof this.stroke === 'string' && this.stroke ? this.stroke : '#111827'
    const sw = this.strokeWidth ?? 4

    ctx.save()
    ctx.strokeStyle = color
    ctx.lineWidth = sw
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    if (Array.isArray(this.strokeDashArray) && this.strokeDashArray.length > 0) {
      ctx.setLineDash(this.strokeDashArray)
    }
    ctx.beginPath()
    if (this.routing === 'curved') {
      const a = rel(pts[0])
      const c = rel(pts[1])
      const b = rel(pts[2])
      ctx.moveTo(a.x, a.y)
      ctx.quadraticCurveTo(c.x, c.y, b.x, b.y)
    } else {
      const r = pts.map(rel)
      ctx.moveTo(r[0].x, r[0].y)
      for (let i = 1; i < r.length; i += 1) ctx.lineTo(r[i].x, r[i].y)
    }
    ctx.stroke()
    ctx.restore()

    // Heads point OUTWARD along the last drawn segment at each end.
    const size = headSize(sw)
    const first = rel(pts[0])
    const second = rel(pts[1] ?? pts[0])
    const last = rel(pts[pts.length - 1])
    const beforeLast = rel(pts[pts.length - 2] ?? pts[pts.length - 1])
    drawHead(ctx, first, Math.atan2(first.y - second.y, first.x - second.x), this.headStart, size, color)
    drawHead(ctx, last, Math.atan2(last.y - beforeLast.y, last.x - beforeLast.x), this.headEnd, size, color)
  }

  toObject(propertiesToInclude: string[] = []): Record<string, unknown> {
    return super.toObject([...CONNECTOR_PROPS, ...propertiesToInclude])
  }

  /**
   * `type` is stripped: Fabric derives it from the class and logs
   * "Setting type has no effect" when a serialized document tries to assign it,
   * which would print once per revived connector on every undo.
   */
  static fromObject(object: Record<string, unknown>): Promise<Connector> {
    const { type: _type, ...rest } = object
    return Promise.resolve(new Connector(rest as ConnectorOptions))
  }
}

// ---- Endpoint controls (D16) ----

function endpointPositionHandler(index: 'start' | 'end' | 'bend') {
  return (_dim: Point, _finalMatrix: TMat2D, fabricObject: FabricObject): Point => {
    const conn = fabricObject as Connector
    const p =
      index === 'bend' ? conn.bendHandle() : index === 'start' ? conn.points[0] : conn.points[conn.points.length - 1]
    const vpt = conn.canvas?.viewportTransform
    const pt = new Point(p.x, p.y)
    return vpt ? pt.transform(vpt) : pt
  }
}

/**
 * Move one endpoint, snapping it to a nearby anchor unless suppressed (D15).
 *
 * Shared by the endpoint controls and by drag-to-draw so both authoring paths
 * snap identically. The binding for that end is always RELEASED first: a
 * hovering snap re-arms it as `pendingBind`, which the editor commits on
 * mouse-up. Dragging an endpoint off its target therefore unbinds it, which is
 * what the gesture means.
 */
export function placeConnectorEndpoint(
  conn: Connector,
  end: 'start' | 'end',
  x: number,
  y: number,
  suppressSnap = false,
): void {
  const canvas = conn.canvas ?? null
  let px = x
  let py = y
  conn.pendingBind = null
  conn.snapHighlight = null
  if (end === 'start') conn.bindStart = null
  else conn.bindEnd = null
  if (canvas && !suppressSnap) {
    const zoom = canvas.getZoom() || 1
    const hit = findSnapTarget(canvas, x, y, SNAP_RADIUS_SCREEN_PX / zoom, conn)
    if (hit) {
      px = hit.point.x
      py = hit.point.y
      conn.pendingBind = { end, pid: hit.pid, anchor: hit.anchor }
      conn.snapHighlight = { x: hit.point.x, y: hit.point.y }
    }
  }
  const idx = end === 'start' ? 0 : conn.points.length - 1
  conn.points[idx] = { x: px, y: py }
  conn.updateGeometry()
}

function endpointActionHandler(index: 'start' | 'end' | 'bend') {
  return (eventData: MouseEvent | TouchEvent, transform: Transform, x: number, y: number): boolean => {
    const conn = transform.target as Connector
    if (index === 'bend') {
      conn.bend = { x, y }
      conn.updateGeometry()
      return true
    }
    placeConnectorEndpoint(conn, index, x, y, 'altKey' in eventData && eventData.altKey === true)
    return true
  }
}

function renderEndpointControl(
  ctx: CanvasRenderingContext2D,
  left: number,
  top: number,
  _styleOverride: unknown,
  fabricObject: FabricObject,
): void {
  const conn = fabricObject as Connector
  const bound = conn.bindStart !== null || conn.bindEnd !== null
  ctx.save()
  ctx.beginPath()
  ctx.arc(left, top, 5, 0, Math.PI * 2)
  ctx.fillStyle = bound ? '#22c55e' : '#ffffff'
  ctx.strokeStyle = '#2563eb'
  ctx.lineWidth = 1.5
  ctx.fill()
  ctx.stroke()
  ctx.restore()
}

function buildConnectorControls(): Record<string, Control> {
  return {
    p0: new Control({
      actionName: 'modifyConnector',
      cursorStyle: 'crosshair',
      positionHandler: endpointPositionHandler('start'),
      actionHandler: endpointActionHandler('start'),
      render: renderEndpointControl,
    }),
    p1: new Control({
      actionName: 'modifyConnector',
      cursorStyle: 'crosshair',
      positionHandler: endpointPositionHandler('end'),
      actionHandler: endpointActionHandler('end'),
      render: renderEndpointControl,
    }),
    bend: new Control({
      actionName: 'modifyConnector',
      cursorStyle: 'move',
      positionHandler: endpointPositionHandler('bend'),
      actionHandler: endpointActionHandler('bend'),
      render: renderEndpointControl,
      visible: false,
    }),
  }
}

/** Show the bend handle only for routings that have one (D17). */
export function syncConnectorControls(conn: Connector): void {
  const bend = conn.controls?.bend
  if (bend) bend.visible = conn.routing !== 'straight'
}

// ---- Scene identity (D13) ----

let sceneRegistered = false

/**
 * Register the Connector class and the scene-wide `pid` custom property.
 *
 * `FabricObject.customProperties` is Fabric's documented serialization
 * extension point and is concatenated for EVERY object, so pushing `pid` there
 * gives all objects a stable persisted id -- not only connectors (D13).
 */
export function registerPaintScene(): void {
  if (sceneRegistered) return
  sceneRegistered = true
  if (!FabricObject.customProperties.includes('pid')) {
    FabricObject.customProperties.push('pid')
  }
  classRegistry.setClass(Connector)
}

registerPaintScene()

function newPid(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `p-${Math.random().toString(36).slice(2)}-${performance.now().toString(36)}`
}

export function getPid(o: FabricObject): string | undefined {
  return (o as unknown as { pid?: string }).pid
}

/** Assign a `pid` if the object has none (legacy scenes mint on load, D13). */
export function ensurePid(o: FabricObject): string {
  const existing = getPid(o)
  if (existing) return existing
  const pid = newPid()
  ;(o as unknown as { pid?: string }).pid = pid
  return pid
}

/** Force a NEW id, for a duplicated or pasted object (D19). */
export function assignFreshPid(o: FabricObject): string {
  const pid = newPid()
  ;(o as unknown as { pid?: string }).pid = pid
  return pid
}

/** Give every object in the scene a stable id. Idempotent. */
export function ensureScenePids(canvas: Canvas): void {
  for (const o of canvas.getObjects()) ensurePid(o)
}

export function findByPid(canvas: Canvas, pid: string): FabricObject | null {
  for (const o of canvas.getObjects()) {
    if (getPid(o) === pid) return o
  }
  return null
}

// ---- Anchors, snapping, binding (D14 / D15) ----

/** The five anchor candidates of an object's bounding box, in scene units. */
export function anchorPoints(o: FabricObject): { anchor: AnchorSide; point: ConnectorPoint }[] {
  const r = o.getBoundingRect()
  const cx = r.left + r.width / 2
  const cy = r.top + r.height / 2
  return [
    { anchor: 'n', point: { x: cx, y: r.top } },
    { anchor: 'e', point: { x: r.left + r.width, y: cy } },
    { anchor: 's', point: { x: cx, y: r.top + r.height } },
    { anchor: 'w', point: { x: r.left, y: cy } },
    { anchor: 'center', point: { x: cx, y: cy } },
  ]
}

/**
 * Resolve an anchor to a scene point. `auto` picks the edge midpoint nearest
 * `toward`, so a connector re-aims at the facing edges as shapes move (D14).
 */
export function anchorPoint(o: FabricObject, anchor: AnchorSide, toward: ConnectorPoint): ConnectorPoint {
  const candidates = anchorPoints(o)
  if (anchor !== 'auto') {
    return candidates.find((c) => c.anchor === anchor)?.point ?? candidates[4].point
  }
  let best = candidates[0]
  let bestD = Number.POSITIVE_INFINITY
  for (const c of candidates) {
    if (c.anchor === 'center') continue
    const d = Math.hypot(c.point.x - toward.x, c.point.y - toward.y)
    if (d < bestD) {
      bestD = d
      best = c
    }
  }
  return best.point
}

export function findSnapTarget(
  canvas: Canvas,
  x: number,
  y: number,
  radiusScene: number,
  exclude: FabricObject,
): { pid: string; anchor: AnchorSide; point: ConnectorPoint } | null {
  let best: { pid: string; anchor: AnchorSide; point: ConnectorPoint } | null = null
  let bestD = radiusScene
  for (const o of canvas.getObjects()) {
    if (o === exclude || o instanceof Connector || !o.visible) continue
    for (const c of anchorPoints(o)) {
      const d = Math.hypot(c.point.x - x, c.point.y - y)
      if (d <= bestD) {
        bestD = d
        best = { pid: ensurePid(o), anchor: c.anchor, point: c.point }
      }
    }
  }
  return best
}

/**
 * Re-entrancy guard (D18). Recomputation must not emit modification events that
 * re-trigger recomputation, and must never add a history entry of its own --
 * moving a shape is ONE user action no matter how many connectors follow it.
 */
let refreshing = false

/**
 * Recompute every bound endpoint from its target's current geometry.
 *
 * Dangling references DEGRADE, never fail (D14): a binding whose `pid` is gone
 * is dropped and the endpoint keeps its last cached coordinate. The connector
 * must not disappear, must not throw, and must not block the scene from
 * loading.
 */
export function refreshConnectorBindings(canvas: Canvas): boolean {
  if (refreshing) return false
  refreshing = true
  let changed = false
  try {
    for (const o of canvas.getObjects()) {
      if (!(o instanceof Connector)) continue
      const conn = o
      const ends: ('start' | 'end')[] = ['start', 'end']
      for (const end of ends) {
        const bind = end === 'start' ? conn.bindStart : conn.bindEnd
        if (!bind) continue
        const target = findByPid(canvas, bind.pid)
        if (!target || target === conn) {
          if (end === 'start') conn.bindStart = null
          else conn.bindEnd = null
          changed = true
          continue
        }
        const otherIdx = end === 'start' ? conn.points.length - 1 : 0
        const next = anchorPoint(target, bind.anchor, conn.points[otherIdx])
        const idx = end === 'start' ? 0 : conn.points.length - 1
        const cur = conn.points[idx]
        if (Math.abs(cur.x - next.x) > 0.01 || Math.abs(cur.y - next.y) > 0.01) {
          conn.points[idx] = next
          changed = true
        }
      }
      if (changed) {
        conn.updateGeometry()
        syncConnectorControls(conn)
      }
    }
  } finally {
    refreshing = false
  }
  return changed
}

/** Commit a hovering snap into a real binding on mouse-up (D15). */
export function commitPendingBind(conn: Connector): boolean {
  const pending = conn.pendingBind
  conn.pendingBind = null
  conn.snapHighlight = null
  if (!pending) return false
  const bind: ConnectorBinding = { pid: pending.pid, anchor: pending.anchor }
  if (pending.end === 'start') conn.bindStart = bind
  else conn.bindEnd = bind
  return true
}

/**
 * Remap or drop bindings on a duplicated connector (D19). When a connector and
 * its targets are duplicated together the copies bind to the copies; a
 * connector duplicated alone drops its bindings (falling back to coordinates)
 * rather than silently pointing at the originals.
 */
export function remapDuplicatedBindings(objects: FabricObject[], pidMap: Map<string, string>): void {
  for (const o of objects) {
    if (!(o instanceof Connector)) continue
    const remap = (b: ConnectorBinding | null): ConnectorBinding | null => {
      if (!b) return null
      const next = pidMap.get(b.pid)
      return next ? { pid: next, anchor: b.anchor } : null
    }
    o.bindStart = remap(o.bindStart)
    o.bindEnd = remap(o.bindEnd)
  }
}

export function isConnector(o: FabricObject | null | undefined): o is Connector {
  return o instanceof Connector
}
