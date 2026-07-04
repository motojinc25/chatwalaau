import { FileImage, Folder, FolderUp, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'

/**
 * Workspace image picker for the Paint editor (CTR-0160 v2, PRP-0102 / UDR-0078 D10).
 *
 * Lets the user pick an image FROM THE CODING WORKSPACE (instead of a local upload)
 * for environments where local file uploads are restricted. Browses the workspace
 * via CTR-0136 `GET /api/workspace/tree` (folders + image files only) and fetches the
 * chosen file's bytes via `GET /api/workspace/raw`, handing a typed `File` back to the
 * Paint editor's existing image loader. Shown only when the File Explorer / coding
 * workspace is available (gated by the caller); a spinner covers the list while a
 * folder or the chosen image is loading.
 */

// Image extensions offered from the workspace. `svg` is loaded as an editable
// vector by the Paint editor; the rest as raster image objects.
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'])
const EXT_MIME: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  bmp: 'image/bmp',
  svg: 'image/svg+xml',
}

interface TreeEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
}

interface WorkspaceImagePickerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Called with a typed image File once the user picks one from the workspace. */
  onPick: (file: File) => void
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}

export function WorkspaceImagePicker({ open, onOpenChange, onPick }: WorkspaceImagePickerProps) {
  const [dir, setDir] = useState('')
  const [entries, setEntries] = useState<TreeEntry[]>([])
  const [loadingDir, setLoadingDir] = useState(false)
  const [loadingFile, setLoadingFile] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadDir = useCallback(async (target: string) => {
    setLoadingDir(true)
    setError(null)
    try {
      const res = await fetch(`/api/workspace/tree?dir=${encodeURIComponent(target)}`)
      if (!res.ok) throw new Error('Could not read the workspace folder.')
      const json = (await res.json()) as { entries?: TreeEntry[] }
      const all = json.entries ?? []
      const dirs = all.filter((e) => e.is_dir)
      const images = all.filter((e) => !e.is_dir && IMAGE_EXTS.has(extOf(e.name)))
      setEntries([...dirs, ...images])
      setDir(target)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read the workspace folder.')
      setEntries([])
    } finally {
      setLoadingDir(false)
    }
  }, [])

  useEffect(() => {
    if (open) void loadDir('')
  }, [open, loadDir])

  const pick = useCallback(
    async (entry: TreeEntry) => {
      setLoadingFile(true)
      setError(null)
      try {
        const res = await fetch(`/api/workspace/raw?path=${encodeURIComponent(entry.path)}`)
        if (!res.ok) throw new Error('Could not load the image.')
        const blob = await res.blob()
        const type = blob.type.startsWith('image/')
          ? blob.type
          : (EXT_MIME[extOf(entry.name)] ?? 'application/octet-stream')
        onPick(new File([blob], entry.name, { type }))
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not load the image.')
      } finally {
        setLoadingFile(false)
      }
    },
    [onPick],
  )

  const parent = dir === '' ? null : dir.split('/').slice(0, -1).join('/')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[70vh] w-[560px] max-w-[92vw] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-5 py-3">
          <DialogTitle className="text-base">Open image from workspace</DialogTitle>
          <DialogDescription className="truncate text-xs">/{dir}</DialogDescription>
        </DialogHeader>
        <div className="relative min-h-0 flex-1 overflow-y-auto p-2">
          {dir !== '' && (
            <button
              type="button"
              onClick={() => void loadDir(parent ?? '')}
              className="mb-1 flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-accent">
              <FolderUp className="h-4 w-4 text-muted-foreground" /> ..
            </button>
          )}
          {entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              disabled={loadingFile}
              onClick={() => (entry.is_dir ? void loadDir(entry.path) : void pick(entry))}
              className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-accent disabled:opacity-50">
              {entry.is_dir ? (
                <Folder className="h-4 w-4 shrink-0 text-muted-foreground" />
              ) : (
                <FileImage className="h-4 w-4 shrink-0 text-muted-foreground" />
              )}
              <span className="truncate">{entry.name}</span>
            </button>
          ))}
          {!loadingDir && entries.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">No images in this folder.</div>
          )}
          {(loadingDir || loadingFile) && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/70">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          )}
        </div>
        {error && <div className="border-t px-5 py-2 text-xs text-destructive">{error}</div>}
      </DialogContent>
    </Dialog>
  )
}
