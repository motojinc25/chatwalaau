/**
 * Artboard size control (CTR-0160 v3, PRP-0125 / UDR-0108 D2 / D3).
 *
 * Named ratio presets, a portrait/landscape swap, a bounded Custom lane with an
 * optional aspect lock, and Fit to window. An over-budget AREA is REJECTED with
 * an inline message naming the budget rather than silently clamped -- silently
 * changing the requested aspect ratio is worse than refusing the value -- and
 * the check happens when the size is ENTERED, so the operator is never told
 * "too big" only after spending an hour on the drawing.
 */
import { Check, ChevronDown, Lock, Maximize, RotateCw, Unlock } from 'lucide-react'
import { useEffect, useState } from 'react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import {
  CANVAS_PRESET_GROUPS,
  clampEdge,
  exceedsAreaBudget,
  MAX_CANVAS_EDGE,
  MAX_CANVAS_PX,
  MIN_CANVAS,
  megapixels,
} from './constants'

interface PaintCanvasSizeControlProps {
  size: { w: number; h: number }
  fitMode: boolean
  onApply: (w: number, h: number) => void
  onFitToWindow: () => void
}

const budgetMpx = (MAX_CANVAS_PX / 1_000_000).toFixed(0)

export function PaintCanvasSizeControl({ size, fitMode, onApply, onFitToWindow }: PaintCanvasSizeControlProps) {
  const [open, setOpen] = useState(false)
  const [draftW, setDraftW] = useState(String(size.w))
  const [draftH, setDraftH] = useState(String(size.h))
  const [lockRatio, setLockRatio] = useState(false)

  // Keep the Custom fields in step with the live artboard while the menu is
  // closed (a preset, a swap, or Fit all change the size behind them).
  useEffect(() => {
    if (open) return
    setDraftW(String(size.w))
    setDraftH(String(size.h))
  }, [open, size.w, size.h])

  const w = Number(draftW)
  const h = Number(draftH)
  const draftValid = Number.isFinite(w) && Number.isFinite(h) && w >= MIN_CANVAS && h >= MIN_CANVAS
  const overBudget = draftValid && exceedsAreaBudget(w, h)

  const applyCustom = () => {
    if (!draftValid || overBudget) return
    onApply(clampEdge(w), clampEdge(h))
    setOpen(false)
  }

  const editDraft = (dim: 'w' | 'h', raw: string) => {
    if (dim === 'w') {
      setDraftW(raw)
      if (lockRatio && size.w > 0) {
        const n = Number(raw)
        if (Number.isFinite(n)) setDraftH(String(Math.round((n * size.h) / size.w)))
      }
      return
    }
    setDraftH(raw)
    if (lockRatio && size.h > 0) {
      const n = Number(raw)
      if (Number.isFinite(n)) setDraftW(String(Math.round((n * size.w) / size.h)))
    }
  }

  return (
    <div className="flex items-center gap-1 text-xs text-zinc-500">
      <span>Canvas</span>
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            title="Artboard size"
            aria-label="Artboard size"
            className="inline-flex h-7 items-center gap-1 rounded border border-zinc-300 bg-white px-2 tabular-nums text-zinc-700 hover:bg-zinc-50">
            {fitMode ? 'Fit' : `${size.w} x ${size.h}`}
            <ChevronDown className="h-3 w-3 text-zinc-400" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-64 p-2">
          {CANVAS_PRESET_GROUPS.map((g) => (
            <div key={g.ratio} className="mb-1.5">
              <div className="px-1 pb-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-400">{g.ratio}</div>
              <div className="grid grid-cols-2 gap-1">
                {g.presets.map((p) => {
                  const active = !fitMode && size.w === p.w && size.h === p.h
                  return (
                    <button
                      key={p.label}
                      type="button"
                      onClick={() => {
                        onApply(p.w, p.h)
                        setOpen(false)
                      }}
                      className={cn(
                        'flex items-center justify-between rounded px-2 py-1 text-left text-xs tabular-nums hover:bg-zinc-100',
                        active && 'bg-blue-100 text-blue-800',
                      )}>
                      {p.label}
                      {active && <Check className="h-3 w-3" />}
                    </button>
                  )
                })}
              </div>
            </div>
          ))}

          <div className="my-1.5 h-px bg-zinc-200" />

          <div className="flex items-center gap-1 px-1">
            <button
              type="button"
              onClick={() => onApply(size.h, size.w)}
              className="inline-flex flex-1 items-center gap-1 rounded px-2 py-1 text-xs text-zinc-700 hover:bg-zinc-100">
              <RotateCw className="h-3 w-3" /> Swap orientation
            </button>
            <button
              type="button"
              onClick={() => {
                onFitToWindow()
                setOpen(false)
              }}
              className={cn(
                'inline-flex flex-1 items-center gap-1 rounded px-2 py-1 text-xs text-zinc-700 hover:bg-zinc-100',
                fitMode && 'bg-blue-100 text-blue-800',
              )}>
              <Maximize className="h-3 w-3" /> Fit to window
            </button>
          </div>

          <div className="my-1.5 h-px bg-zinc-200" />

          <div className="px-1 pb-1">
            <div className="pb-1 text-[10px] font-medium uppercase tracking-wide text-zinc-400">Custom</div>
            <div className="flex items-center gap-1">
              <input
                type="number"
                min={MIN_CANVAS}
                max={MAX_CANVAS_EDGE}
                value={draftW}
                onChange={(e) => editDraft('w', e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && applyCustom()}
                aria-label="Canvas width"
                className="h-7 w-20 rounded border border-zinc-300 px-1 text-center tabular-nums"
              />
              <span>x</span>
              <input
                type="number"
                min={MIN_CANVAS}
                max={MAX_CANVAS_EDGE}
                value={draftH}
                onChange={(e) => editDraft('h', e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && applyCustom()}
                aria-label="Canvas height"
                className="h-7 w-20 rounded border border-zinc-300 px-1 text-center tabular-nums"
              />
              <button
                type="button"
                title={lockRatio ? 'Aspect ratio locked' : 'Aspect ratio unlocked'}
                aria-label="Toggle aspect ratio lock"
                onClick={() => setLockRatio((v) => !v)}
                className={cn(
                  'inline-flex h-7 w-7 items-center justify-center rounded border border-zinc-300',
                  lockRatio ? 'bg-blue-100 text-blue-700' : 'text-zinc-500 hover:bg-zinc-50',
                )}>
                {lockRatio ? <Lock className="h-3 w-3" /> : <Unlock className="h-3 w-3" />}
              </button>
            </div>
            {overBudget ? (
              <p className="pt-1 text-[11px] leading-snug text-red-600">
                {megapixels(clampEdge(w), clampEdge(h))} MPx exceeds the {budgetMpx} MPx budget. Pick a smaller size --
                the attached PNG has to stay under the upload limit.
              </p>
            ) : (
              <p className="pt-1 text-[11px] leading-snug text-zinc-400">
                Up to {MAX_CANVAS_EDGE} px per side and {budgetMpx} MPx total.
              </p>
            )}
            <button
              type="button"
              disabled={!draftValid || overBudget}
              onClick={applyCustom}
              className="mt-1 h-7 w-full rounded bg-zinc-900 text-xs text-white disabled:opacity-40">
              Apply
            </button>
          </div>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
