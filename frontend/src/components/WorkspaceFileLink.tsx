import { Eye, FileDown, Loader2 } from 'lucide-react'
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from 'react'
import { downloadUrl, workspaceRawUrl } from '@/lib/workspace-download'
import { isPreviewableRef, workspaceRefName } from '@/lib/workspace-ref'

/**
 * Workspace file references in assistant messages (CTR-0012 v1.10, CTR-0207,
 * PRP-0166, UDR-0150 D3/D4/D8).
 *
 * A `workspace:` / `sandbox:` link or image in rendered Markdown is dispatched here
 * instead of becoming an anchor. The reference is NEVER written into an href or src
 * (D4): Download fetches CTR-0136 `/raw` to a blob, an image is shown through an
 * object URL, and Open hands the path to the File Explorer's existing viewer
 * routing. Delivery reuses `/raw` and inherits its double gate (D3): when the File
 * Explorer surface is inert the reference renders as an explanatory chip.
 */

export interface WorkspaceLinkContextValue {
  /**
   * File Explorer availability (useFileExplorerAvailable). `undefined` where no
   * probe is mounted (/popup, /sidebar): the control stays interactive and a
   * failure shows the server's reason on click.
   */
  available?: boolean
  /** Open a path in the File Explorer viewer. Absent where no explorer is mounted. */
  onOpen?: (path: string) => void
}

const WorkspaceLinkContext = createContext<WorkspaceLinkContextValue>({})

export function WorkspaceLinkProvider({ value, children }: { value: WorkspaceLinkContextValue; children: ReactNode }) {
  return <WorkspaceLinkContext.Provider value={value}>{children}</WorkspaceLinkContext.Provider>
}

const DISABLED_REASON = 'Download unavailable: File Explorer is disabled'

/** Map a CTR-0136 failure detail to the chip text of PRP-0166 section 3.3. */
function reasonFor(detail: string): string {
  if (detail === 'File Explorer is disabled') return DISABLED_REASON
  if (detail === 'File not found') return 'File not found in the workspace'
  return detail
}

function Chip({ path, children, title }: { path: string; children: ReactNode; title?: string }) {
  return (
    <span
      className="inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-muted/40 px-1.5 py-0.5 align-baseline text-[0.8125rem]"
      title={title ?? path}
      data-workspace-path={path}>
      {children}
    </span>
  )
}

export function WorkspaceFileLink({ path, children }: { path: string; children?: ReactNode }) {
  const { available, onOpen } = useContext(WorkspaceLinkContext)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const name = workspaceRefName(path)
  const label = children ?? name

  const handleDownload = useCallback(async () => {
    setBusy(true)
    setError(null)
    const detail = await downloadUrl(workspaceRawUrl(path), name)
    setBusy(false)
    if (detail) setError(reasonFor(detail))
  }, [path, name])

  if (available === false) {
    return (
      <Chip path={path} title={`${path} -- ${DISABLED_REASON}`}>
        <FileDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="text-muted-foreground">{label}</span>
        <span className="text-xs text-muted-foreground">({DISABLED_REASON})</span>
      </Chip>
    )
  }

  return (
    <Chip path={path}>
      <FileDown className="h-3.5 w-3.5 shrink-0 text-blue-600" />
      <button
        type="button"
        className="truncate text-blue-600 hover:underline disabled:opacity-60"
        onClick={() => void handleDownload()}
        disabled={busy}
        aria-label={`Download ${name}`}
        title={`Download ${path}`}>
        {label}
      </button>
      {onOpen && isPreviewableRef(path) && (
        <button
          type="button"
          className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          onClick={() => onOpen(path)}
          aria-label={`Open ${name} in File Explorer`}
          title="Open in File Explorer">
          <Eye className="h-3.5 w-3.5" />
        </button>
      )}
      {busy && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
      {error && <span className="text-xs text-destructive">{error}</span>}
    </Chip>
  )
}

export function WorkspaceImage({ path, alt }: { path: string; alt?: string }) {
  const { available } = useContext(WorkspaceLinkContext)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (available === false) return
    let cancelled = false
    let created: string | null = null
    ;(async () => {
      try {
        const res = await fetch(workspaceRawUrl(path))
        if (!res.ok) {
          const body = await res.json().catch(() => ({}))
          if (!cancelled) setError(reasonFor(typeof body?.detail === 'string' ? body.detail : 'Failed to load image'))
          return
        }
        const blob = await res.blob()
        if (cancelled) return
        created = URL.createObjectURL(blob)
        setObjectUrl(created)
      } catch {
        if (!cancelled) setError('Failed to load image')
      }
    })()
    return () => {
      cancelled = true
      if (created) URL.revokeObjectURL(created)
    }
  }, [path, available])

  if (objectUrl) {
    return <img src={objectUrl} alt={alt ?? workspaceRefName(path)} className="my-2 max-w-full rounded" />
  }
  return <WorkspaceImageFallback path={path} alt={alt} reason={available === false ? DISABLED_REASON : error} />
}

function WorkspaceImageFallback({ path, alt, reason }: { path: string; alt?: string; reason: string | null }) {
  return (
    <Chip path={path}>
      {reason ? null : <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
      <span className="text-muted-foreground">{alt || workspaceRefName(path)}</span>
      {reason && <span className="text-xs text-muted-foreground">({reason})</span>}
    </Chip>
  )
}
