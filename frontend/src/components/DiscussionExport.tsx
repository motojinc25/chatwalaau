// Export of an AI discussion run (CTR-0276, PRP-0235, UDR-0218 D1 / D2): Download always, and
// Save to workspace when the File Explorer is on -- through its own write endpoint (CTR-0136), so
// no discussion-specific server write path exists. A file already there is replaced only after
// the person confirms.
import { Download, FolderDown } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { useFileExplorerAvailable } from '@/hooks/useFileExplorerAvailable'
import { discussionApi, downloadMarkdown, EXPORT_FOLDER } from '@/lib/discussionApi'

async function workspaceHas(path: string): Promise<boolean> {
  const res = await fetch(`/api/workspace/tree?dir=${encodeURIComponent(EXPORT_FOLDER)}`)
  if (!res.ok) return false // the folder does not exist yet
  const body = (await res.json()) as { entries?: { path: string }[] }
  return (body.entries ?? []).some((e) => e.path.replaceAll('\\', '/') === path)
}

async function saveToWorkspace(path: string, content: string): Promise<void> {
  const res = await fetch('/api/workspace/file', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const body = await res.json()
      if (typeof body?.detail === 'string') message = body.detail
    } catch {
      // keep the status text
    }
    throw new Error(message)
  }
}

export function DiscussionExportActions({
  threadId,
  runId,
  compact,
}: {
  threadId: string
  runId: string
  compact?: boolean
}) {
  const workspace = useFileExplorerAvailable()
  const [includeLog, setIncludeLog] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [replace, setReplace] = useState<{ path: string; text: string } | null>(null)

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setNotice(null)
    try {
      await fn()
    } catch (err) {
      setNotice(err instanceof Error ? `Export failed: ${err.message}` : 'Export failed.')
    } finally {
      setBusy(false)
    }
  }

  const download = () =>
    run(async () => {
      const { name, text } = await discussionApi.exportRun(threadId, runId, includeLog)
      downloadMarkdown(name, text)
    })

  const save = () =>
    run(async () => {
      const { name, text } = await discussionApi.exportRun(threadId, runId, includeLog)
      const path = `${EXPORT_FOLDER}/${name}`
      if (await workspaceHas(path)) {
        setReplace({ path, text })
        return
      }
      await saveToWorkspace(path, text)
      setNotice(`Saved to ${path}`)
    })

  const confirmReplace = () =>
    run(async () => {
      if (!replace) return
      await saveToWorkspace(replace.path, replace.text)
      setNotice(`Saved to ${replace.path}`)
      setReplace(null)
    })

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="discussion-export">
      <Button size="sm" variant="outline" className="h-7" onClick={() => void download()} disabled={busy}>
        <Download className="mr-1 h-3 w-3" />
        Export
      </Button>
      {workspace && (
        <Button size="sm" variant="outline" className="h-7" onClick={() => void save()} disabled={busy}>
          <FolderDown className="mr-1 h-3 w-3" />
          Save to workspace
        </Button>
      )}
      {!compact && (
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          <input type="checkbox" checked={includeLog} onChange={(e) => setIncludeLog(e.target.checked)} />
          Include the full discussion
        </label>
      )}
      {replace && (
        <span className="flex basis-full items-center gap-2 text-xs">
          {replace.path} exists.
          <Button size="sm" variant="outline" className="h-6 px-2 text-xs" onClick={() => void confirmReplace()}>
            Replace it
          </Button>
          <Button size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={() => setReplace(null)}>
            Keep it
          </Button>
        </span>
      )}
      {notice && (
        <span className="basis-full text-xs text-muted-foreground" role="status" data-testid="discussion-export-notice">
          {notice}
        </span>
      )}
    </div>
  )
}
