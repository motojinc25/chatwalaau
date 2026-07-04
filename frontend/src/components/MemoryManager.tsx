import Editor, { type OnMount } from '@monaco-editor/react'
import { Brain, Info, Loader2, Save } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import '@/lib/monaco-setup'

/**
 * Memory Management portal (CTR-0167, FEAT-0057, PRP-0101, UDR-0080).
 *
 * Opened from a launcher icon in the SessionSidebar footer. A ~90% modal styled
 * like the Cron / File Explorer portals: left = the three built-in file memories
 * (IDENTITY / USER / MEMORY) each with a role summary and an enable badge; right =
 * a Markdown editor (monaco, reused from the File Explorer -- no new dependency)
 * with a char-count / cap indicator, Save, an unsaved-close confirmation, and a
 * per-file "applies from a new chat / on restart" notice.
 *
 * Data comes from GET /api/memory/files and saves via PUT /api/memory/files/{key}
 * (CTR-0166). The trusted operator write is backup + char-limit only (UDR-0080 D2);
 * a save changes the LIVE file, not the running session's frozen snapshot, so it
 * takes effect from the next chat (USER / MEMORY) or the next restart (IDENTITY).
 *
 * Controlled component: the parent (ChatPage) owns open state. No client-side
 * persistence (the files are the SSOT).
 */

interface MemoryFile {
  key: string
  title: string
  role: string
  enabled: boolean
  content: string
  char_count: number
  char_limit: number
  applies_when: string
}

interface MemoryManagerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

type PendingConfirm = { type: 'close' } | { type: 'switch'; nextKey: string }

export function MemoryManager({ open, onOpenChange }: MemoryManagerProps) {
  const [files, setFiles] = useState<MemoryFile[]>([])
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [saved, setSaved] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [justSaved, setJustSaved] = useState(false)
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null)
  const [isDark, setIsDark] = useState(false)

  const selected = files.find((f) => f.key === selectedKey) ?? null
  const dirty = selected != null && draft !== saved
  const overCap = selected != null && draft.length > selected.char_limit

  const applySelect = useCallback((next: MemoryFile) => {
    setSelectedKey(next.key)
    setDraft(next.content)
    setSaved(next.content)
    setJustSaved(false)
    setError(null)
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/memory/files')
      if (!res.ok) throw new Error('Could not load memory files.')
      const json = (await res.json()) as { files: MemoryFile[] }
      setFiles(json.files)
      const first = json.files[0]
      if (first) {
        setSelectedKey(first.key)
        setDraft(first.content)
        setSaved(first.content)
      }
      setJustSaved(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load memory files.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setIsDark(document.documentElement.classList.contains('dark'))
    void load()
  }, [open, load])

  const save = useCallback(async () => {
    if (!selected || saving || overCap || !dirty) return
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`/api/memory/files/${selected.key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: draft }),
      })
      if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(detail.detail || 'Save failed.')
      }
      const json = (await res.json()) as { char_count: number }
      setSaved(draft)
      setFiles((prev) =>
        prev.map((f) => (f.key === selected.key ? { ...f, content: draft, char_count: json.char_count } : f)),
      )
      setJustSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed.')
    } finally {
      setSaving(false)
    }
  }, [selected, saving, overCap, dirty, draft])

  const saveRef = useRef(save)
  saveRef.current = save
  const handleMount: OnMount = useCallback((editor, monaco) => {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      void saveRef.current()
    })
  }, [])

  // Route every close attempt (X / Esc / Close button) through the dirty guard.
  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next && dirty) {
        setConfirm({ type: 'close' })
        return
      }
      onOpenChange(next)
    },
    [dirty, onOpenChange],
  )

  const requestSelect = useCallback(
    (file: MemoryFile) => {
      if (file.key === selectedKey) return
      if (dirty) {
        setConfirm({ type: 'switch', nextKey: file.key })
        return
      }
      applySelect(file)
    },
    [selectedKey, dirty, applySelect],
  )

  const resolveConfirm = useCallback(() => {
    const pending = confirm
    setConfirm(null)
    if (!pending) return
    if (pending.type === 'close') {
      onOpenChange(false)
      return
    }
    const next = files.find((f) => f.key === pending.nextKey)
    if (next) applySelect(next)
  }, [confirm, files, onOpenChange, applySelect])

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-[90vh] w-[90vw] max-w-[90vw] flex-col gap-0 p-0">
          <DialogHeader className="border-b px-6 py-4">
            <DialogTitle className="flex items-center gap-2">
              <Brain className="h-4 w-4" /> Agent Memory
            </DialogTitle>
            <DialogDescription>
              View and edit the assistant's built-in memory files. Saving creates a timestamped backup first. Changes
              apply from a new chat (or on restart for the identity).
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1">
            {/* Left: the three fixed memory files */}
            <div className="flex w-72 shrink-0 flex-col border-r">
              <div className="min-h-0 flex-1 overflow-y-auto p-2">
                {files.map((file) => (
                  <button
                    key={file.key}
                    type="button"
                    onClick={() => requestSelect(file)}
                    className={cn(
                      'mb-1 w-full rounded-md px-3 py-2 text-left transition-colors hover:bg-accent',
                      file.key === selectedKey && 'bg-accent',
                    )}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{file.title}</span>
                      {!file.enabled && (
                        <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          not injected
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{file.role}</p>
                  </button>
                ))}
                {loading && files.length === 0 && (
                  <div className="flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" /> Loading...
                  </div>
                )}
              </div>
            </div>

            {/* Right: the editor for the selected file */}
            <div className="flex min-w-0 flex-1 flex-col">
              {selected ? (
                <>
                  <div className="flex items-start justify-between gap-4 border-b px-4 py-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium">{selected.title}</p>
                      <p className="mt-0.5 flex items-center gap-1 text-xs text-muted-foreground">
                        <Info className="h-3 w-3 shrink-0" />
                        {selected.applies_when}
                      </p>
                    </div>
                    <span
                      className={cn(
                        'shrink-0 whitespace-nowrap text-xs tabular-nums',
                        overCap ? 'font-medium text-destructive' : 'text-muted-foreground',
                      )}>
                      {draft.length} / {selected.char_limit}
                    </span>
                  </div>

                  <div className="min-h-0 flex-1">
                    <Editor
                      path={`${selected.key}.md`}
                      language="markdown"
                      value={draft}
                      theme={isDark ? 'vs-dark' : 'vs'}
                      onChange={(value) => setDraft(value ?? '')}
                      onMount={handleMount}
                      options={{
                        minimap: { enabled: false },
                        fontSize: 13,
                        automaticLayout: true,
                        scrollBeyondLastLine: false,
                        wordWrap: 'on',
                        tabSize: 2,
                        lineNumbers: 'off',
                      }}
                    />
                  </div>
                </>
              ) : (
                <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                  {loading ? 'Loading...' : 'Select a memory file to edit.'}
                </div>
              )}
            </div>
          </div>

          {/* Footer: status + actions */}
          <div className="flex items-center justify-between gap-3 border-t px-6 py-3">
            <div className="min-w-0 text-xs">
              {error ? (
                <span className="text-destructive">{error}</span>
              ) : overCap ? (
                <span className="text-destructive">Over the character limit -- shorten before saving.</span>
              ) : justSaved && !dirty ? (
                <span className="text-muted-foreground">Saved. A backup was created.</span>
              ) : dirty ? (
                <span className="text-muted-foreground">Unsaved changes.</span>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button size="sm" onClick={() => void save()} disabled={!dirty || overCap || saving}>
                {saving ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Save className="mr-1 h-3 w-3" />}
                Save
              </Button>
              <Button size="sm" variant="outline" onClick={() => handleOpenChange(false)}>
                Close
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              You have unsaved edits to {selected?.title ?? 'this file'}. If you continue, your changes will be lost.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction onClick={resolveConfirm}>Discard changes</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
