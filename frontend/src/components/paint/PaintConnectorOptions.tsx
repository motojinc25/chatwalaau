/**
 * Connector routing / arrow-head controls (CTR-0160 v3 + CTR-0191,
 * PRP-0125 / UDR-0108 D11 / D17).
 *
 * `routing` is the "ArrowType" axis and `headStart` / `headEnd` are the
 * "ArrowHeads" axis. Both are plain fields on the one Connector class, so the
 * Line tool is simply the connector with both heads set to `none`.
 */
import { ARROW_HEADS, type ArrowHead, CONNECTOR_ROUTINGS, type ConnectorRouting } from './connector'

interface PaintConnectorOptionsProps {
  routing: ConnectorRouting
  headStart: ArrowHead
  headEnd: ArrowHead
  onChange: (patch: { routing?: ConnectorRouting; headStart?: ArrowHead; headEnd?: ArrowHead }) => void
}

const selectCls =
  'h-7 rounded border border-zinc-300 bg-white px-1 text-xs capitalize text-zinc-700 outline-none focus:ring-1 focus:ring-zinc-400'

export function PaintConnectorOptions({ routing, headStart, headEnd, onChange }: PaintConnectorOptionsProps) {
  return (
    <div className="flex items-center gap-1 text-xs text-zinc-500">
      <span title="Connector routing (ArrowType)">Type</span>
      <select
        value={routing}
        aria-label="Connector routing"
        onChange={(e) => onChange({ routing: e.target.value as ConnectorRouting })}
        className={selectCls}>
        {CONNECTOR_ROUTINGS.map((r) => (
          <option key={r} value={r}>
            {r}
          </option>
        ))}
      </select>
      <span className="pl-1" title="Arrow heads">
        Heads
      </span>
      <select
        value={headStart}
        aria-label="Start arrow head"
        onChange={(e) => onChange({ headStart: e.target.value as ArrowHead })}
        className={selectCls}>
        {ARROW_HEADS.map((h) => (
          <option key={h} value={h}>
            {h}
          </option>
        ))}
      </select>
      <select
        value={headEnd}
        aria-label="End arrow head"
        onChange={(e) => onChange({ headEnd: e.target.value as ArrowHead })}
        className={selectCls}>
        {ARROW_HEADS.map((h) => (
          <option key={h} value={h}>
            {h}
          </option>
        ))}
      </select>
    </div>
  )
}
