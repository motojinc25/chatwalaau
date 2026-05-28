import {
  Archive,
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  FolderPlus,
  Loader2,
  LogOut,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import type { SessionFolder, SessionSummary } from '@/types/chat'

interface SessionSidebarProps {
  sessions: SessionSummary[]
  folders: SessionFolder[]
  currentThreadId: string
  creatingFolder: boolean
  deletingFolderId: string | null
  movingSessionId: string | null
  onSwitch: (threadId: string) => void
  onDelete: (threadId: string) => void
  onDeleteFolder: (folderId: string) => Promise<boolean>
  onCreateFolder: (name: string) => Promise<boolean>
  onMoveToFolder: (threadId: string, folderId: string | null) => Promise<boolean>
  onRename: (threadId: string, title: string) => void
  onArchive: (threadId: string) => void
  onPin: (threadId: string, pinned: boolean) => void
  onCreate: () => void
  onClose: () => void
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

export function SessionSidebar({
  sessions,
  folders,
  currentThreadId,
  creatingFolder,
  deletingFolderId,
  movingSessionId,
  onSwitch,
  onDelete,
  onDeleteFolder,
  onCreateFolder,
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
  const [searchOpen, setSearchOpen] = useState(false)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [createFolderOpen, setCreateFolderOpen] = useState(false)
  const [folderName, setFolderName] = useState('')
  const [collapsedFolderIds, setCollapsedFolderIds] = useState<Set<string>>(() => new Set())
  const [draggedSessionId, setDraggedSessionId] = useState<string | null>(null)
  const [dropFolderId, setDropFolderId] = useState<string | null>(null)
  const renameRef = useRef<HTMLInputElement>(null)
  const folderNameRef = useRef<HTMLInputElement>(null)

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
  const folderGroups = useMemo(() => {
    return folders
      .map((folder) => ({
        folder,
        sessions: sortedSessions.filter((session) => session.folder_id === folder.id),
      }))
      .sort((a, b) => {
        if (a.sessions.length === 0 && b.sessions.length === 0) return a.folder.name.localeCompare(b.folder.name)
        if (a.sessions.length === 0) return 1
        if (b.sessions.length === 0) return -1
        return a.sessions[0].updated_at > b.sessions[0].updated_at ? -1 : 1
      })
  }, [folders, sortedSessions])
  const deleteFolderSessionCount = useMemo(
    () => folderGroups.find((group) => group.folder.id === deleteFolderTarget?.id)?.sessions.length ?? 0,
    [deleteFolderTarget?.id, folderGroups],
  )

  useEffect(() => {
    if (renamingId) renameRef.current?.focus()
  }, [renamingId])

  useEffect(() => {
    if (createFolderOpen) folderNameRef.current?.focus()
  }, [createFolderOpen])

  useEffect(() => {
    const activeFolderId = sessions.find((session) => session.thread_id === currentThreadId)?.folder_id
    if (!activeFolderId) return
    setCollapsedFolderIds((prev) => {
      if (!prev.has(activeFolderId)) return prev
      const next = new Set(prev)
      next.delete(activeFolderId)
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
    const created = await onCreateFolder(folderName)
    if (!created) return
    setCreateFolderOpen(false)
    setFolderName('')
  }, [folderName, onCreateFolder])

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
    setCollapsedFolderIds((prev) => {
      const next = new Set(prev)
      if (next.has(folderId)) next.delete(folderId)
      else next.add(folderId)
      return next
    })
  }, [])

  const handleSessionDragStart = useCallback((threadId: string) => {
    setDraggedSessionId(threadId)
  }, [])

  const handleSessionDragEnd = useCallback(() => {
    setDraggedSessionId(null)
    setDropFolderId(null)
  }, [])

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
                  {session.title || 'New session'}
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
                            <Folder className="mr-2 h-3.5 w-3.5" />
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
          {folderGroups.map(({ folder, sessions: groupedSessions }) => {
            const isCollapsed = collapsedFolderIds.has(folder.id)
            const isDropTarget = dropFolderId === folder.id

            return (
              <div key={folder.id} className="border-t border-border/20 first:border-t-0">
                {/* biome-ignore lint/a11y/noStaticElementInteractions: folder row is a drag target for native DnD */}
                <div
                  className={cn(
                    'group flex items-center gap-2 px-3 py-2 transition-colors',
                    isDropTarget && 'bg-accent/80',
                  )}
                  onDragOver={(event) => {
                    if (!draggedSessionId) return
                    event.preventDefault()
                    event.dataTransfer.dropEffect = 'move'
                    setDropFolderId(folder.id)
                  }}
                  onDragLeave={() => {
                    if (dropFolderId === folder.id) setDropFolderId(null)
                  }}
                  onDrop={(event) => {
                    if (!draggedSessionId) return
                    event.preventDefault()
                    setDropFolderId(null)
                    setDraggedSessionId(null)
                    void onMoveToFolder(draggedSessionId, folder.id)
                  }}>
                  <button
                    type="button"
                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                    onClick={() => toggleFolder(folder.id)}>
                    {isCollapsed ? (
                      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    ) : (
                      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    )}
                    <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
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
                        {deletingFolderId === folder.id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <MoreHorizontal className="h-3 w-3" />
                        )}
                      </span>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-40">
                      <DropdownMenuItem
                        className="text-destructive focus:text-destructive"
                        disabled={deletingFolderId === folder.id}
                        onClick={() => setDeleteFolderTarget(folder)}>
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
          })}
        </section>

        <section className="py-2">
          <div className="flex items-center gap-2 px-3 pb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            <Plus className="h-3.5 w-3.5" />
            Chats
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
          if (!open) setFolderName('')
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

      <SessionSearchDialog sessions={sessions} open={searchOpen} onOpenChange={setSearchOpen} onSelect={onSwitch} />
    </aside>
  )
}
