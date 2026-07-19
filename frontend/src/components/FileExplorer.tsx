import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  DragOverlay,
  type DragStartEvent,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import Editor, { type OnMount } from '@monaco-editor/react'
import {
  ChevronRight,
  Download,
  File as FileIcon,
  FilePlus,
  Folder as FolderIcon,
  FolderOpen,
  FolderPlus,
  FolderUp,
  Loader2,
  Paperclip,
  RefreshCw,
  Save,
  SplitSquareHorizontal,
  Upload,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { type NodeRendererProps, Tree } from 'react-arborist'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { ImageViewer } from '@/components/file-explorer/ImageViewer'
import { PdfViewer } from '@/components/file-explorer/PdfViewer'
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
 * File Explorer overlay (CTR-0137, FEAT-0049, PRP-0091/PRP-0093, UDR-0069/UDR-0071).
 *
 * A full-screen, VSCode-style overlay rooted at the coding workspace: a left
 * react-arborist file tree, a react-resizable-panels splitter, and a right editor area.
 * All file IO goes through the jailed CTR-0136 API; the editor never resolves paths itself.
 *
 * PRP-0093 (UDR-0071) enhancements:
 * - Download a file (tree / tab menu) or a folder as a ZIP (tree menu) via CTR-0136 v2.
 * - Preview a PDF (pdf.js, custom zoom) or an image (zoom + pan) instead of monaco; the
 *   bytes come from CTR-0136 /raw as an authenticated blob URL (revoked on tab close).
 * - Editor GROUPS: the single editor pane generalizes to N groups in a nested
 *   react-resizable-panels layout; tabs move between groups by @dnd-kit drag-and-drop, and
 *   dropping a tab on a group edge splits it. A path is open in at most one group (move
 *   relocates; re-opening focuses). The dirty / close confirmations are generalized across
 *   groups; the layout is ephemeral (flattened to one group when the overlay re-opens).
 *
 * Controlled component: the parent (ChatPage) owns open state so both the sidebar
 * footer icon and the /files slash command drive one instance.
 */

interface FileExplorerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * Attach an image/PDF preview to the chat composer (CTR-0137 v?, PRP-0116).
   * When provided, image/PDF viewer tabs show an "Attach to chat" action
   * (enabled only when the overlay is closeable -- no dirty tabs). Handing a
   * File up keeps the active thread id owned by the composer.
   */
  onAttach?: (file: File) => void
}

/** Best-guess MIME for a workspace media file being attached to the composer. */
function attachMime(name: string, kind: TabKind): string {
  if (kind === 'pdf') return 'application/pdf'
  const e = extOf(name)
  const map: Record<string, string> = {
    png: 'image/png',
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    gif: 'image/gif',
    webp: 'image/webp',
  }
  return map[e] ?? `image/${e || 'octet-stream'}`
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

type TabKind = 'text' | 'image' | 'pdf'

interface Tab {
  path: string
  name: string
  kind: TabKind
  loading: boolean
  saving: boolean
  error: string | null
  content: string
  original: string
  mtime: number
  isBinary: boolean
  blobUrl?: string // image/pdf preview object URL (revoked on close)
}

interface EditorGroup {
  id: string
  tabs: Tab[]
  activePath: string | null
}

// Binary layout tree over editor groups (UDR-0071 D6): a leaf is one group; a split is a
// horizontal/vertical pair. Splitting replaces a leaf with a split; emptying a group
// collapses its leaf (the sibling is promoted).
type LayoutNode =
  | { id: string; kind: 'leaf'; groupId: string }
  | { id: string; kind: 'split'; dir: 'horizontal' | 'vertical'; a: LayoutNode; b: LayoutNode }

type EdgeSide = 'left' | 'right' | 'top' | 'bottom'

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'avif'])

const isDirty = (t: Tab): boolean => t.kind === 'text' && !t.isBinary && !t.loading && t.content !== t.original

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i < 0 ? '' : name.slice(i + 1).toLowerCase()
}
function tabKind(name: string): TabKind {
  const e = extOf(name)
  if (e === 'pdf') return 'pdf'
  if (IMAGE_EXTS.has(e)) return 'image'
  return 'text'
}

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

// ---- Layout-tree helpers (pure) ----

function leafGroupIds(node: LayoutNode | null): string[] {
  if (!node) return []
  if (node.kind === 'leaf') return [node.groupId]
  return [...leafGroupIds(node.a), ...leafGroupIds(node.b)]
}

function removeLeaf(node: LayoutNode, groupId: string): LayoutNode | null {
  if (node.kind === 'leaf') return node.groupId === groupId ? null : node
  const a = removeLeaf(node.a, groupId)
  const b = removeLeaf(node.b, groupId)
  if (a === null) return b
  if (b === null) return a
  if (a === node.a && b === node.b) return node
  return { ...node, a, b }
}

function splitLeaf(
  node: LayoutNode,
  targetGroupId: string,
  dir: 'horizontal' | 'vertical',
  newLeaf: LayoutNode,
  placeNewFirst: boolean,
  splitId: string,
): LayoutNode {
  if (node.kind === 'leaf') {
    if (node.groupId !== targetGroupId) return node
    return {
      id: splitId,
      kind: 'split',
      dir,
      a: placeNewFirst ? newLeaf : node,
      b: placeNewFirst ? node : newLeaf,
    }
  }
  return {
    ...node,
    a: splitLeaf(node.a, targetGroupId, dir, newLeaf, placeNewFirst, splitId),
    b: splitLeaf(node.b, targetGroupId, dir, newLeaf, placeNewFirst, splitId),
  }
}

// ---- Download helper (CTR-0136 v2) ----

/** Revoke an image/pdf preview object URL when its tab is removed. */
function revokeTab(t: Tab): void {
  if (t.blobUrl) URL.revokeObjectURL(t.blobUrl)
}

async function downloadUrl(url: string, filename: string): Promise<void> {
  try {
    const res = await fetch(url)
    if (!res.ok) return
    const blob = await res.blob()
    const objectUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(objectUrl)
  } catch {
    // silent: download is best-effort
  }
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

export function FileExplorer({ open, onOpenChange, onAttach }: FileExplorerProps) {
  const [data, setData] = useState<FileNode[]>([])
  const [rootLoaded, setRootLoaded] = useState(false)
  const loadedRef = useRef<Set<string>>(new Set())

  // Editor groups + layout tree (UDR-0071 D6).
  const [groups, setGroups] = useState<Record<string, EditorGroup>>({})
  const [layout, setLayout] = useState<LayoutNode | null>(null)
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null)

  // Unique-id generator for groups / split nodes (instance-scoped).
  const uidRef = useRef(0)
  const newId = useCallback((p: string) => `${p}${++uidRef.current}`, [])

  // Refs mirror the latest state for use inside event callbacks (drag end, open effect)
  // without stale closures.
  const groupsRef = useRef(groups)
  groupsRef.current = groups
  const layoutRef = useRef(layout)
  layoutRef.current = layout
  const activeGroupRef = useRef(activeGroupId)
  activeGroupRef.current = activeGroupId

  const [createTarget, setCreateTarget] = useState<{ dir: string; kind: 'file' | 'dir' } | null>(null)
  const [createName, setCreateName] = useState('')
  const [createBusy, setCreateBusy] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ path: string; isDir: boolean } | null>(null)
  const [renameName, setRenameName] = useState('')
  const [renameBusy, setRenameBusy] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<{ path: string; isDir: boolean } | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [pendingCloseTab, setPendingCloseTab] = useState<{ groupId: string; path: string } | null>(null)
  const [pendingBulkClose, setPendingBulkClose] = useState<{ groupId: string; paths: string[] } | null>(null)
  const [leaveConfirm, setLeaveConfirm] = useState(false)

  const [dragTab, setDragTab] = useState<{ groupId: string; path: string; name: string } | null>(null)

  const [treeRef, treeSize] = useElementSize()

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))

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

  // Refresh the whole tree from the root, showing a spinning indicator until the
  // reload completes (v0.90.1). Collapses the lazy-loaded cache so every level is
  // re-fetched on next expand. The indicator is held for at least one full spin
  // cycle (1s, matching Tailwind's animate-spin) so a fast refresh is still visible.
  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    loadedRef.current.clear()
    const minSpin = new Promise((resolve) => setTimeout(resolve, 1000))
    try {
      await Promise.all([reloadDir(''), minSpin])
    } finally {
      setRefreshing(false)
    }
  }, [reloadDir])

  useEffect(() => {
    if (open && !rootLoaded) void reloadDir('')
  }, [open, rootLoaded, reloadDir])

  // ---- Local upload (CTR-0137, PRP-0104, UDR-0083) ----
  // Two hidden inputs (files / folder) whose target directory is set just before
  // .click(). The upload rides an XHR so upload.onprogress drives an OVERALL
  // determinate progress bar across every file in the request.
  const fileUploadRef = useRef<HTMLInputElement>(null)
  const folderUploadRef = useRef<HTMLInputElement>(null)
  const uploadDirRef = useRef<string>('')
  const [uploadState, setUploadState] = useState<{
    dir: string
    loaded: number
    total: number
    count: number
  } | null>(null)

  const doUpload = useCallback(
    (dir: string, fileList: FileList | null) => {
      if (!fileList || fileList.length === 0) return
      const files = Array.from(fileList)
      const form = new FormData()
      form.append('dir', dir)
      for (const f of files) {
        form.append('files', f)
        // webkitRelativePath is populated for a folder (webkitdirectory) pick, so
        // the backend recreates the folder tree; empty for a plain multi-file pick.
        const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || ''
        form.append('paths', rel)
      }
      const totalBytes = files.reduce((sum, f) => sum + f.size, 0)
      setUploadState({ dir, loaded: 0, total: totalBytes, count: files.length })
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/workspace/upload')
      xhr.withCredentials = true
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          setUploadState((s) => (s ? { ...s, loaded: e.loaded, total: e.total } : s))
        }
      }
      xhr.onload = () => {
        setUploadState(null)
        if (xhr.status >= 200 && xhr.status < 300) {
          loadedRef.current.delete(dir)
          void reloadDir(dir)
        } else {
          let msg = `Upload failed (HTTP ${xhr.status})`
          try {
            const detail = JSON.parse(xhr.responseText)?.detail
            if (typeof detail === 'string') msg = detail
          } catch {
            // keep the default message
          }
          window.alert(msg)
        }
      }
      xhr.onerror = () => {
        setUploadState(null)
        window.alert('Upload failed (network error)')
      }
      xhr.send(form)
    },
    [reloadDir],
  )

  const triggerUpload = useCallback((dir: string, kind: 'files' | 'folder') => {
    uploadDirRef.current = dir
    const el = kind === 'files' ? fileUploadRef.current : folderUploadRef.current
    if (el) {
      el.value = ''
      el.click()
    }
  }, [])

  const uploadPct =
    uploadState && uploadState.total > 0 ? Math.min(100, Math.round((uploadState.loaded / uploadState.total) * 100)) : 0

  // On (re)open, flatten all open tabs into a single group (UDR-0071 D8: layout is
  // ephemeral and reset on reopen; open files are preserved).
  const wasOpenRef = useRef(false)
  useEffect(() => {
    if (open && !wasOpenRef.current) {
      const ids = leafGroupIds(layoutRef.current)
      const all = ids.flatMap((id) => groupsRef.current[id]?.tabs ?? [])
      const gid = newId('g')
      const prevActive = activeGroupRef.current ? groupsRef.current[activeGroupRef.current]?.activePath : null
      const active = all.find((t) => t.path === prevActive)?.path ?? all[all.length - 1]?.path ?? null
      setGroups({ [gid]: { id: gid, tabs: all, activePath: active } })
      setLayout({ id: gid, kind: 'leaf', groupId: gid })
      setActiveGroupId(gid)
    }
    wasOpenRef.current = open
  }, [open, newId])

  // Keep activeGroupId pointing at a live group. Only ids that actually exist
  // in `groups` qualify -- a stale layout leaf must not be re-adopted (defect
  // fix, v0.102.0: re-pointing at a dead leaf id made the explorer ignore
  // every subsequent file open until a page reload).
  useEffect(() => {
    if (activeGroupId && !groups[activeGroupId]) {
      setActiveGroupId(leafGroupIds(layout).find((id) => !!groups[id]) ?? null)
    }
  }, [groups, layout, activeGroupId])

  const onToggle = useCallback(
    (id: string) => {
      if (!loadedRef.current.has(id)) void reloadDir(id)
    },
    [reloadDir],
  )

  // ---- Tab helpers ----

  // Update a tab (matched by unique path) across every group.
  const mapTab = useCallback((path: string, fn: (t: Tab) => Tab) => {
    setGroups((prev) => {
      const next: Record<string, EditorGroup> = {}
      for (const [id, g] of Object.entries(prev)) {
        next[id] = { ...g, tabs: g.tabs.map((t) => (t.path === path ? fn(t) : t)) }
      }
      return next
    })
  }, [])

  // Locate a tab (path is unique across groups). Reads the live ref, so it is stable.
  const findTab = useCallback((path: string): { groupId: string; tab: Tab } | null => {
    for (const g of Object.values(groupsRef.current)) {
      const tab = g.tabs.find((t) => t.path === path)
      if (tab) return { groupId: g.id, tab }
    }
    return null
  }, [])

  const openFile = useCallback(
    async (path: string) => {
      // Already open: focus its tab + group (path is unique across groups).
      const existing = findTab(path)
      if (existing) {
        setActiveGroupId(existing.groupId)
        setGroups((prev) => {
          const g = prev[existing.groupId]
          if (!g) return prev
          return { ...prev, [existing.groupId]: { ...g, activePath: path } }
        })
        return
      }

      const kind = tabKind(baseName(path))
      // Resolve the destination group (active, else create one).
      let targetId = activeGroupRef.current
      if (!targetId || !groupsRef.current[targetId]) targetId = newId('g')
      const tid = targetId

      const newTab: Tab = {
        path,
        name: baseName(path),
        kind,
        loading: true,
        saving: false,
        error: null,
        content: '',
        original: '',
        mtime: 0,
        isBinary: false,
      }
      setGroups((prev) => {
        const next = { ...prev }
        const g = next[tid] ?? { id: tid, tabs: [], activePath: null }
        if (g.tabs.some((t) => t.path === path)) {
          next[tid] = { ...g, activePath: path }
        } else {
          next[tid] = { ...g, tabs: [...g.tabs, newTab], activePath: path }
        }
        return next
      })
      setLayout((prev) => prev ?? { id: tid, kind: 'leaf', groupId: tid })
      setActiveGroupId(tid)

      try {
        if (kind === 'text') {
          const res = await fetch(`/api/workspace/file?path=${encodeURIComponent(path)}`)
          if (!res.ok) {
            const detail = await res.json().catch(() => ({}))
            mapTab(path, (t) => ({ ...t, loading: false, error: detail.detail || 'Failed to open' }))
            return
          }
          const json = await res.json()
          mapTab(path, (t) => ({
            ...t,
            loading: false,
            error: null,
            content: json.content ?? '',
            original: json.content ?? '',
            mtime: json.mtime ?? 0,
            isBinary: !!json.is_binary,
          }))
        } else {
          const res = await fetch(`/api/workspace/raw?path=${encodeURIComponent(path)}`)
          if (!res.ok) {
            const detail = await res.json().catch(() => ({}))
            mapTab(path, (t) => ({ ...t, loading: false, error: detail.detail || 'Failed to open' }))
            return
          }
          const blob = await res.blob()
          const blobUrl = URL.createObjectURL(blob)
          mapTab(path, (t) => ({ ...t, loading: false, error: null, blobUrl }))
        }
      } catch {
        mapTab(path, (t) => ({ ...t, loading: false, error: 'Failed to open' }))
      }
    },
    [mapTab, newId, findTab],
  )

  const onEditorChange = useCallback(
    (value: string | undefined) => {
      const gid = activeGroupRef.current
      const g = gid ? groupsRef.current[gid] : null
      const path = g?.activePath
      if (!path) return
      mapTab(path, (t) => ({ ...t, content: value ?? '' }))
    },
    [mapTab],
  )

  const saveTab = useCallback(
    async (path: string) => {
      const found = findTab(path)
      const target = found?.tab
      if (!target || !isDirty(target)) return
      mapTab(path, (t) => ({ ...t, saving: true, error: null }))
      try {
        const res = await fetch('/api/workspace/file', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path, content: target.content, expected_mtime: target.mtime }),
        })
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}))
          const msg = res.status === 409 ? 'File changed on disk; reopen to merge.' : detail.detail || 'Save failed'
          mapTab(path, (t) => ({ ...t, saving: false, error: msg }))
          return
        }
        const json = await res.json()
        mapTab(path, (t) => ({ ...t, saving: false, error: null, original: t.content, mtime: json.mtime ?? t.mtime }))
        void reloadDir(parentOf(path))
      } catch {
        mapTab(path, (t) => ({ ...t, saving: false, error: 'Save failed' }))
      }
    },
    [mapTab, reloadDir, findTab],
  )

  // Stable ref so the monaco Ctrl/Cmd+S command always saves the active group's active tab.
  const saveActiveRef = useRef<() => void>(() => {})
  saveActiveRef.current = () => {
    const gid = activeGroupRef.current
    const path = gid ? groupsRef.current[gid]?.activePath : null
    if (path) void saveTab(path)
  }
  const handleMount: OnMount = useCallback((editor, monaco) => {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveActiveRef.current())
  }, [])

  // ---- Close / move / split tabs (group-aware) ----

  const removeTabsFrom = useCallback((groupId: string, paths: string[]) => {
    const drop = new Set(paths)
    const g = groupsRef.current[groupId]
    g?.tabs.forEach((t) => {
      if (drop.has(t.path)) revokeTab(t)
    })
    const emptied = !!g && g.tabs.every((t) => drop.has(t.path))
    setGroups((prev) => {
      const f = prev[groupId]
      if (!f) return prev
      const tabs = f.tabs.filter((t) => !drop.has(t.path))
      const next = { ...prev }
      if (tabs.length) {
        const activePath = f.activePath && drop.has(f.activePath) ? (tabs[tabs.length - 1]?.path ?? null) : f.activePath
        next[groupId] = { ...f, tabs, activePath }
      } else {
        delete next[groupId]
      }
      return next
    })
    // NOTE: removeLeaf returns null when the LAST leaf is removed -- the layout
    // must become null (empty state) in that case. Coalescing null back to
    // `prev` kept a leaf pointing at the deleted group, and openFile's
    // `setLayout(prev => prev ?? ...)` then never re-attached a new group:
    // double-clicking tree files silently did nothing until a reload (defect
    // fix, v0.102.0).
    if (emptied) setLayout((prev) => (prev ? removeLeaf(prev, groupId) : prev))
  }, [])

  const requestCloseTab = useCallback(
    (groupId: string, path: string) => {
      const t = groupsRef.current[groupId]?.tabs.find((x) => x.path === path)
      if (t && isDirty(t)) setPendingCloseTab({ groupId, path })
      else removeTabsFrom(groupId, [path])
    },
    [removeTabsFrom],
  )

  const requestCloseTabs = useCallback(
    (groupId: string, paths: string[]) => {
      if (!paths.length) return
      const drop = new Set(paths)
      const anyDirty = groupsRef.current[groupId]?.tabs.some((t) => drop.has(t.path) && isDirty(t))
      if (anyDirty) setPendingBulkClose({ groupId, paths })
      else removeTabsFrom(groupId, paths)
    },
    [removeTabsFrom],
  )

  // Move a tab to another group (drag-and-drop; relocation keeps a path in one group).
  const moveTabToGroup = useCallback((path: string, fromId: string, toId: string) => {
    if (fromId === toId) return
    const from = groupsRef.current[fromId]
    const tab = from?.tabs.find((t) => t.path === path)
    if (!from || !tab) return
    const emptied = from.tabs.length === 1
    setGroups((prev) => {
      const next = { ...prev }
      const f = next[fromId]
      const to = next[toId]
      if (!f || !to) return prev
      const fromTabs = f.tabs.filter((t) => t.path !== path)
      if (fromTabs.length) {
        const activePath = f.activePath === path ? (fromTabs[fromTabs.length - 1]?.path ?? null) : f.activePath
        next[fromId] = { ...f, tabs: fromTabs, activePath }
      } else {
        delete next[fromId]
      }
      next[toId] = { ...to, tabs: to.tabs.some((t) => t.path === path) ? to.tabs : [...to.tabs, tab], activePath: path }
      return next
    })
    // Same null-means-empty contract as removeTabsFrom (defect fix, v0.102.0).
    if (emptied) setLayout((prev) => (prev ? removeLeaf(prev, fromId) : prev))
    setActiveGroupId(toId)
  }, [])

  // Split a target group by pulling a tab out into a new group on the given side.
  const splitWithTab = useCallback(
    (path: string, fromId: string, targetId: string, side: EdgeSide) => {
      const from = groupsRef.current[fromId]
      const tab = from?.tabs.find((t) => t.path === path)
      if (!from || !tab) return
      const onlyTab = from.tabs.length === 1
      if (fromId === targetId && onlyTab) return // nothing to gain
      const newGroupId = newId('g')
      const splitId = newId('s')
      const dir = side === 'left' || side === 'right' ? 'horizontal' : 'vertical'
      const placeNewFirst = side === 'left' || side === 'top'
      setGroups((prev) => {
        const next = { ...prev }
        const f = next[fromId]
        if (!f) return prev
        const fromTabs = f.tabs.filter((t) => t.path !== path)
        if (fromTabs.length) {
          const activePath = f.activePath === path ? (fromTabs[fromTabs.length - 1]?.path ?? null) : f.activePath
          next[fromId] = { ...f, tabs: fromTabs, activePath }
        } else {
          delete next[fromId]
        }
        next[newGroupId] = { id: newGroupId, tabs: [tab], activePath: path }
        return next
      })
      setLayout((prev) => {
        if (!prev) return prev
        let l = splitLeaf(
          prev,
          targetId,
          dir,
          { id: newGroupId, kind: 'leaf', groupId: newGroupId },
          placeNewFirst,
          splitId,
        )
        if (onlyTab && fromId !== targetId) l = removeLeaf(l, fromId) ?? l
        return l
      })
      setActiveGroupId(newGroupId)
    },
    [newId],
  )

  const splitActiveRight = useCallback(
    (groupId: string) => {
      const g = groupsRef.current[groupId]
      if (!g || g.tabs.length < 2 || !g.activePath) return
      splitWithTab(g.activePath, groupId, groupId, 'right')
    },
    [splitWithTab],
  )

  const onDragStart = useCallback((e: DragStartEvent) => {
    const d = e.active.data.current as { groupId: string; path: string; name: string } | undefined
    if (d) setDragTab(d)
  }, [])

  const onDragEnd = useCallback(
    (e: DragEndEvent) => {
      const active = e.active.data.current as { groupId: string; path: string } | undefined
      const over = e.over?.data.current as { type: 'strip' | 'edge'; groupId: string; side?: EdgeSide } | undefined
      setDragTab(null)
      if (!active || !over) return
      if (over.type === 'strip') moveTabToGroup(active.path, active.groupId, over.groupId)
      else if (over.type === 'edge' && over.side) splitWithTab(active.path, active.groupId, over.groupId, over.side)
    },
    [moveTabToGroup, splitWithTab],
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

  // Re-point any open tab whose path (or whose ancestor dir) was renamed/moved.
  const repointTabs = useCallback((from: string, next: string) => {
    const prefix = `${from}/`
    setGroups((prev) => {
      const out: Record<string, EditorGroup> = {}
      for (const [id, g] of Object.entries(prev)) {
        const tabs = g.tabs.map((t) => {
          if (t.path === from) return { ...t, path: next, name: baseName(next) }
          if (t.path.startsWith(prefix)) return { ...t, path: next + t.path.slice(from.length) }
          return t
        })
        const activePath =
          g.activePath === from
            ? next
            : g.activePath?.startsWith(prefix)
              ? next + g.activePath.slice(from.length)
              : g.activePath
        out[id] = { ...g, tabs, activePath }
      }
      return out
    })
  }, [])

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
      repointTabs(from, next)
      return true
    },
    [reloadDir, repointTabs],
  )

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

  const handleTreeMove = useCallback(
    async ({ dragIds, parentId }: { dragIds: string[]; parentId: string | null }) => {
      const targetDir = parentId ?? ''
      for (const from of dragIds) {
        const to = joinPath(targetDir, baseName(from))
        if (to === from) continue
        const res = await fetch('/api/workspace/move', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ from, to }),
        })
        if (res.ok) repointTabs(from, to)
        await reloadDir(parentOf(from))
      }
      await reloadDir(targetDir)
    },
    [reloadDir, repointTabs],
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
        // Drop any open tab under the deleted path (file or directory subtree), per group.
        const prefix = `${deleteTarget.path}/`
        const dropped = (t: Tab) => t.path === deleteTarget.path || t.path.startsWith(prefix)
        for (const g of Object.values(groupsRef.current)) {
          const paths = g.tabs.filter(dropped).map((t) => t.path)
          if (paths.length) removeTabsFrom(g.id, paths)
        }
        setDeleteTarget(null)
      }
    } finally {
      setDeleting(false)
    }
  }, [deleteTarget, reloadDir, removeTabsFrom])

  // ---- Download (CTR-0136 v2) ----

  const downloadFile = useCallback((path: string) => {
    void downloadUrl(`/api/workspace/raw?path=${encodeURIComponent(path)}`, baseName(path) || 'download')
  }, [])
  const downloadFolder = useCallback((path: string) => {
    void downloadUrl(`/api/workspace/archive?path=${encodeURIComponent(path)}`, `${baseName(path) || 'workspace'}.zip`)
  }, [])

  // ---- Overlay close: warn if unsaved, then discard + reset on confirm ----

  const discardAndClose = useCallback(() => {
    setGroups((prev) => {
      const out: Record<string, EditorGroup> = {}
      for (const [id, g] of Object.entries(prev)) {
        out[id] = { ...g, tabs: g.tabs.map((t) => (isDirty(t) ? { ...t, content: t.original, error: null } : t)) }
      }
      return out
    })
    setLeaveConfirm(false)
    onOpenChange(false)
  }, [onOpenChange])

  const anyDirty = Object.values(groups).some((g) => g.tabs.some(isDirty))
  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next && anyDirty) {
        setLeaveConfirm(true)
        return
      }
      onOpenChange(next)
    },
    [anyDirty, onOpenChange],
  )

  // Attach an image/PDF preview tab to the chat composer (PRP-0116, CTR-0137).
  // Enabled only when the overlay is closeable (no dirty tabs): fetch the bytes
  // already loaded for the preview blob, wrap as a File, hand it up, and close.
  const attachTabToChat = useCallback(
    async (tab: Tab) => {
      if (!onAttach || anyDirty || !tab.blobUrl || (tab.kind !== 'image' && tab.kind !== 'pdf')) return
      try {
        const blob = await (await fetch(tab.blobUrl)).blob()
        const file = new File([blob], tab.name, { type: attachMime(tab.name, tab.kind) })
        onAttach(file)
        onOpenChange(false)
      } catch {
        // Best-effort: a failed fetch leaves the explorer open (no attachment).
      }
    },
    [onAttach, anyDirty, onOpenChange],
  )

  // ---- Tree node renderer ----

  const NodeRenderer = useCallback(
    ({ node, style, dragHandle }: NodeRendererProps<FileNode>) => {
      const d = node.data
      const activePathOfActiveGroup = activeGroupId ? groups[activeGroupId]?.activePath : null
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
                activePathOfActiveGroup === d.id && !d.isDir && 'bg-blue-50',
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
                <ContextMenuItem onSelect={() => triggerUpload(d.id, 'files')}>
                  <Upload className="h-4 w-4" /> Upload files here
                </ContextMenuItem>
                <ContextMenuItem onSelect={() => triggerUpload(d.id, 'folder')}>
                  <FolderUp className="h-4 w-4" /> Upload folder here
                </ContextMenuItem>
                <ContextMenuItem onSelect={() => downloadFolder(d.id)}>
                  <Download className="h-4 w-4" /> Download as ZIP
                </ContextMenuItem>
              </>
            ) : (
              <>
                <ContextMenuItem onSelect={() => void openFile(d.id)}>
                  <FileIcon className="h-4 w-4" /> Open
                </ContextMenuItem>
                <ContextMenuItem onSelect={() => downloadFile(d.id)}>
                  <Download className="h-4 w-4" /> Download
                </ContextMenuItem>
              </>
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
    [activeGroupId, groups, openFile, downloadFile, downloadFolder, triggerUpload],
  )

  // ---- Editor group rendering ----

  const renderLayout = (node: LayoutNode): React.ReactNode => {
    if (node.kind === 'leaf') {
      const g = groups[node.groupId]
      if (!g) return null
      return (
        <GroupView
          key={node.groupId}
          group={g}
          isActiveGroup={activeGroupId === node.groupId}
          dragging={dragTab !== null}
          onFocusGroup={setActiveGroupId}
          onActivateTab={(path) =>
            setGroups((prev) => {
              const grp = prev[node.groupId]
              return grp ? { ...prev, [node.groupId]: { ...grp, activePath: path } } : prev
            })
          }
          onCloseTab={requestCloseTab}
          onCloseTabs={requestCloseTabs}
          onSplit={splitActiveRight}
          onSave={saveTab}
          onEditorChange={onEditorChange}
          onEditorMount={handleMount}
          onDownloadTab={downloadFile}
          onAttachTab={onAttach ? (tab) => void attachTabToChat(tab) : undefined}
          attachDisabled={anyDirty}
        />
      )
    }
    return (
      <PanelGroup key={node.id} direction={node.dir} className="min-h-0 flex-1">
        <Panel id={node.a.id} order={1} minSize={15} className="flex min-h-0 min-w-0 flex-col">
          {renderLayout(node.a)}
        </Panel>
        <PanelResizeHandle
          className={cn(
            'bg-zinc-200 transition-colors hover:bg-blue-400 data-[resize-handle-state=drag]:bg-blue-500',
            node.dir === 'horizontal' ? 'w-px' : 'h-px',
          )}
        />
        <Panel id={node.b.id} order={2} minSize={15} className="flex min-h-0 min-w-0 flex-col">
          {renderLayout(node.b)}
        </Panel>
      </PanelGroup>
    )
  }

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
                  title="Upload files"
                  disabled={uploadState !== null}
                  onClick={() => triggerUpload('', 'files')}>
                  <Upload className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 text-zinc-500"
                  title="Upload folder"
                  disabled={uploadState !== null}
                  onClick={() => triggerUpload('', 'folder')}>
                  <FolderUp className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 text-zinc-500"
                  title="Refresh"
                  disabled={refreshing}
                  onClick={() => void handleRefresh()}>
                  <RefreshCw className={cn('h-3.5 w-3.5', refreshing && 'animate-spin')} />
                </Button>
              </div>
              {/* Hidden upload inputs (CTR-0137, PRP-0104). The folder input uses the
                  non-standard webkitdirectory attribute for whole-folder selection. */}
              <input
                ref={fileUploadRef}
                type="file"
                multiple
                className="hidden"
                onChange={(e) => doUpload(uploadDirRef.current, e.currentTarget.files)}
              />
              <input
                ref={folderUploadRef}
                type="file"
                className="hidden"
                {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
                onChange={(e) => doUpload(uploadDirRef.current, e.currentTarget.files)}
              />
              {/* Overall upload progress (PRP-0104): a determinate bar across all files. */}
              {uploadState && (
                <div className="flex shrink-0 items-center gap-2 border-b bg-blue-50 px-2 py-1 text-[11px] text-blue-700">
                  <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
                  <span className="shrink-0 whitespace-nowrap">Uploading {uploadState.count}…</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded bg-blue-200">
                    <div className="h-full bg-blue-500 transition-all" style={{ width: `${uploadPct}%` }} />
                  </div>
                  <span className="shrink-0 tabular-nums">{uploadPct}%</span>
                </div>
              )}
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
                  onMove={handleTreeMove}>
                  {NodeRenderer}
                </Tree>
              </div>
            </Panel>

            <PanelResizeHandle className="w-px bg-zinc-200 transition-colors hover:bg-blue-400 data-[resize-handle-state=drag]:bg-blue-500" />

            {/* Right: editor groups */}
            <Panel minSize={30} className="flex min-w-0 flex-col">
              <DndContext
                sensors={sensors}
                collisionDetection={closestCenter}
                onDragStart={onDragStart}
                onDragEnd={onDragEnd}>
                {layout ? (
                  renderLayout(layout)
                ) : (
                  <div className="flex h-full items-center justify-center text-sm text-zinc-400">
                    Select a file from the tree to open it.
                  </div>
                )}
                <DragOverlay>
                  {dragTab ? (
                    <div className="rounded border bg-white px-3 py-1.5 text-[13px] text-zinc-700 shadow">
                      {dragTab.name}
                    </div>
                  ) : null}
                </DragOverlay>
              </DndContext>
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
                if (pendingCloseTab) removeTabsFrom(pendingCloseTab.groupId, [pendingCloseTab.path])
                setPendingCloseTab(null)
              }}>
              Discard
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Overlay-close warning: confirm discards edits and resets tabs to opened state */}
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

      {/* Bulk Close All / Close Others when some target tabs are unsaved */}
      <AlertDialog open={pendingBulkClose !== null} onOpenChange={(o) => !o && setPendingBulkClose(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingBulkClose
                ? `${pendingBulkClose.paths.filter((p) => groups[pendingBulkClose.groupId]?.tabs.some((t) => t.path === p && isDirty(t))).length} of the tabs being closed have unsaved changes. Closing them will discard those changes.`
                : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingBulkClose) removeTabsFrom(pendingBulkClose.groupId, pendingBulkClose.paths)
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

// ---- Inner components (need @dnd-kit hooks per group / tab) ----

interface GroupViewProps {
  group: EditorGroup
  isActiveGroup: boolean
  dragging: boolean
  onFocusGroup: (groupId: string) => void
  onActivateTab: (path: string) => void
  onCloseTab: (groupId: string, path: string) => void
  onCloseTabs: (groupId: string, paths: string[]) => void
  onSplit: (groupId: string) => void
  onSave: (path: string) => void
  onEditorChange: (value: string | undefined) => void
  onEditorMount: OnMount
  onDownloadTab: (path: string) => void
  onAttachTab?: (tab: Tab) => void
  attachDisabled?: boolean
}

function GroupView({
  group,
  isActiveGroup,
  dragging,
  onFocusGroup,
  onActivateTab,
  onCloseTab,
  onCloseTabs,
  onSplit,
  onSave,
  onEditorChange,
  onEditorMount,
  onDownloadTab,
  onAttachTab,
  attachDisabled,
}: GroupViewProps) {
  const activeTab = group.tabs.find((t) => t.path === group.activePath) ?? null
  const { setNodeRef: setStripRef, isOver: stripOver } = useDroppable({
    id: `strip:${group.id}`,
    data: { type: 'strip', groupId: group.id },
  })

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: clicking the group focuses it (keyboard focus handled by inner controls)
    <div className="flex h-full min-h-0 flex-col" onMouseDown={() => onFocusGroup(group.id)}>
      <div
        ref={setStripRef}
        className={cn(
          'flex shrink-0 items-center overflow-x-auto border-b bg-zinc-50',
          stripOver && 'bg-blue-50 ring-1 ring-inset ring-blue-300',
        )}>
        {group.tabs.map((t) => (
          <DraggableTab
            key={t.path}
            tab={t}
            groupId={group.id}
            active={group.activePath === t.path}
            onActivate={onActivateTab}
            onClose={(path) => onCloseTab(group.id, path)}
            onCloseOthers={() =>
              onCloseTabs(
                group.id,
                group.tabs.filter((x) => x.path !== t.path).map((x) => x.path),
              )
            }
            onCloseAll={() =>
              onCloseTabs(
                group.id,
                group.tabs.map((x) => x.path),
              )
            }
            onDownload={onDownloadTab}
            disableCloseOthers={group.tabs.length < 2}
          />
        ))}
        <div className="ml-auto flex items-center pr-1">
          {onAttachTab && activeTab && (activeTab.kind === 'image' || activeTab.kind === 'pdf') ? (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 text-[12px] text-zinc-600"
              disabled={attachDisabled}
              title={
                attachDisabled
                  ? 'Save or discard unsaved changes before attaching'
                  : 'Attach this file to the chat composer'
              }
              onClick={() => onAttachTab(activeTab)}>
              <Paperclip className="h-3.5 w-3.5" />
              Attach to chat
            </Button>
          ) : null}
          {activeTab && isDirty(activeTab) ? (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 text-[12px] text-zinc-600"
              disabled={activeTab.saving}
              onClick={() => onSave(activeTab.path)}>
              {activeTab.saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              Save
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-zinc-500"
            title="Split editor right"
            disabled={group.tabs.length < 2}
            onClick={() => onSplit(group.id)}>
            <SplitSquareHorizontal className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <div className={cn('relative min-h-0 flex-1 bg-white', isActiveGroup && 'ring-1 ring-inset ring-blue-200')}>
        {activeTab == null ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-400">
            Select a file from the tree to open it.
          </div>
        ) : activeTab.loading ? (
          <div className="flex h-full items-center justify-center gap-2 text-sm text-zinc-500">
            <Loader2 className="h-4 w-4 animate-spin" /> Opening {activeTab.name}...
          </div>
        ) : activeTab.error ? (
          <div className="flex h-full flex-col">
            <div className="border-b border-red-200 bg-red-50 px-3 py-1 text-[12px] text-red-700">
              {activeTab.error}
            </div>
            {activeTab.kind === 'text' && !activeTab.isBinary ? (
              <Editor
                path={activeTab.path}
                value={activeTab.content}
                theme="vs"
                onChange={onEditorChange}
                onMount={onEditorMount}
                options={{
                  minimap: { enabled: false },
                  fontSize: 13,
                  automaticLayout: true,
                  scrollBeyondLastLine: false,
                  tabSize: 2,
                }}
              />
            ) : null}
          </div>
        ) : activeTab.kind === 'image' && activeTab.blobUrl ? (
          <ImageViewer url={activeTab.blobUrl} name={activeTab.name} />
        ) : activeTab.kind === 'pdf' && activeTab.blobUrl ? (
          <PdfViewer url={activeTab.blobUrl} name={activeTab.name} />
        ) : activeTab.isBinary ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-400">
            Binary file -- opened read-only (not editable).
          </div>
        ) : (
          <Editor
            path={activeTab.path}
            value={activeTab.content}
            theme="vs"
            onChange={onEditorChange}
            onMount={onEditorMount}
            options={{
              minimap: { enabled: false },
              fontSize: 13,
              automaticLayout: true,
              scrollBeyondLastLine: false,
              tabSize: 2,
            }}
          />
        )}

        {/* Edge drop zones for split-on-drop (only while a tab is being dragged). */}
        {dragging ? <EdgeZones groupId={group.id} /> : null}
      </div>
    </div>
  )
}

interface DraggableTabProps {
  tab: Tab
  groupId: string
  active: boolean
  onActivate: (path: string) => void
  onClose: (path: string) => void
  onCloseOthers: () => void
  onCloseAll: () => void
  onDownload: (path: string) => void
  disableCloseOthers: boolean
}

function DraggableTab({
  tab,
  groupId,
  active,
  onActivate,
  onClose,
  onCloseOthers,
  onCloseAll,
  onDownload,
  disableCloseOthers,
}: DraggableTabProps) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `tab:${groupId}:${tab.path}`,
    data: { groupId, path: tab.path, name: tab.name },
  })
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        {/* biome-ignore lint/a11y/noStaticElementInteractions: drag handle + click target with a nested close button (cannot be a <button>) */}
        <div
          ref={setNodeRef}
          {...attributes}
          {...listeners}
          onClick={() => onActivate(tab.path)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              onActivate(tab.path)
            }
          }}
          className={cn(
            'flex max-w-[14rem] cursor-pointer items-center gap-1 border-r px-3 py-1.5 text-[13px]',
            active ? 'bg-white text-zinc-900' : 'text-zinc-500 hover:bg-zinc-100',
            isDragging && 'opacity-50',
          )}
          title={tab.path}>
          {tab.saving || tab.loading ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
          <span className="truncate">{tab.name}</span>
          {isDirty(tab) ? <span className="ml-1 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500" /> : null}
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              onClose(tab.path)
            }}
            onPointerDown={(e) => e.stopPropagation()}
            className="ml-1 rounded p-0.5 text-zinc-400 hover:bg-zinc-200 hover:text-zinc-700"
            aria-label={`Close ${tab.name}`}>
            <X className="h-3 w-3" />
          </button>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={() => onClose(tab.path)}>Close</ContextMenuItem>
        <ContextMenuItem disabled={disableCloseOthers} onSelect={onCloseOthers}>
          Close Others
        </ContextMenuItem>
        <ContextMenuItem onSelect={onCloseAll}>Close All</ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={() => onDownload(tab.path)}>
          <Download className="h-4 w-4" /> Download
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}

function EdgeZones({ groupId }: { groupId: string }) {
  const left = useDroppable({ id: `edge:${groupId}:left`, data: { type: 'edge', groupId, side: 'left' } })
  const right = useDroppable({ id: `edge:${groupId}:right`, data: { type: 'edge', groupId, side: 'right' } })
  const top = useDroppable({ id: `edge:${groupId}:top`, data: { type: 'edge', groupId, side: 'top' } })
  const bottom = useDroppable({ id: `edge:${groupId}:bottom`, data: { type: 'edge', groupId, side: 'bottom' } })
  const zone = 'absolute z-10 bg-blue-400/0 transition-colors'
  const onCls = 'bg-blue-400/30'
  return (
    <>
      <div ref={left.setNodeRef} className={cn(zone, 'left-0 top-0 h-full w-1/4', left.isOver && onCls)} />
      <div ref={right.setNodeRef} className={cn(zone, 'right-0 top-0 h-full w-1/4', right.isOver && onCls)} />
      <div ref={top.setNodeRef} className={cn(zone, 'left-1/4 top-0 h-1/3 w-1/2', top.isOver && onCls)} />
      <div ref={bottom.setNodeRef} className={cn(zone, 'bottom-0 left-1/4 h-1/3 w-1/2', bottom.isOver && onCls)} />
    </>
  )
}
