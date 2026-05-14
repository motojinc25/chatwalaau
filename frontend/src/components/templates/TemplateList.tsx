import { useMemo } from 'react'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'
import type { PromptTemplate } from '@/types/chat'

interface TemplateListProps {
  templates: PromptTemplate[]
  selectedId: string | null
  searchQuery: string
  onSelect: (id: string) => void
}

function formatDate(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function truncateName(name: string, maxLen = 35): string {
  let width = 0
  for (let i = 0; i < name.length; i++) {
    // CJK characters count as ~2, ASCII as 1
    width += name.charCodeAt(i) > 0x7f ? 2 : 1
    if (width > maxLen) return `${name.slice(0, i)}...`
  }
  return name
}

export function TemplateList({ templates, selectedId, searchQuery, onSelect }: TemplateListProps) {
  const filtered = useMemo(() => {
    if (!searchQuery.trim()) return templates
    const q = searchQuery.toLowerCase()
    return templates.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q) ||
        t.category.toLowerCase().includes(q) ||
        t.body.toLowerCase().includes(q),
    )
  }, [templates, searchQuery])

  if (templates.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted-foreground">
        No templates yet.
        <br />
        Click + New to create one.
      </div>
    )
  }

  if (filtered.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted-foreground">
        No matching templates.
      </div>
    )
  }

  return (
    <ScrollArea className="h-full">
      <div className="p-1">
        {filtered.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => onSelect(t.id)}
            className={cn(
              'flex w-full flex-col items-start gap-0.5 rounded-md px-3 py-2 text-left text-sm transition-colors',
              'hover:bg-accent',
              selectedId === t.id && 'bg-accent',
            )}>
            <span className="w-full font-medium leading-tight" title={t.name}>
              {truncateName(t.name)}
            </span>
            <div className="flex w-full items-center gap-1.5">
              {t.category && (
                <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {t.category}
                </span>
              )}
              <span className="text-[10px] text-muted-foreground/60">{formatDate(t.updated_at)}</span>
            </div>
          </button>
        ))}
      </div>
    </ScrollArea>
  )
}
