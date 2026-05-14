import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import type { SessionSummary } from '@/types/chat'

interface SearchResult extends SessionSummary {
  snippet?: string
}

interface SessionSearchDialogProps {
  sessions: SessionSummary[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelect: (threadId: string) => void
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  if (!query.trim()) return <>{text}</>

  const lowerText = text.toLowerCase()
  const lowerQuery = query.toLowerCase()
  const idx = lowerText.indexOf(lowerQuery)

  if (idx === -1) return <>{text}</>

  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-yellow-200 dark:bg-yellow-800">{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
    </>
  )
}

function formatDateTime(iso: string): string {
  if (!iso) return ''
  const date = new Date(iso)
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function SessionSearchDialog({ sessions, open, onOpenChange, onSelect }: SessionSearchDialogProps) {
  const [query, setQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null)
  const [isSearching, setIsSearching] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (open) {
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])

  // Debounced full-text search via API
  const performSearch = useCallback((q: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current)

    if (!q.trim()) {
      setSearchResults(null)
      setIsSearching(false)
      return
    }

    setIsSearching(true)
    debounceRef.current = setTimeout(async () => {
      try {
        const res = await fetch(`/api/sessions/search?q=${encodeURIComponent(q.trim())}`)
        if (res.ok) {
          setSearchResults(await res.json())
        }
      } catch {
        // fallback: no results
      } finally {
        setIsSearching(false)
      }
    }, 300)
  }, [])

  const handleQueryChange = useCallback(
    (value: string) => {
      setQuery(value)
      performSearch(value)
    },
    [performSearch],
  )

  // When query is empty, show all sessions
  const displayed: SearchResult[] = useMemo(() => {
    if (searchResults !== null) return searchResults
    return sessions
  }, [searchResults, sessions])

  const handleSelect = (threadId: string) => {
    onSelect(threadId)
    onOpenChange(false)
    setQuery('')
    setSearchResults(null)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && displayed.length > 0) {
      handleSelect(displayed[0].thread_id)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v)
        if (!v) {
          setQuery('')
          setSearchResults(null)
        }
      }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Search sessions</DialogTitle>
        </DialogHeader>
        <Input
          ref={inputRef}
          placeholder="Search by title or content..."
          value={query}
          onChange={(e) => handleQueryChange(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <div className="h-[60vh] overflow-y-auto">
          {isSearching && <p className="py-4 text-center text-sm text-muted-foreground">Searching...</p>}
          {!isSearching && displayed.length === 0 && (
            <p className="py-4 text-center text-sm text-muted-foreground">No sessions found</p>
          )}
          {!isSearching &&
            displayed.map((session) => (
              <button
                type="button"
                key={session.thread_id}
                className="flex w-full flex-col rounded-md px-3 py-2 text-left text-sm transition-colors hover:bg-muted"
                onClick={() => handleSelect(session.thread_id)}>
                <div className="flex w-full items-center justify-between">
                  <span className="min-w-0 flex-1 truncate">
                    <HighlightedText text={session.title || 'New session'} query={query} />
                  </span>
                  <span className="ml-2 flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
                    {formatDateTime(session.updated_at)}
                    {session.message_count > 0 && ` · ${session.message_count} msgs`}
                    {session.image_count > 0 && ` · ${session.image_count} imgs`}
                    {session.source === 'openai-api' && (
                      <span className="inline-block shrink-0 rounded px-1 py-0.5 text-[10px] font-medium leading-none bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">
                        API
                      </span>
                    )}
                  </span>
                </div>
                {session.snippet && (
                  <p className="mt-0.5 truncate text-xs text-muted-foreground">
                    <HighlightedText text={session.snippet} query={query} />
                  </p>
                )}
              </button>
            ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
