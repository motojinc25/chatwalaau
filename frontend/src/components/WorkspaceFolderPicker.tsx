// A one-level-at-a-time picker over the folders of the coding workspace (PRP-0244). Shared by the
// AI discussion's code folder (PRP-0236, UDR-0219 D1) and the harness project folder (CTR-0197 v3,
// UDR-0225 D1): folders only, never files; the caller supplies the listing call and, optionally,
// a badge per folder and a "New folder" action.
import { ChevronUp, Folder, FolderPlus } from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'

export interface WorkspaceFolderListing {
  dir: string
  parent: string | null
  dirs: string[]
  truncated: boolean
}

interface WorkspaceFolderPickerProps<L extends WorkspaceFolderListing> {
  /** The folder shown first ("" is the workspace root). */
  initialDir: string
  /** Lists one level of `dir`. */
  load: (dir: string) => Promise<L>
  /** "Use this folder". */
  onUse: (dir: string) => void
  onCancel: () => void
  /** A small marker after a folder name (for example its rules files). */
  badge?: (listing: L, name: string) => ReactNode
  /** Creates folder `name` in `dir` and resolves to its path; enables "New folder". */
  onCreate?: (dir: string, name: string) => Promise<string>
  /** Disables every action (for example while a turn runs). */
  disabled?: boolean
  testId?: string
}

export function WorkspaceFolderPicker<L extends WorkspaceFolderListing>({
  initialDir,
  load,
  onUse,
  onCancel,
  badge,
  onCreate,
  disabled,
  testId,
}: WorkspaceFolderPickerProps<L>) {
  const [listing, setListing] = useState<L | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newName, setNewName] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  const open = useCallback(
    (dir: string) => {
      setError(null)
      load(dir)
        .then(setListing)
        .catch((err) => setError(err instanceof Error ? err.message : 'Could not list the folders'))
    },
    [load],
  )

  useEffect(() => {
    open(initialDir)
  }, [open, initialDir])

  const create = () => {
    if (!onCreate || !listing || !newName?.trim()) return
    setCreating(true)
    setError(null)
    onCreate(listing.dir, newName.trim())
      .then((path) => {
        setNewName(null)
        open(path)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Could not create the folder'))
      .finally(() => setCreating(false))
  }

  return (
    <div className="space-y-1 rounded-md border p-2" data-testid={testId}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="min-w-0 flex-1 truncate font-mono" data-testid={testId ? `${testId}-dir` : undefined}>
          {listing ? listing.dir || '(workspace)' : '...'}
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2"
          disabled={disabled || !listing || listing.parent === null}
          onClick={() => listing && listing.parent !== null && open(listing.parent)}>
          <ChevronUp className="mr-1 h-3 w-3" />
          Up
        </Button>
        {onCreate && (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2"
            disabled={disabled || !listing}
            onClick={() => setNewName((v) => (v === null ? '' : null))}>
            <FolderPlus className="mr-1 h-3 w-3" />
            New folder
          </Button>
        )}
        <Button
          size="sm"
          className="h-6 px-2"
          disabled={disabled || !listing}
          onClick={() => listing && onUse(listing.dir)}>
          Use this folder
        </Button>
        <Button variant="ghost" size="sm" className="h-6 px-2" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {newName !== null && (
        <div className="flex items-center gap-2 text-xs">
          <input
            // biome-ignore lint/a11y/noAutofocus: the field appears on an explicit "New folder" click
            autoFocus
            aria-label="New folder name"
            className="h-7 min-w-0 flex-1 rounded-md border bg-background px-2"
            value={newName}
            maxLength={255}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') create()
              if (e.key === 'Escape') setNewName(null)
            }}
          />
          <Button size="sm" className="h-7 px-2" disabled={creating || !newName.trim()} onClick={create}>
            Create
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs text-red-600">
          {error}
        </p>
      )}
      <ul className="max-h-48 overflow-y-auto text-xs">
        {listing?.dirs.map((name) => (
          <li key={name}>
            <button
              type="button"
              disabled={disabled}
              className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left hover:bg-muted"
              onClick={() => open(listing.dir ? `${listing.dir}/${name}` : name)}>
              <Folder className="h-3 w-3 shrink-0" aria-hidden="true" />
              <span className="min-w-0 truncate">{name}</span>
              {badge?.(listing, name)}
            </button>
          </li>
        ))}
        {listing && listing.dirs.length === 0 && <li className="px-1 text-muted-foreground">No sub-folders</li>}
        {listing?.truncated && <li className="px-1 text-muted-foreground">More folders are not shown.</li>}
      </ul>
    </div>
  )
}
