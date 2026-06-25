import { Loader2, Menu } from 'lucide-react'
import { lazy, Suspense, useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChatPanel } from '@/components/ChatPanel'
import { CronManager } from '@/components/CronManager'
import { SessionSidebar } from '@/components/SessionSidebar'
import { TemporaryChatToggle } from '@/components/TemporaryChatToggle'
import { Button } from '@/components/ui/button'
import { useCronAvailable } from '@/hooks/useCronAvailable'
import { useFileExplorerAvailable } from '@/hooks/useFileExplorerAvailable'
import { useSession } from '@/hooks/useSession'
import { useTemporaryChat } from '@/hooks/useTemporaryChat'

// Lazy-loaded so the heavy monaco-editor bundle is fetched only when the File
// Explorer is enabled and first opened (CTR-0137, UDR-0069 D5).
const FileExplorer = lazy(() => import('@/components/FileExplorer').then((m) => ({ default: m.FileExplorer })))

export function ChatPage() {
  const navigate = useNavigate()
  const {
    threadId,
    sessions,
    folders,
    initialMessages,
    continuationToken,
    isSwitching,
    sidebarOpen,
    setSidebarOpen,
    isCreatingFolder,
    deletingFolderId,
    updatingFolderId,
    movingSessionId,
    isImporting,
    createSession,
    exportSession,
    importSession,
    createFolder,
    switchSession,
    deleteSession,
    deleteFolder,
    updateFolderColor,
    reorderFolders,
    forkSession,
    moveSessionToFolder,
    renameSession,
    archiveSession,
    pinSession,
    refreshSessions,
  } = useSession()

  // Temporary Chat (CTR-0107, PRP-0076). When active, the panel runs against a
  // fresh temp_ thread held in React state only; entering never modifies an
  // existing chat (UDR-0052 D11), and picking a sidebar session / new chat exits
  // temporary first.
  const temp = useTemporaryChat()
  const effectiveThreadId = temp.isTemporary && temp.tempThreadId ? temp.tempThreadId : threadId

  // Cron Scheduler portal (CTR-0135, PRP-0089). State is lifted here so both the
  // sidebar-footer launcher icon and the /cron slash command open the same modal.
  const cronAvailable = useCronAvailable()
  const [cronOpen, setCronOpen] = useState(false)

  // File Explorer overlay (CTR-0137, PRP-0091). Lifted here so both the sidebar-footer
  // launcher icon and the /files slash command open the same overlay instance.
  const fileExplorerAvailable = useFileExplorerAvailable()
  const [filesOpen, setFilesOpen] = useState(false)

  const handleStreamComplete = useCallback(() => {
    // Temporary chats are never listed and never exposed in the URL (UDR-0052
    // D5): skip the history refresh + ?session= navigation entirely.
    if (temp.isTemporary) return
    refreshSessions()
    navigate(`/chat?session=${threadId}`, { replace: true })
  }, [temp.isTemporary, refreshSessions, navigate, threadId])

  // Immediate sidebar entry (PRP-0077, CTR-0016): show a brand-new chat as soon
  // as the first message is sent -- the session was just created server-side
  // (truncate title + pending flag), so a refresh surfaces it instantly without
  // waiting for the AI answer. The LLM title (when SESSION_TITLE_MODE=llm) then
  // arrives in real time via the CTR-0110 WebSocket push (handled in useSession).
  const handleSessionCreated = useCallback(() => {
    if (temp.isTemporary) return
    refreshSessions()
  }, [temp.isTemporary, refreshSessions])

  const handleBranch = useCallback(
    (messageIndex: number) => {
      forkSession(threadId, messageIndex)
    },
    [forkSession, threadId],
  )

  const handleSwitch = useCallback(
    (id: string) => {
      temp.exit()
      switchSession(id)
    },
    [temp, switchSession],
  )

  const handleCreate = useCallback(() => {
    temp.exit()
    createSession()
  }, [temp, createSession])

  return (
    <div className="flex h-screen">
      {sidebarOpen && (
        <SessionSidebar
          sessions={sessions}
          folders={folders}
          currentThreadId={temp.isTemporary ? '' : threadId}
          creatingFolder={isCreatingFolder}
          deletingFolderId={deletingFolderId}
          updatingFolderId={updatingFolderId}
          movingSessionId={movingSessionId}
          importing={isImporting}
          onSwitch={handleSwitch}
          onDelete={deleteSession}
          onExport={exportSession}
          onImport={importSession}
          onDeleteFolder={deleteFolder}
          onCreateFolder={createFolder}
          onUpdateFolderColor={updateFolderColor}
          onReorderFolders={reorderFolders}
          onMoveToFolder={moveSessionToFolder}
          onRename={renameSession}
          onArchive={archiveSession}
          onPin={pinSession}
          onCreate={handleCreate}
          onClose={() => setSidebarOpen(false)}
          cronAvailable={cronAvailable}
          onOpenCron={() => setCronOpen(true)}
          fileExplorerAvailable={fileExplorerAvailable}
          onOpenFiles={() => setFilesOpen(true)}
        />
      )}

      {cronAvailable && <CronManager open={cronOpen} onOpenChange={setCronOpen} />}

      {fileExplorerAvailable && (
        <Suspense fallback={null}>
          <FileExplorer open={filesOpen} onOpenChange={setFilesOpen} />
        </Suspense>
      )}

      <div className="relative flex flex-1 flex-col">
        {!sidebarOpen && (
          <Button
            variant="ghost"
            size="icon"
            className="absolute left-3 top-3 z-10 h-8 w-8"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open sessions">
            <Menu className="h-4 w-4" />
          </Button>
        )}

        {/* Temporary Chat top-right toggle (CTR-0107, PRP-0076). */}
        <div className="absolute right-3 top-3 z-20">
          <TemporaryChatToggle isTemporary={temp.isTemporary} onEnter={temp.enter} onExit={temp.exit} />
        </div>

        {isSwitching ? (
          <div className="flex flex-1 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            <span className="ml-2 text-sm text-muted-foreground">Loading session...</span>
          </div>
        ) : (
          <ChatPanel
            key={effectiveThreadId}
            threadId={effectiveThreadId}
            initialMessages={temp.isTemporary ? [] : initialMessages}
            continuationToken={temp.isTemporary ? null : continuationToken}
            onStreamComplete={handleStreamComplete}
            onSessionCreated={handleSessionCreated}
            onBranchFromMessage={temp.isTemporary ? undefined : handleBranch}
            onSlashCron={() => setCronOpen(true)}
            onSlashFiles={() => setFilesOpen(true)}
            temporary={temp.isTemporary}
          />
        )}
      </div>
    </div>
  )
}
