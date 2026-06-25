import Editor, { type OnMount } from '@monaco-editor/react'
import {
  ChevronRight,
  File as FileIcon,
  FilePlus,
  Folder as FolderIcon,
  FolderOpen,
  FolderPlus,
  Loader2,
  RefreshCw,
  Save,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { type NodeRendererProps, Tree } from 'react-arborist'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
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
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import '@/lib/monaco-setup'

/**
 * File Explorer overlay (CTR-0137, FEAT-0049, PRP-0091, UDR-0069).
 *
 * A full-screen, VSCode-style overlay rooted at the coding workspace: a left
 * react-arborist file tree, a react-resizable-panels splitter, and a right tabbed
 * monaco editor (light "vs" theme). All file IO goes through the jailed CTR-0136 API;
 * the editor never resolves paths itself (UDR-0069 D2). In the tree, a single click
 * selects (a folder also toggles) and a double click opens a file. Open/save/delete show
 * blocking indicators; delete requires a final confirmation (UDR-0069 D8). Any close that
 * would drop unsaved work -- a single dirty tab, a bulk Close All / Close Others including
 * unsaved tabs, or closing the whole overlay -- prompts a discard confirmation first; for
 * the overlay, confirming restores each tab to its opened content (so re-opening shows the
 * clean state) and closes.
 *
 * Controlled component: the parent (ChatPage) owns open state so both the sidebar
 * footer icon and the /files slash command drive one instance.
 */

interface FileExplorerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

interface FileNode {
  id: string // workspace-relative path (unique)
  name: string
  isDir: boolean
  children?: FileNode[]
}

interface TreeEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
  mtime?: number
}

interface Tab {
  path: string
  name: string
  loading: boolean
  saving: boolean
  error: string | null
  content: string
  original: string
  mtime: number
  isBinary: boolean
}

const isDirty = (t: Tab): boolean => !t.isBinary && !t.loading && t.content !== t.original

function parentOf(path: string): string {
  const i = path.lastIndexOf('/')
  return i < 0 ? '' : path.slice(0, i)
}
function baseName(path: string): string {
  const i = path.lastIndexOf('/')
  return i < 0 ? path : path.slice(i + 1)
}
function joinPath(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name
}

function toNode(e: TreeEntry): FileNode {
  return e.is_dir ? { id: e.path, name: e.name, isDir: true, children: [] } : { id: e.path, name: e.name, isDir: false }
}

/** Immutably replace a directory node's children, keyed by id. */
function setChildrenById(nodes: FileNode[], dirId: string, children: FileNode[]): FileNode[] {
  return nodes.map((n) => {
    if (n.id === dirId) return { ...n, children }
    if (n.isDir && n.children && n.children.length > 0) {
      return { ...n, children: setChildrenById(n.children, dirId, children) }
    }
    return n
  })
}

function useElementSize() {
  const [size, setSize] = useState({ width: 0, height: 0 })
  // Callback ref (not useRef): re-runs whenever the node attaches/detaches. The
  // FileExplorer stays mounted while the dialog is closed, so the measured div does
  // not exist until the dialog opens; a one-shot mount effect would never observe it
  // and react-arborist would render at 0x0 (an empty tree). The callback ref attaches
  // the ResizeObserver the moment the div mounts on open.
  const [el, setEl] = useState<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!el) return
    const measure = () => setSize({ width: el.clientWidth, height: el.clientHeight })
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    measure()
    return () => ro.disconnect()
  }, [el])
  return [setEl, size] as const
}

export function FileExplorer({ open, onOpenChange }: FileExplorerProps) {
  const [data, setData] = useState<FileNode[]>([])
  const [rootLoaded, setRootLoaded] = useState(false)
  const loadedRef = useRef<Set<string>>(new Set())

  const [tabs, setTabs] = useState<Tab[]>([])
  const [activePath, setActivePath] = useState<string | null>(null)

  const [createTarget, setCreateTarget] = useState<{ dir: string; kind: 'file' | 'dir' } | null>(null)
  const [createName, setCreateName] = useState('')
  const [createBusy, setCreateBusy] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ path: string; isDir: boolean } | null>(null)
  const [renameName, setRenameName] = useState('')
  const [renameBusy, setRenameBusy] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<{ path: string; isDir: boolean } | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [pendingCloseTab, setPendingCloseTab] = useState<string | null>(null)
  // Bulk "Close all / Close others" when some target tabs are unsaved (issue 2).
  const [pendingBulkClose, setPendingBulkClose] = useState<string[] | null>(null)
  // Overlay-close warning when there are unsaved tabs (issue 1).
  const [leaveConfirm, setLeaveConfirm] = useState(false)

  const [treeRef, treeSize] = useElementSize()

  // ---- Tree data loading ----

  const reloadDir = useCallback(async (dirPath: string) => {
    try {
      const res = await fetch(`/api/workspace/tree?dir=${encodeURIComponent(dirPath)}`)
      if (!res.ok) return
      const json = await res.json()
      const children: FileNode[] = (json.entries ?? []).map(toNode)
      loadedRef.current.add(dirPath)
      if (dirPath === '') {
        setData(children)
        setRootLoaded(true)
      } else {
        setData((prev) => setChildrenById(prev, dirPath, children))
      }
    } catch {
      // silent
    }
  }, [])

  useEffect(() => {
    if (open && !rootLoaded) void reloadDir('')
  }, [open, rootLoaded, reloadDir])

  const onToggle = useCallback(
    (id: string) => {
      if (!loadedRef.current.has(id)) void reloadDir(id)
    },
    [reloadDir],
  )

  // ---- Editor tabs ----

  const openFile = useCallback(async (path: string) => {
    setActivePath(path)
    setTabs((prev) => {
      if (prev.some((t) => t.path === path)) return prev
      return [
        ...prev,
        {
          path,
          name: baseName(path),
          loading: true,
          saving: false,
          error: null,
          content: '',
          original: '',
          mtime: 0,
          isBinary: false,
        },
      ]
    })
    try {
      const res = await fetch(`/api/workspace/file?path=${encodeURIComponent(path)}`)
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        setTabs((prev) =>
          prev.map((t) => (t.path === path ? { ...t, loading: false, error: detail.detail || 'Failed to open' } : t)),
        )
        return
      }
      const json = await res.json()
      setTabs((prev) =>
        prev.map((t) =>
          t.path === path
            ? {
                ...t,
                loading: false,
                error: null,
                content: json.content ?? '',
                original: json.content ?? '',
                mtime: json.mtime ?? 0,
                isBinary: !!json.is_binary,
              }
            : t,
        ),
      )
    } catch {
      setTabs((prev) => prev.map((t) => (t.path === path ? { ...t, loading: false, error: 'Failed to open' } : t)))
    }
  }, [])

  const activeTab = tabs.find((t) => t.path === activePath) ?? null

  const onEditorChange = useCallback(
    (value: string | undefined) => {
      if (activePath == null) return
      setTabs((prev) => prev.map((t) => (t.path === activePath ? { ...t, content: value ?? '' } : t)))
    },
    [activePath],
  )

  const saveTab = useCallback(
    async (path: string) => {
      let target: Tab | undefined
      setTabs((prev) => {
        target = prev.find((t) => t.path === path)
        return target && isDirty(target)
          ? prev.map((t) => (t.path === path ? { ...t, saving: true, error: null } : t))
          : prev
      })
      if (!target || !isDirty(target)) return
      try {
        const res = await fetch('/api/workspace/file', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path, content: target.content, expected_mtime: target.mtime }),
        })
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}))
          const msg = res.status === 409 ? 'File changed on disk; reopen to merge.' : detail.detail || 'Save failed'
          setTabs((prev) => prev.map((t) => (t.path === path ? { ...t, saving: false, error: msg } : t)))
          return
        }
        const json = await res.json()
        setTabs((prev) =>
          prev.map((t) =>
            t.path === path
              ? { ...t, saving: false, error: null, original: t.content, mtime: json.mtime ?? t.mtime }
              : t,
          ),
        )
        void reloadDir(parentOf(path))
      } catch {
        setTabs((prev) => prev.map((t) => (t.path === path ? { ...t, saving: false, error: 'Save failed' } : t)))
      }
    },
    [reloadDir],
  )

  // Stable ref so the monaco Ctrl/Cmd+S command always saves the current tab.
  const saveActiveRef = useRef<() => void>(() => {})
  saveActiveRef.current = () => {
    if (activePath) void saveTab(activePath)
  }

  const handleMount: OnMount = useCallback((editor, monaco) => {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveActiveRef.current())
  }, [])

  const removeTab = useCallback((path: string) => {
    setTabs((prev) => {
      const next = prev.filter((t) => t.path !== path)
      setActivePath((cur) => (cur === path ? (next.length ? next[next.length - 1].path : null) : cur))
      return next
    })
  }, [])

  const requestCloseTab = useCallback(
    (path: string) => {
      const t = tabs.find((x) => x.path === path)
      if (t && isDirty(t)) setPendingCloseTab(path)
      else removeTab(path)
    },
    [tabs, removeTab],
  )

  // Remove a set of tabs at once, keeping the active tab valid.
  const doCloseTabs = useCallback((paths: string[]) => {
    const drop = new Set(paths)
    setTabs((prev) => {
      const next = prev.filter((t) => !drop.has(t.path))
      setActivePath((cur) => (cur && drop.has(cur) ? (next.length ? next[next.length - 1].path : null) : cur))
      return next
    })
  }, [])

  // Close many tabs (Close All / Close Others). If any target tab is unsaved, confirm
  // first so edits are not lost silently (issue 2).
  const requestCloseTabs = useCallback(
    (paths: string[]) => {
      if (!paths.length) return
      const drop = new Set(paths)
      const anyDirtyAmong = tabs.some((t) => drop.has(t.path) && isDirty(t))
      if (anyDirtyAmong) setPendingBulkClose(paths)
      else doCloseTabs(paths)
    },
    [tabs, doCloseTabs],
  )

  // ---- Mutations: create / rename-move / delete ----

  const submitCreate = useCallback(async () => {
    if (!createTarget) return
    const name = createName.trim()
    if (!name) return
    setCreateBusy(true)
    const path = joinPath(createTarget.dir, name)
    try {
      const res = await fetch('/api/workspace/entry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, kind: createTarget.kind }),
      })
      if (res.ok) {
        await reloadDir(createTarget.dir)
        if (createTarget.kind === 'file') void openFile(path)
        setCreateTarget(null)
        setCreateName('')
      }
    } finally {
      setCreateBusy(false)
    }
  }, [createTarget, createName, reloadDir, openFile])

  // Single rename primitive shared by the rename dialog and react-arborist's inline
  // edit. Returns true on success / no-op so the caller can clear its UI.
  const renameTo = useCallback(
    async (from: string, rawName: string): Promise<boolean> => {
      const name = rawName.trim()
      const next = joinPath(parentOf(from), name)
      if (!name || next === from) return true
      const res = await fetch('/api/workspace/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from, to: next }),
      })
      if (!res.ok) return false
      await reloadDir(parentOf(from))
      // Re-point an open tab whose path (or whose ancestor dir) was renamed.
      const prefix = `${from}/`
      setTabs((prev) =>
        prev.map((t) => {
          if (t.path === from) return { ...t, path: next, name: baseName(next) }
          if (t.path.startsWith(prefix)) {
            const moved = next + t.path.slice(from.length)
            return { ...t, path: moved }
          }
          return t
        }),
      )
      setActivePath((cur) => (cur === from ? next : cur?.startsWith(prefix) ? next + cur.slice(from.length) : cur))
      return true
    },
    [reloadDir],
  )

  // react-arborist inline-edit handler (F2 etc.). The context-menu Rename uses a
  // dialog instead, because Radix returns focus on menu close and instantly blurs the
  // inline input (cancelling the edit before the user can type).
  const handleRename = useCallback(
    ({ id, name }: { id: string; name: string }) => {
      void renameTo(id, name)
    },
    [renameTo],
  )

  const submitRename = useCallback(async () => {
    if (!renameTarget) return
    const name = renameName.trim()
    if (!name) return
    setRenameBusy(true)
    try {
      const ok = await renameTo(renameTarget.path, name)
      if (ok) {
        setRenameTarget(null)
        setRenameName('')
      }
    } finally {
      setRenameBusy(false)
    }
  }, [renameTarget, renameName, renameTo])

  const handleMove = useCallback(
    async ({ dragIds, parentId }: { dragIds: string[]; parentId: string | null }) => {
      const targetDir = parentId ?? ''
      for (const from of dragIds) {
        const to = joinPath(targetDir, baseName(from))
        if (to === from) continue
        await fetch('/api/workspace/move', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ from, to }),
        })
        await reloadDir(parentOf(from))
      }
      await reloadDir(targetDir)
    },
    [reloadDir],
  )

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      const res = await fetch(`/api/workspace/entry?path=${encodeURIComponent(deleteTarget.path)}`, {
        method: 'DELETE',
      })
      if (res.ok) {
        await reloadDir(parentOf(deleteTarget.path))
        // Drop any open tab under the deleted path (file or directory subtree).
        const prefix = `${deleteTarget.path}/`
        setTabs((prev) => prev.filter((t) => t.path !== deleteTarget.path && !t.path.startsWith(prefix)))
        setActivePath((cur) => (cur && (cur === deleteTarget.path || cur.startsWith(prefix)) ? null : cur))
        setDeleteTarget(null)
      }
    } finally {
      setDeleting(false)
    }
  }, [deleteTarget, reloadDir])

  // ---- Overlay close: warn if unsaved, then discard + reset on confirm (issue 1) ----

  // Reset every dirty tab back to its opened content (discard in-progress edits) and close.
  const discardAndClose = useCallback(() => {
    setTabs((prev) => prev.map((t) => (isDirty(t) ? { ...t, content: t.original, error: null } : t)))
    setLeaveConfirm(false)
    onOpenChange(false)
  }, [onOpenChange])

  const anyDirty = tabs.some(isDirty)
  const handleOpenChange = useCallback(
    (next: boolean) => {
      // Closing with unsaved tabs warns first; confirming restores the opened (clean)
      // state and closes. A clean close (or opening) passes straight through.
      if (!next && anyDirty) {
        setLeaveConfirm(true)
        return
      }
      onOpenChange(next)
    },
    [anyDirty, onOpenChange],
  )

  // ---- Tree node renderer ----

  const NodeRenderer = useCallback(
    ({ node, style, dragHandle }: NodeRendererProps<FileNode>) => {
      const d = node.data
      // Single click selects (and a folder also toggles); double click opens a file.
      // Keyboard Enter/Space opens a file (a11y parity with double click).
      const onRowClick = () => {
        if (node.isEditing) return
        node.select()
        if (d.isDir) node.toggle()
      }
      const onRowDoubleClick = () => {
        if (node.isEditing || d.isDir) return
        void openFile(d.id)
      }
      const onRowActivate = () => {
        if (node.isEditing) return
        if (d.isDir) node.toggle()
        else void openFile(d.id)
      }
      return (
        <ContextMenu>
          <ContextMenuTrigger asChild>
            {/* biome-ignore lint/a11y/noStaticElementInteractions: react-arborist owns row focus/keyboard; this is the drag handle + click target */}
            <div
              ref={dragHandle}
              style={style}
              onClick={onRowClick}
              onDoubleClick={onRowDoubleClick}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onRowActivate()
                }
              }}
              className={cn(
                'flex h-7 cursor-pointer select-none items-center gap-1 rounded px-1 text-[13px] text-zinc-700 hover:bg-zinc-100',
                node.isSelected && 'bg-blue-100 text-zinc-900',
                activePath === d.id && !d.isDir && 'bg-blue-50',
              )}>
              <span className="flex w-4 shrink-0 items-center justify-center text-zinc-400">
                {d.isDir ? (
                  <ChevronRight className={cn('h-3.5 w-3.5 transition-transform', node.isOpen && 'rotate-90')} />
                ) : null}
              </span>
              {d.isDir ? (
                node.isOpen ? (
                  <FolderOpen className="h-4 w-4 shrink-0 text-blue-500" />
                ) : (
                  <FolderIcon className="h-4 w-4 shrink-0 text-blue-500" />
                )
              ) : (
                <FileIcon className="h-4 w-4 shrink-0 text-zinc-400" />
              )}
              {node.isEditing ? (
                <input
                  ref={(el) => el?.focus()}
                  defaultValue={d.name}
                  onClick={(e) => e.stopPropagation()}
                  onBlur={() => node.reset()}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') node.submit(e.currentTarget.value)
                    if (e.key === 'Escape') node.reset()
                  }}
                  className="h-5 flex-1 rounded border border-blue-400 bg-white px-1 text-[13px] outline-none"
                />
              ) : (
                <span className="truncate">{d.name}</span>
              )}
            </div>
          </ContextMenuTrigger>
          <ContextMenuContent>
            {d.isDir ? (
              <>
                <ContextMenuItem
                  onSelect={() => {
                    node.open()
                    setCreateName('')
                    setCreateTarget({ dir: d.id, kind: 'file' })
                  }}>
                  <FilePlus className="h-4 w-4" /> New File
                </ContextMenuItem>
                <ContextMenuItem
                  onSelect={() => {
                    node.open()
                    setCreateName('')
                    setCreateTarget({ dir: d.id, kind: 'dir' })
                  }}>
                  <FolderPlus className="h-4 w-4" /> New Folder
                </ContextMenuItem>
              </>
            ) : (
              <ContextMenuItem onSelect={() => void openFile(d.id)}>
                <FileIcon className="h-4 w-4" /> Open
              </ContextMenuItem>
            )}
            <ContextMenuItem
              onSelect={() => {
                setRenameName(d.name)
                setRenameTarget({ path: d.id, isDir: d.isDir })
              }}>
              Rename
            </ContextMenuItem>
            <ContextMenuSeparator />
            <ContextMenuItem variant="destructive" onSelect={() => setDeleteTarget({ path: d.id, isDir: d.isDir })}>
              Delete
            </ContextMenuItem>
          </ContextMenuContent>
        </ContextMenu>
      )
    },
    [activePath, openFile],
  )

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="flex h-screen w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-white p-0 text-zinc-900 sm:rounded-none">
          <DialogHeader className="flex shrink-0 flex-row items-center justify-between border-b px-3 py-2 text-left">
            <DialogTitle className="text-sm font-semibold text-zinc-900">File Explorer</DialogTitle>
          </DialogHeader>

          <PanelGroup direction="horizontal" className="min-h-0 flex-1">
            {/* Left: file tree */}
            <Panel defaultSize={22} minSize={12} className="flex min-w-0 flex-col">
              <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1">
                <span className="mr-auto truncate text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                  Workspace
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 text-zinc-500"
                  title="New File"
                  onClick={() => {
                    setCreateName('')
                    setCreateTarget({ dir: '', kind: 'file' })
                  }}>
                  <FilePlus className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 text-zinc-500"
                  title="New Folder"
                  onClick={() => {
                    setCreateName('')
                    setCreateTarget({ dir: '', kind: 'dir' })
                  }}>
                  <FolderPlus className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 text-zinc-500"
                  title="Refresh"
                  onClick={() => {
                    loadedRef.current.clear()
                    void reloadDir('')
                  }}>
                  <RefreshCw className="h-3.5 w-3.5" />
                </Button>
              </div>
              <div ref={treeRef} className="min-h-0 flex-1 overflow-hidden bg-white">
                <Tree<FileNode>
                  data={data}
                  openByDefault={false}
                  width={treeSize.width}
                  height={treeSize.height}
                  rowHeight={28}
                  indent={14}
                  onToggle={onToggle}
                  onRename={handleRename}
                  onMove={handleMove}>
                  {NodeRenderer}
                </Tree>
              </div>
            </Panel>

            <PanelResizeHandle className="w-px bg-zinc-200 transition-colors hover:bg-blue-400 data-[resize-handle-state=drag]:bg-blue-500" />

            {/* Right: tabbed editor */}
            <Panel minSize={30} className="flex min-w-0 flex-col">
              <div className="flex shrink-0 items-center overflow-x-auto border-b bg-zinc-50">
                {tabs.map((t) => (
                  <ContextMenu key={t.path}>
                    <ContextMenuTrigger asChild>
                      {/* biome-ignore lint/a11y/noStaticElementInteractions: editor tab is a click/keyboard target with a nested close button (cannot be a <button>) */}
                      <div
                        onClick={() => setActivePath(t.path)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            setActivePath(t.path)
                          }
                        }}
                        className={cn(
                          'flex max-w-[14rem] cursor-pointer items-center gap-1 border-r px-3 py-1.5 text-[13px]',
                          activePath === t.path ? 'bg-white text-zinc-900' : 'text-zinc-500 hover:bg-zinc-100',
                        )}
                        title={t.path}>
                        {t.saving || t.loading ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                        <span className="truncate">{t.name}</span>
                        {isDirty(t) ? <span className="ml-1 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500" /> : null}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            requestCloseTab(t.path)
                          }}
                          className="ml-1 rounded p-0.5 text-zinc-400 hover:bg-zinc-200 hover:text-zinc-700"
                          aria-label={`Close ${t.name}`}>
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    </ContextMenuTrigger>
                    <ContextMenuContent>
                      <ContextMenuItem onSelect={() => requestCloseTab(t.path)}>Close</ContextMenuItem>
                      <ContextMenuItem
                        disabled={tabs.length < 2}
                        onSelect={() => requestCloseTabs(tabs.filter((x) => x.path !== t.path).map((x) => x.path))}>
                        Close Others
                      </ContextMenuItem>
                      <ContextMenuItem onSelect={() => requestCloseTabs(tabs.map((x) => x.path))}>
                        Close All
                      </ContextMenuItem>
                    </ContextMenuContent>
                  </ContextMenu>
                ))}
                {activeTab ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="ml-auto mr-1 h-7 gap-1 text-[12px] text-zinc-600"
                    disabled={!isDirty(activeTab) || activeTab.saving}
                    onClick={() => void saveTab(activeTab.path)}>
                    {activeTab.saving ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Save className="h-3.5 w-3.5" />
                    )}
                    Save
                  </Button>
                ) : null}
              </div>

              <div className="relative min-h-0 flex-1 bg-white">
                {activeTab == null ? (
                  <div className="flex h-full items-center justify-center text-sm text-zinc-400">
                    Select a file from the tree to open it.
                  </div>
                ) : activeTab.loading ? (
                  <div className="flex h-full items-center justify-center gap-2 text-sm text-zinc-500">
                    <Loader2 className="h-4 w-4 animate-spin" /> Opening {activeTab.name}...
                  </div>
                ) : activeTab.isBinary ? (
                  <div className="flex h-full items-center justify-center text-sm text-zinc-400">
                    Binary file -- opened read-only (not editable).
                  </div>
                ) : (
                  <>
                    {activeTab.error ? (
                      <div className="border-b border-red-200 bg-red-50 px-3 py-1 text-[12px] text-red-700">
                        {activeTab.error}
                      </div>
                    ) : null}
                    <Editor
                      path={activeTab.path}
                      value={activeTab.content}
                      theme="vs"
                      onChange={onEditorChange}
                      onMount={handleMount}
                      options={{
                        readOnly: false,
                        minimap: { enabled: false },
                        fontSize: 13,
                        automaticLayout: true,
                        scrollBeyondLastLine: false,
                        tabSize: 2,
                      }}
                    />
                  </>
                )}
              </div>
            </Panel>
          </PanelGroup>
        </DialogContent>
      </Dialog>

      {/* New file/folder name dialog */}
      <Dialog open={createTarget !== null} onOpenChange={(o) => !o && setCreateTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>New {createTarget?.kind === 'dir' ? 'Folder' : 'File'}</DialogTitle>
          </DialogHeader>
          <Input
            autoFocus
            value={createName}
            placeholder={createTarget?.kind === 'dir' ? 'folder-name' : 'file-name.txt'}
            onChange={(e) => setCreateName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void submitCreate()
            }}
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setCreateTarget(null)}>
              Cancel
            </Button>
            <Button disabled={!createName.trim() || createBusy} onClick={() => void submitCreate()}>
              {createBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Rename dialog (reliable; avoids the inline-edit focus race) */}
      <Dialog open={renameTarget !== null} onOpenChange={(o) => !o && !renameBusy && setRenameTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Rename {renameTarget?.isDir ? 'folder' : 'file'}</DialogTitle>
          </DialogHeader>
          <Input
            autoFocus
            value={renameName}
            onChange={(e) => setRenameName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void submitRename()
            }}
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRenameTarget(null)}>
              Cancel
            </Button>
            <Button
              disabled={
                !renameName.trim() ||
                renameName.trim() === (renameTarget ? baseName(renameTarget.path) : '') ||
                renameBusy
              }
              onClick={() => void submitRename()}>
              {renameBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Rename'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation (UDR-0069 D8) */}
      <AlertDialog open={deleteTarget !== null} onOpenChange={(o) => !o && !deleting && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {deleteTarget?.isDir ? 'folder' : 'file'}?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{deleteTarget ? baseName(deleteTarget.path) : ''}&quot;
              {deleteTarget?.isDir ? ' and all its contents' : ''} will be permanently deleted. This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                void confirmDelete()
              }}
              disabled={deleting}>
              {deleting ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Delete'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Unsaved single-tab close warning */}
      <AlertDialog open={pendingCloseTab !== null} onOpenChange={(o) => !o && setPendingCloseTab(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              This file has unsaved changes. Closing the tab will discard them.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingCloseTab) removeTab(pendingCloseTab)
                setPendingCloseTab(null)
              }}>
              Discard
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Overlay-close warning: confirm discards edits and resets tabs to opened state (issue 1) */}
      <AlertDialog open={leaveConfirm} onOpenChange={(o) => !o && setLeaveConfirm(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              You have unsaved changes in one or more tabs. Closing the File Explorer will discard them and restore
              those files to the content they were opened with.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction onClick={discardAndClose}>Discard and close</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Bulk Close All / Close Others when some target tabs are unsaved (issue 2) */}
      <AlertDialog open={pendingBulkClose !== null} onOpenChange={(o) => !o && setPendingBulkClose(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingBulkClose
                ? `${pendingBulkClose.filter((p) => tabs.some((t) => t.path === p && isDirty(t))).length} of the tabs being closed have unsaved changes. Closing them will discard those changes.`
                : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingBulkClose) doCloseTabs(pendingBulkClose)
                setPendingBulkClose(null)
              }}>
              Close anyway
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
