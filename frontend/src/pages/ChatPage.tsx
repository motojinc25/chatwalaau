import { Loader2, Menu } from 'lucide-react'
import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChatPanel } from '@/components/ChatPanel'
import { SessionSidebar } from '@/components/SessionSidebar'
import { TemporaryChatToggle } from '@/components/TemporaryChatToggle'
import { Button } from '@/components/ui/button'
import { useSession } from '@/hooks/useSession'
import { useTemporaryChat } from '@/hooks/useTemporaryChat'

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
    createSession,
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

  const handleStreamComplete = useCallback(() => {
    // Temporary chats are never listed and never exposed in the URL (UDR-0052
    // D5): skip the history refresh + ?session= navigation entirely.
    if (temp.isTemporary) return
    refreshSessions()
    navigate(`/chat?session=${threadId}`, { replace: true })
  }, [temp.isTemporary, refreshSessions, navigate, threadId])

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
          onSwitch={handleSwitch}
          onDelete={deleteSession}
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
        />
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
            onBranchFromMessage={temp.isTemporary ? undefined : handleBranch}
            temporary={temp.isTemporary}
          />
        )}
      </div>
    </div>
  )
}
