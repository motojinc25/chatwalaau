import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  Archive,
  Check,
  ChevronDown,
  ChevronRight,
  Download,
  Folder,
  FolderOpen,
  FolderPlus,
  GripVertical,
  Info,
  Loader2,
  LogOut,
  MoreHorizontal,
  Palette,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import {
  type ChangeEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { AboutDialog } from '@/components/AboutDialog'
import { PermissionsDisabledBanner } from '@/components/PermissionsDisabledBanner'
import { SessionSearchDialog } from '@/components/SessionSearchDialog'
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { useAuth } from '@/hooks/useAuth'
import { cn } from '@/lib/utils'
import {
  DEFAULT_FOLDER_COLOR,
  FOLDER_COLORS,
  type FolderColor,
  type SessionFolder,
  type SessionSummary,
} from '@/types/chat'

interface SessionSidebarProps {
  sessions: SessionSummary[]
  folders: SessionFolder[]
  currentThreadId: string
  creatingFolder: boolean
  deletingFolderId: string | null
  updatingFolderId: string | null
  movingSessionId: string | null
  importing: boolean
  onSwitch: (threadId: string) => void
  onDelete: (threadId: string) => void
  onExport: (threadId: string) => void
  onImport: (file: File) => Promise<boolean>
  onDeleteFolder: (folderId: string) => Promise<boolean>
  onCreateFolder: (name: string, color: FolderColor) => Promise<boolean>
  onUpdateFolderColor: (folderId: string, color: FolderColor) => Promise<boolean>
  onReorderFolders: (orderedIds: string[]) => Promise<boolean>
  onMoveToFolder: (threadId: string, folderId: string | null) => Promise<boolean>
  onRename: (threadId: string, title: string) => void
  onArchive: (threadId: string) => void
  onPin: (threadId: string, pinned: boolean) => void
  onCreate: () => void
  onClose: () => void
}

// Per-device open/closed state (UDR-0046 D4): the set of explicitly-expanded
// folder ids is stored here; unknown / new folders default collapsed.
const FOLDER_EXPANDED_STORAGE_KEY = 'chatwalaau:folders-expanded'

// Palette token -> theme-controlled classes (UDR-0046 D2). Written as literal
// strings so the Tailwind scanner includes them. `neutral` is the uncolored
// (pre-PRP-0070) look.
const FOLDER_COLOR_CLASSES: Record<FolderColor, { border: string; icon: string; swatch: string }> = {
  neutral: { border: 'border-l-transparent', icon: 'text-muted-foreground', swatch: 'bg-muted-foreground/40' },
  red: { border: 'border-l-red-500', icon: 'text-red-500', swatch: 'bg-red-500' },
  orange: { border: 'border-l-orange-500', icon: 'text-orange-500', swatch: 'bg-orange-500' },
  amber: { border: 'border-l-amber-500', icon: 'text-amber-500', swatch: 'bg-amber-500' },
  green: { border: 'border-l-green-500', icon: 'text-green-500', swatch: 'bg-green-500' },
  blue: { border: 'border-l-blue-500', icon: 'text-blue-500', swatch: 'bg-blue-500' },
  violet: { border: 'border-l-violet-500', icon: 'text-violet-500', swatch: 'bg-violet-500' },
  pink: { border: 'border-l-pink-500', icon: 'text-pink-500', swatch: 'bg-pink-500' },
}

function loadExpandedFolderIds(): Set<string> {
  try {
    const raw = localStorage.getItem(FOLDER_EXPANDED_STORAGE_KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw)
    if (Array.isArray(parsed)) {
      return new Set(parsed.filter((id): id is string => typeof id === 'string'))
    }
  } catch {
    // Corrupt localStorage value -> ignore and fall back to default-collapsed
    // (HEAL-1 client side); the next persist overwrites it with a clean value.
  }
  return new Set()
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

function sortSessions(sessions: SessionSummary[]): SessionSummary[] {
  const pinned = sessions
    .filter((s) => s.pinned_at)
    .sort((a, b) => ((a.pinned_at ?? '') < (b.pinned_at ?? '') ? -1 : 1))
  const unpinned = sessions.filter((s) => !s.pinned_at).sort((a, b) => (a.updated_at > b.updated_at ? -1 : 1))
  return [...pinned, ...unpinned]
}

function getSessionMeta(session: SessionSummary): string {
  return [
    formatDateTime(session.updated_at),
    session.message_count > 0 ? `${session.message_count} msgs` : '',
    session.image_count > 0 ? `${session.image_count} imgs` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

interface PaletteSwatchesProps {
  value: FolderColor
  onSelect: (color: FolderColor) => void
  disabled?: boolean
}

function PaletteSwatches({ value, onSelect, disabled }: PaletteSwatchesProps) {
  return (
    <div className="flex flex-wrap gap-2">
      {FOLDER_COLORS.map((color) => (
        <button
          key={color}
          type="button"
          disabled={disabled}
          onClick={() => onSelect(color)}
          aria-label={`Color ${color}`}
          aria-pressed={value === color}
          className={cn(
            'flex h-7 w-7 items-center justify-center rounded-full border transition-transform hover:scale-110 disabled:opacity-50',
            FOLDER_COLOR_CLASSES[color].swatch,
            value === color ? 'ring-2 ring-ring ring-offset-1 ring-offset-background' : 'border-transparent',
          )}>
          {value === color && <Check className="h-3.5 w-3.5 text-white drop-shadow" />}
        </button>
      ))}
    </div>
  )
}

interface FolderGroupProps {
  folder: SessionFolder
  groupedSessions: SessionSummary[]
  isCollapsed: boolean
  isDropTarget: boolean
  deletingFolderId: string | null
  updatingFolderId: string | null
  draggedSessionId: string | null
  onToggle: (folderId: string) => void
  onDragOverFolder: (folderId: string) => void
  onDragLeaveFolder: (folderId: string) => void
  onDropSession: (folderId: string) => void
  onOpenColor: (folder: SessionFolder) => void
  onDeleteFolder: (folder: SessionFolder) => void
  renderSessionRow: (session: SessionSummary, nested?: boolean) => ReactNode
}

function FolderGroup({
  folder,
  groupedSessions,
  isCollapsed,
  isDropTarget,
  deletingFolderId,
  updatingFolderId,
  draggedSessionId,
  onToggle,
  onDragOverFolder,
  onDragLeaveFolder,
  onDropSession,
  onOpenColor,
  onDeleteFolder,
  renderSessionRow,
}: FolderGroupProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id })
  const colorClasses = FOLDER_COLOR_CLASSES[folder.color] ?? FOLDER_COLOR_CLASSES[DEFAULT_FOLDER_COLOR]
  const style = { transform: CSS.Transform.toString(transform), transition }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn('border-t border-border/20 first:border-t-0', isDragging && 'z-10 opacity-80')}>
      {/* biome-ignore lint/a11y/noStaticElementInteractions: folder row is a drag target for native session DnD */}
      <div
        className={cn(
          'group flex items-center gap-1 border-l-2 px-2 py-2 transition-colors',
          colorClasses.border,
          isDropTarget && 'bg-accent/80',
        )}
        onDragOver={(event) => {
          if (!draggedSessionId) return
          event.preventDefault()
          event.dataTransfer.dropEffect = 'move'
          onDragOverFolder(folder.id)
        }}
        onDragLeave={() => onDragLeaveFolder(folder.id)}
        onDrop={(event) => {
          if (!draggedSessionId) return
          event.preventDefault()
          onDropSession(folder.id)
        }}>
        {/* @dnd-kit drag handle: `attributes` injects role="button" + aria-roledescription at runtime, which Biome cannot see statically. */}
        {/* biome-ignore lint/a11y/useAriaPropsSupportedByRole: role is supplied at runtime by @dnd-kit `attributes` */}
        <span
          aria-label="Reorder folder"
          className="flex h-6 w-4 shrink-0 cursor-grab touch-none items-center justify-center text-muted-foreground/60 hover:text-foreground active:cursor-grabbing"
          {...attributes}
          {...listeners}>
          <GripVertical className="h-3.5 w-3.5" />
        </span>
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          onClick={() => onToggle(folder.id)}>
          {isCollapsed ? (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <Folder className={cn('h-3.5 w-3.5 shrink-0', colorClasses.icon)} />
          <span className="truncate text-sm font-medium">{folder.name}</span>
          <span className="rounded-full bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {groupedSessions.length}
          </span>
        </button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            {/* biome-ignore lint/a11y/useSemanticElements: nested interactive, span is intentional */}
            <span
              role="button"
              tabIndex={-1}
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              aria-label="Folder options">
              {deletingFolderId === folder.id || updatingFolderId === folder.id ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <MoreHorizontal className="h-3 w-3" />
              )}
            </span>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-40">
            <DropdownMenuItem onClick={() => onOpenColor(folder)}>
              <Palette className="mr-2 h-3.5 w-3.5" />
              Change color
            </DropdownMenuItem>
            <DropdownMenuItem
              className="text-destructive focus:text-destructive"
              disabled={deletingFolderId === folder.id}
              onClick={() => onDeleteFolder(folder)}>
              <Trash2 className="mr-2 h-3.5 w-3.5" />
              Delete folder
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {!isCollapsed && (
        <div className="pb-1">
          {groupedSessions.length > 0 ? (
            groupedSessions.map((session) => renderSessionRow(session, true))
          ) : (
            <div className="px-9 py-2 text-xs text-muted-foreground">Drop chats here or use the menu</div>
          )}
        </div>
      )}
    </div>
  )
}

export function SessionSidebar({
  sessions,
  folders,
  currentThreadId,
  creatingFolder,
  deletingFolderId,
  updatingFolderId,
  movingSessionId,
  importing,
  onSwitch,
  onDelete,
  onExport,
  onImport,
  onDeleteFolder,
  onCreateFolder,
  onUpdateFolderColor,
  onReorderFolders,
  onMoveToFolder,
  onRename,
  onArchive,
  onPin,
  onCreate,
  onClose,
}: SessionSidebarProps) {
  const sortedSessions = useMemo(() => sortSessions(sessions), [sessions])
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null)
  const [deleteFolderTarget, setDeleteFolderTarget] = useState<SessionFolder | null>(null)
  const [colorTarget, setColorTarget] = useState<SessionFolder | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [createFolderOpen, setCreateFolderOpen] = useState(false)
  const [folderName, setFolderName] = useState('')
  const [newFolderColor, setNewFolderColor] = useState<FolderColor>(DEFAULT_FOLDER_COLOR)
  // UDR-0046 D4: default collapsed. We track the explicitly-EXPANDED set, so a
  // new / unknown folder defaults collapsed without an extra write.
  const [expandedFolderIds, setExpandedFolderIds] = useState<Set<string>>(loadExpandedFolderIds)
  const [draggedSessionId, setDraggedSessionId] = useState<string | null>(null)
  const [dropFolderId, setDropFolderId] = useState<string | null>(null)
  const renameRef = useRef<HTMLInputElement>(null)
  const folderNameRef = useRef<HTMLInputElement>(null)
  // Session Import file picker (PRP-0084, CTR-0016 v4).
  const importInputRef = useRef<HTMLInputElement>(null)

  const handleImportChange = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0]
      // Reset the input so re-selecting the same file fires onChange again.
      event.target.value = ''
      if (file) await onImport(file)
    },
    [onImport],
  )

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  // Web SPA auth (CTR-0096, PRP-0057): the logout entry is only meaningful
  // when the operator has enabled the ID/PW lane AND the current visitor
  // is authenticated via that lane. For loopback / API_KEY-only / open
  // modes the button is hidden.
  const auth = useAuth()
  const showLogout = auth.mode === 'login-required' && auth.authenticated
  const handleLogout = useCallback(async () => {
    await auth.logout()
    window.location.assign('/login')
  }, [auth])

  const folderMap = useMemo(() => new Map(folders.map((folder) => [folder.id, folder])), [folders])
  const rootSessions = useMemo(
    () => sortedSessions.filter((session) => !session.folder_id || !folderMap.has(session.folder_id)),
    [folderMap, sortedSessions],
  )
  // Folders keep their persisted manual order (UDR-0046 D1/D6); `folders`
  // already arrives sorted by `order` ascending from CTR-0015.
  const folderGroups = useMemo(
    () =>
      folders.map((folder) => ({
        folder,
        sessions: sortedSessions.filter((session) => session.folder_id === folder.id),
      })),
    [folders, sortedSessions],
  )
  const deleteFolderSessionCount = useMemo(
    () => folderGroups.find((group) => group.folder.id === deleteFolderTarget?.id)?.sessions.length ?? 0,
    [deleteFolderTarget?.id, folderGroups],
  )

  // Keep the color modal's preview in sync with the live folder record.
  const colorTargetLive = useMemo(
    () => (colorTarget ? (folderMap.get(colorTarget.id) ?? colorTarget) : null),
    [colorTarget, folderMap],
  )

  useEffect(() => {
    if (renamingId) renameRef.current?.focus()
  }, [renamingId])

  useEffect(() => {
    if (createFolderOpen) folderNameRef.current?.focus()
  }, [createFolderOpen])

  // Persist the expanded set to localStorage (per device, UDR-0046 D4).
  useEffect(() => {
    try {
      localStorage.setItem(FOLDER_EXPANDED_STORAGE_KEY, JSON.stringify([...expandedFolderIds]))
    } catch {
      // ignore storage failures (private mode / quota)
    }
  }, [expandedFolderIds])

  // Auto-expand the active session's folder so the current chat is always visible.
  useEffect(() => {
    const activeFolderId = sessions.find((session) => session.thread_id === currentThreadId)?.folder_id
    if (!activeFolderId) return
    setExpandedFolderIds((prev) => {
      if (prev.has(activeFolderId)) return prev
      const next = new Set(prev)
      next.add(activeFolderId)
      return next
    })
  }, [currentThreadId, sessions])

  const startRename = useCallback((session: SessionSummary) => {
    setRenamingId(session.thread_id)
    setRenameValue(session.title || '')
  }, [])

  const commitRename = useCallback(() => {
    if (!renamingId) return
    const trimmed = renameValue.trim()
    if (trimmed) {
      onRename(renamingId, trimmed)
    }
    setRenamingId(null)
    setRenameValue('')
  }, [renamingId, renameValue, onRename])

  const cancelRename = useCallback(() => {
    setRenamingId(null)
    setRenameValue('')
  }, [])

  const handleRenameKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      // Skip while an IME composition is in progress (CJK conversion).
      if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
        e.preventDefault()
        commitRename()
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        cancelRename()
      }
    },
    [commitRename, cancelRename],
  )

  const handleCreateFolder = useCallback(async () => {
    const created = await onCreateFolder(folderName, newFolderColor)
    if (!created) return
    setCreateFolderOpen(false)
    setFolderName('')
    setNewFolderColor(DEFAULT_FOLDER_COLOR)
  }, [folderName, newFolderColor, onCreateFolder])

  const handleCreateFolderKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      // Skip while an IME composition is in progress (CJK conversion).
      if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
        e.preventDefault()
        void handleCreateFolder()
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setCreateFolderOpen(false)
        setFolderName('')
      }
    },
    [handleCreateFolder],
  )

  const toggleFolder = useCallback((folderId: string) => {
    setExpandedFolderIds((prev) => {
      const next = new Set(prev)
      if (next.has(folderId)) next.delete(folderId)
      else next.add(folderId)
      return next
    })
  }, [])

  const handleFolderDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event
      if (!over || active.id === over.id) return
      const ids = folderGroups.map((group) => group.folder.id)
      const oldIndex = ids.indexOf(active.id as string)
      const newIndex = ids.indexOf(over.id as string)
      if (oldIndex < 0 || newIndex < 0) return
      void onReorderFolders(arrayMove(ids, oldIndex, newIndex))
    },
    [folderGroups, onReorderFolders],
  )

  const handleColorSelect = useCallback(
    async (color: FolderColor) => {
      if (!colorTarget) return
      const ok = await onUpdateFolderColor(colorTarget.id, color)
      if (ok) setColorTarget(null)
    },
    [colorTarget, onUpdateFolderColor],
  )

  const handleSessionDragStart = useCallback((threadId: string) => {
    setDraggedSessionId(threadId)
  }, [])

  const handleSessionDragEnd = useCallback(() => {
    setDraggedSessionId(null)
    setDropFolderId(null)
  }, [])

  const handleDragOverFolder = useCallback((folderId: string) => setDropFolderId(folderId), [])
  const handleDragLeaveFolder = useCallback(
    (folderId: string) => setDropFolderId((prev) => (prev === folderId ? null : prev)),
    [],
  )
  const handleDropSession = useCallback(
    (folderId: string) => {
      setDropFolderId(null)
      const sessionId = draggedSessionId
      setDraggedSessionId(null)
      if (sessionId) void onMoveToFolder(sessionId, folderId)
    },
    [draggedSessionId, onMoveToFolder],
  )

  const renderSessionRow = useCallback(
    (session: SessionSummary, nested = false) => {
      const isActive = session.thread_id === currentThreadId
      const isRenaming = renamingId === session.thread_id
      const availableFolders = folders.filter((folder) => folder.id !== session.folder_id)

      return (
        // biome-ignore lint/a11y/noStaticElementInteractions: drag-and-drop requires drag events on the row container
        <div
          key={session.thread_id}
          className={cn(
            // PRP-0055 follow-up: transparent baseline left-border on every row
            // keeps layout stable; the active row swaps it to primary for a clear
            // affordance, combined with the stronger bg-accent fill.
            'group flex w-full items-start gap-2 border-b border-l-2 border-l-transparent border-border/30 px-3 py-2.5 transition-colors hover:bg-muted/50',
            nested && 'border-b-0 bg-background/50 pl-9',
            isActive && 'border-l-primary bg-accent hover:bg-accent',
          )}
          draggable={!isRenaming}
          onDragStart={(event) => {
            if (isRenaming) {
              event.preventDefault()
              return
            }
            event.dataTransfer.effectAllowed = 'move'
            event.dataTransfer.setData('text/plain', session.thread_id)
            handleSessionDragStart(session.thread_id)
          }}
          onDragEnd={handleSessionDragEnd}>
          <button
            type="button"
            className="min-w-0 flex-1 cursor-pointer text-left"
            onClick={() => !isRenaming && onSwitch(session.thread_id)}>
            {isRenaming ? (
              <Input
                ref={renameRef}
                type="text"
                value={renameValue}
                onChange={(e) => setRenameValue(e.target.value)}
                onKeyDown={handleRenameKeyDown}
                onBlur={commitRename}
                className="h-8"
              />
            ) : (
              <>
                <p className="flex items-center gap-1 truncate text-sm">
                  {session.pinned_at && <Pin className="h-3 w-3 shrink-0 text-muted-foreground" />}
                  <span className="truncate">{session.title || 'New session'}</span>
                  {/* Auto Session Title in progress (PRP-0077, CTR-0109): a small
                      spinner until the background title task finalizes (cleared by
                      the CTR-0110 push). */}
                  {session.auto_title_pending && (
                    <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
                  )}
                  {movingSessionId === session.thread_id && <Loader2 className="h-3 w-3 shrink-0 animate-spin" />}
                </p>
                <p className="flex items-center gap-1 text-xs text-muted-foreground">
                  <span className="truncate">{getSessionMeta(session)}</span>
                  {session.source === 'openai-api' && (
                    <span className="inline-block shrink-0 rounded bg-blue-100 px-1 py-0.5 text-[10px] font-medium leading-none text-blue-700 dark:bg-blue-900 dark:text-blue-300">
                      API
                    </span>
                  )}
                </p>
              </>
            )}
          </button>
          {!isRenaming && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                {/* biome-ignore lint/a11y/useSemanticElements: nested interactive, span is intentional */}
                <span
                  role="button"
                  tabIndex={-1}
                  className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                  aria-label="Session options">
                  <MoreHorizontal className="h-3 w-3" />
                </span>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                <DropdownMenuItem onClick={() => onPin(session.thread_id, !session.pinned_at)}>
                  {session.pinned_at ? (
                    <>
                      <PinOff className="mr-2 h-3.5 w-3.5" />
                      Unpin
                    </>
                  ) : (
                    <>
                      <Pin className="mr-2 h-3.5 w-3.5" />
                      Pin
                    </>
                  )}
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => startRename(session)}>
                  <Pencil className="mr-2 h-3.5 w-3.5" />
                  Rename
                </DropdownMenuItem>
                {folders.length > 0 ? (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger>
                      <FolderPlus className="mr-2 h-3.5 w-3.5" />
                      Move to folder
                    </DropdownMenuSubTrigger>
                    <DropdownMenuSubContent className="w-44">
                      {availableFolders.length > 0 ? (
                        availableFolders.map((folder) => (
                          <DropdownMenuItem
                            key={folder.id}
                            disabled={movingSessionId === session.thread_id}
                            onClick={() => void onMoveToFolder(session.thread_id, folder.id)}>
                            <Folder
                              className={cn(
                                'mr-2 h-3.5 w-3.5',
                                (FOLDER_COLOR_CLASSES[folder.color] ?? FOLDER_COLOR_CLASSES[DEFAULT_FOLDER_COLOR]).icon,
                              )}
                            />
                            {folder.name}
                          </DropdownMenuItem>
                        ))
                      ) : (
                        <DropdownMenuItem disabled>No other folders</DropdownMenuItem>
                      )}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                ) : (
                  <DropdownMenuItem disabled>
                    <FolderPlus className="mr-2 h-3.5 w-3.5" />
                    No folders yet
                  </DropdownMenuItem>
                )}
                {session.folder_id && (
                  <DropdownMenuItem
                    disabled={movingSessionId === session.thread_id}
                    onClick={() => void onMoveToFolder(session.thread_id, null)}>
                    <FolderOpen className="mr-2 h-3.5 w-3.5" />
                    Remove from folder
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onClick={() => onArchive(session.thread_id)}>
                  <Archive className="mr-2 h-3.5 w-3.5" />
                  Archive
                </DropdownMenuItem>
                {/* Session Export (PRP-0084, CTR-0016 v4): download this chat as
                    a self-contained ZIP bundle (session JSON + its uploads). */}
                <DropdownMenuItem onClick={() => onExport(session.thread_id)}>
                  <Download className="mr-2 h-3.5 w-3.5" />
                  Export
                </DropdownMenuItem>
                <DropdownMenuItem
                  className="text-destructive focus:text-destructive"
                  onClick={() => setDeleteTarget(session)}>
                  <Trash2 className="mr-2 h-3.5 w-3.5" />
                  Delete
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      )
    },
    [
      commitRename,
      currentThreadId,
      folders,
      handleRenameKeyDown,
      handleSessionDragEnd,
      handleSessionDragStart,
      movingSessionId,
      onArchive,
      onExport,
      onMoveToFolder,
      onPin,
      onSwitch,
      renameValue,
      renamingId,
      startRename,
    ],
  )

  return (
    <aside className="flex h-full w-[307px] shrink-0 flex-col border-r bg-muted/30">
      <div className="flex h-12 shrink-0 items-center justify-between border-b px-3">
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="ChatWalaʻau" className="h-5 w-5" />
          <span className="text-sm font-medium">ChatWalaʻau</span>
          {auth.demoMode && (
            <span
              className="rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-300"
              title="Demo Mode: every LLM / TTS / STT / image / embedding call is scripted. Open-Meteo (Weather) is the only live external API.">
              Demo
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => setSearchOpen(true)}
            aria-label="Search sessions">
            <Search className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onCreate} aria-label="New session">
            <Plus className="h-4 w-4" />
          </Button>
          {showLogout && (
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={handleLogout}
              aria-label="Sign out"
              title={auth.username ? `Sign out (${auth.username})` : 'Sign out'}>
              <LogOut className="h-4 w-4" />
            </Button>
          )}
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onClose} aria-label="Close sidebar">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {auth.toolApprovalMode === 'skip' && (
        <div className="border-b px-3 pt-3">
          <PermissionsDisabledBanner />
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        <section className="border-b border-border/50 py-2">
          <div className="flex items-center justify-between px-3 pb-1">
            <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
              <FolderOpen className="h-3.5 w-3.5" />
              Folders
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => setCreateFolderOpen(true)}
              aria-label="Create folder"
              disabled={creatingFolder}>
              {creatingFolder ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderPlus className="h-4 w-4" />}
            </Button>
          </div>
          {folderGroups.length === 0 && <div className="px-3 py-2 text-xs text-muted-foreground">No folders yet</div>}
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleFolderDragEnd}>
            <SortableContext
              items={folderGroups.map((group) => group.folder.id)}
              strategy={verticalListSortingStrategy}>
              {folderGroups.map(({ folder, sessions: groupedSessions }) => (
                <FolderGroup
                  key={folder.id}
                  folder={folder}
                  groupedSessions={groupedSessions}
                  isCollapsed={!expandedFolderIds.has(folder.id)}
                  isDropTarget={dropFolderId === folder.id}
                  deletingFolderId={deletingFolderId}
                  updatingFolderId={updatingFolderId}
                  draggedSessionId={draggedSessionId}
                  onToggle={toggleFolder}
                  onDragOverFolder={handleDragOverFolder}
                  onDragLeaveFolder={handleDragLeaveFolder}
                  onDropSession={handleDropSession}
                  onOpenColor={setColorTarget}
                  onDeleteFolder={setDeleteFolderTarget}
                  renderSessionRow={renderSessionRow}
                />
              ))}
            </SortableContext>
          </DndContext>
        </section>

        <section className="py-2">
          <div className="flex items-center justify-between px-3 pb-1">
            <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
              <Plus className="h-3.5 w-3.5" />
              Chats
            </div>
            {/* Session Import (PRP-0084, CTR-0016 v4): upload a ZIP bundle as a
                new chat. The animated indicator shows until import completes,
                after which useSession refreshes the list and selects it. */}
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => importInputRef.current?.click()}
              aria-label="Import chat"
              title="Import chat from a .zip bundle"
              disabled={importing}>
              {importing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            </Button>
            <input
              ref={importInputRef}
              type="file"
              accept=".zip,application/zip"
              className="hidden"
              onChange={handleImportChange}
            />
          </div>
          {rootSessions.length === 0 ? (
            <div className="px-3 py-2 text-xs text-muted-foreground">
              {folderGroups.length > 0 ? 'No root chats' : 'No sessions yet'}
            </div>
          ) : (
            rootSessions.map((session) => renderSessionRow(session))
          )}
        </section>
      </div>

      {/* App info footer (CTR-0101, FEAT-0029): version label + About button */}
      <div className="flex h-9 shrink-0 items-center justify-between border-t px-3">
        <span className="text-[11px] text-muted-foreground">{auth.version ? `v${auth.version}` : ''}</span>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-muted-foreground"
          onClick={() => setAboutOpen(true)}
          aria-label="About ChatWalaʻau">
          <Info className="h-4 w-4" />
        </Button>
      </div>

      <AlertDialog open={deleteTarget !== null} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete session?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{deleteTarget?.title || 'New session'}&quot; will be permanently deleted. This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (deleteTarget) {
                  onDelete(deleteTarget.thread_id)
                  setDeleteTarget(null)
                }
              }}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteFolderTarget !== null} onOpenChange={(open) => !open && setDeleteFolderTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete folder?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{deleteFolderTarget?.name || 'Folder'}&quot; will be removed. The {deleteFolderSessionCount} session
              {deleteFolderSessionCount === 1 ? '' : 's'} inside it will stay intact and return to the Chats section.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={!!deletingFolderId}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={!deleteFolderTarget || !!deletingFolderId}
              onClick={async () => {
                if (!deleteFolderTarget) return
                const deleted = await onDeleteFolder(deleteFolderTarget.id)
                if (deleted) setDeleteFolderTarget(null)
              }}>
              {deletingFolderId ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Deleting...
                </>
              ) : (
                'Delete folder'
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog
        open={createFolderOpen}
        onOpenChange={(open) => {
          setCreateFolderOpen(open)
          if (!open) {
            setFolderName('')
            setNewFolderColor(DEFAULT_FOLDER_COLOR)
          }
        }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create folder</DialogTitle>
            <DialogDescription>Create a sidebar folder for related chats.</DialogDescription>
          </DialogHeader>
          <Input
            ref={folderNameRef}
            value={folderName}
            onChange={(e) => setFolderName(e.target.value)}
            onKeyDown={handleCreateFolderKeyDown}
            placeholder="Folder name"
            maxLength={100}
          />
          <div className="space-y-2">
            <span className="text-xs font-medium text-muted-foreground">Color</span>
            <PaletteSwatches value={newFolderColor} onSelect={setNewFolderColor} disabled={creatingFolder} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateFolderOpen(false)} disabled={creatingFolder}>
              Cancel
            </Button>
            <Button onClick={() => void handleCreateFolder()} disabled={creatingFolder || !folderName.trim()}>
              {creatingFolder ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Creating...
                </>
              ) : (
                'Create folder'
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={colorTarget !== null} onOpenChange={(open) => !open && setColorTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Folder color</DialogTitle>
            <DialogDescription>Pick a color for &quot;{colorTargetLive?.name || 'Folder'}&quot;.</DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-3 py-2">
            <PaletteSwatches
              value={colorTargetLive?.color ?? DEFAULT_FOLDER_COLOR}
              onSelect={(color) => void handleColorSelect(color)}
              disabled={updatingFolderId === colorTarget?.id}
            />
            {updatingFolderId === colorTarget?.id && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setColorTarget(null)}
              disabled={updatingFolderId === colorTarget?.id}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <SessionSearchDialog sessions={sessions} open={searchOpen} onOpenChange={setSearchOpen} onSelect={onSwitch} />

      <AboutDialog open={aboutOpen} onOpenChange={setAboutOpen} version={auth.version} />
    </aside>
  )
}
