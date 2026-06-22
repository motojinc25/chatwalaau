import { Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import type { CommandEntry, CommandsInventory } from '@/lib/slashCommands'

interface HelpPortalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Slash command Help Portal (CTR-0128, PRP-0088, UDR-0066). A searchable modal
 * listing every effective command grouped by category, with collision warnings.
 * Opened by the `/help` command.
 */
export function HelpPortal({ open, onOpenChange }: HelpPortalProps) {
  const [inv, setInv] = useState<CommandsInventory | null>(null)
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!open) return
    setQuery('')
    fetch('/api/commands')
      .then((res) => (res.ok ? res.json() : null))
      .then((data: CommandsInventory | null) => setInv(data))
      .catch(() => {})
  }, [open])

  const grouped = useMemo(() => {
    const commands = inv?.commands ?? []
    const q = query.trim().toLowerCase()
    const filtered = q
      ? commands.filter(
          (c) =>
            c.token.toLowerCase().includes(q) ||
            c.description.toLowerCase().includes(q) ||
            c.aliases.some((a) => a.toLowerCase().includes(q)),
        )
      : commands
    const byCategory = new Map<string, CommandEntry[]>()
    for (const c of filtered) {
      const cat = c.category || 'Other'
      const arr = byCategory.get(cat) ?? []
      arr.push(c)
      byCategory.set(cat, arr)
    }
    return [...byCategory.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [inv, query])

  const collisions = inv?.collisions ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[80vh] max-w-2xl flex-col gap-0 p-0">
        <DialogHeader className="shrink-0 border-b px-6 py-4">
          <DialogTitle>Slash commands</DialogTitle>
          <div className="relative mt-2">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search commands..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="pl-9"
            />
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {collisions.length > 0 && (
            <div className="mb-4 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-600">
              Some command names collide across sources (first one wins):{' '}
              {collisions.map((c) => `/${c.token}`).join(', ')}
            </div>
          )}
          {grouped.length === 0 && <p className="text-sm text-muted-foreground">No commands found.</p>}
          {grouped.map(([category, commands]) => (
            <div key={category} className="mb-5">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{category}</h3>
              <div className="flex flex-col gap-2">
                {commands.map((c) => (
                  <div key={`${c.source}:${c.token}`} className="rounded-md border px-3 py-2">
                    <div className="flex items-baseline gap-2">
                      <code className="text-sm font-medium">/{c.token}</code>
                      {c.args_hint && <span className="text-xs text-muted-foreground">{c.args_hint}</span>}
                      {c.aliases.length > 0 && (
                        <span className="text-xs text-muted-foreground">
                          (aliases: {c.aliases.map((a) => `/${a}`).join(', ')})
                        </span>
                      )}
                    </div>
                    {c.description && <p className="mt-0.5 text-xs text-muted-foreground">{c.description}</p>}
                  </div>
                ))}
              </div>
            </div>
          ))}
          <p className="mt-2 text-xs text-muted-foreground">
            Tip: type <code>/</code> in the chat box for inline suggestions, <code>@</code> to reference a workspace
            file, and use <code>$1</code> / <code>$ARGUMENTS</code> placeholders in templates and skills.
          </p>
        </div>
      </DialogContent>
    </Dialog>
  )
}
