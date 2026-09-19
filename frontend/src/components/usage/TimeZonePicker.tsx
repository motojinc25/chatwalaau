import { Globe } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { browserTimeZone, supportedTimeZones } from '@/lib/usageApi'
import { cn } from '@/lib/utils'

/**
 * Always-visible calendar-zone control (UDR-0155 D2). The selected zone is what every
 * summary and export request carries; "Browser" resets to the viewer's own zone.
 */
export function TimeZonePicker({ value, onChange }: { value: string; onChange: (tz: string) => void }) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const zones = useMemo(() => supportedTimeZones(), [])
  const browser = browserTimeZone()

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const needle = filter.trim().toLowerCase()
  const shown = needle ? zones.filter((z) => z.toLowerCase().includes(needle)) : zones

  const pick = (tz: string) => {
    onChange(tz)
    setOpen(false)
    setFilter('')
  }

  return (
    <div ref={rootRef} className="relative">
      <Button
        variant="outline"
        size="sm"
        className="h-7 gap-1.5 px-2 text-xs"
        onClick={() => setOpen((o) => !o)}
        title="Time zone used for days and months"
        aria-haspopup="listbox"
        aria-expanded={open}>
        <Globe className="h-3.5 w-3.5" />
        <span className="font-mono">{value}</span>
        {value === browser && <span className="text-muted-foreground">(browser)</span>}
      </Button>
      {open && (
        <div className="absolute right-0 top-8 z-20 w-72 rounded-md border bg-popover p-2 shadow-md">
          <Input
            autoFocus
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search time zones"
            onKeyDown={(e) => {
              // Close the list, not the whole dashboard dialog.
              if (e.key === 'Escape') {
                e.stopPropagation()
                setOpen(false)
              }
            }}
            className="h-7 text-xs"
          />
          <button
            type="button"
            className="mt-2 w-full rounded px-2 py-1 text-left text-xs hover:bg-accent"
            onClick={() => pick(browser)}>
            Browser ({browser})
          </button>
          <div role="listbox" aria-label="Time zones" className="mt-1 max-h-64 overflow-y-auto border-t pt-1">
            {shown.map((tz) => (
              <button
                type="button"
                role="option"
                aria-selected={tz === value}
                key={tz}
                className={cn(
                  'block w-full rounded px-2 py-1 text-left font-mono text-xs hover:bg-accent',
                  tz === value && 'bg-accent',
                )}
                onClick={() => pick(tz)}>
                {tz}
              </button>
            ))}
            {shown.length === 0 && <p className="px-2 py-1 text-xs text-muted-foreground">No match.</p>}
          </div>
        </div>
      )}
    </div>
  )
}
