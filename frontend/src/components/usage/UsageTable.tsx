import { ArrowDown, ArrowUp } from 'lucide-react'
import { type ReactNode, useMemo, useState } from 'react'
import { cn } from '@/lib/utils'

/**
 * Small sortable table for the Usage Dashboard (CTR-0215). A column's ``value`` is
 * what it sorts by; ``undefined`` (not reported) always sorts last in either
 * direction, so a missing figure never masquerades as the smallest one.
 */

export interface UsageColumn<T> {
  id: string
  label: string
  value: (row: T) => number | string | undefined
  render?: (row: T) => ReactNode
  align?: 'left' | 'right'
  className?: string
}

export function UsageTable<T>({
  rows,
  columns,
  rowKey,
  initialSort,
  empty = 'No rows.',
}: {
  rows: T[]
  columns: UsageColumn<T>[]
  rowKey: (row: T) => string
  initialSort?: { id: string; desc: boolean }
  empty?: string
}) {
  const [sort, setSort] = useState(initialSort ?? { id: columns[0]?.id ?? '', desc: false })

  const sorted = useMemo(() => {
    const column = columns.find((c) => c.id === sort.id)
    if (!column) return rows
    return [...rows].sort((a, b) => {
      const va = column.value(a)
      const vb = column.value(b)
      if (va === undefined && vb === undefined) return 0
      if (va === undefined) return 1
      if (vb === undefined) return -1
      const cmp = typeof va === 'number' && typeof vb === 'number' ? va - vb : String(va).localeCompare(String(vb))
      return sort.desc ? -cmp : cmp
    })
  }, [rows, columns, sort])

  if (rows.length === 0) return <p className="px-1 py-3 text-xs text-muted-foreground">{empty}</p>

  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-xs">
        <thead className="bg-muted/50">
          <tr>
            {columns.map((c) => (
              <th
                key={c.id}
                className={cn(
                  'whitespace-nowrap px-2 py-1.5 font-medium',
                  c.align === 'right' ? 'text-right' : 'text-left',
                )}>
                <button
                  type="button"
                  className="inline-flex items-center gap-1 hover:text-foreground"
                  onClick={() => setSort((s) => ({ id: c.id, desc: s.id === c.id ? !s.desc : true }))}>
                  {c.label}
                  {sort.id === c.id &&
                    (sort.desc ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" />)}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={rowKey(row)} className="border-t">
              {columns.map((c) => (
                <td
                  key={c.id}
                  className={cn(
                    'whitespace-nowrap px-2 py-1.5',
                    c.align === 'right' && 'text-right font-mono tabular-nums',
                    c.className,
                  )}>
                  {c.render ? c.render(row) : String(c.value(row) ?? '--')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
