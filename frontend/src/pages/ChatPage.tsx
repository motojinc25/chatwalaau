import { Loader2, Menu } from 'lucide-react'
import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChatPanel } from '@/components/ChatPanel'
import { SessionSidebar } from '@/components/SessionSidebar'
import { Button } from '@/components/ui/button'
import { useSession } from '@/hooks/useSession'

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
    movingSessionId,
    createSession,
    createFolder,
    switchSession,
    deleteSession,
    deleteFolder,
    forkSession,
    moveSessionToFolder,
    renameSession,
    archiveSession,
    pinSession,
    refreshSessions,
  } = useSession()

  const handleStreamComplete = useCallback(() => {
    refreshSessions()
    navigate(`/chat?session=${threadId}`, { replace: true })
  }, [refreshSessions, navigate, threadId])

  const handleBranch = useCallback(
    (messageIndex: number) => {
      forkSession(threadId, messageIndex)
    },
    [forkSession, threadId],
  )

  return (
    <div className="flex h-screen">
      {sidebarOpen && (
        <SessionSidebar
          sessions={sessions}
          folders={folders}
          currentThreadId={threadId}
          creatingFolder={isCreatingFolder}
          deletingFolderId={deletingFolderId}
          movingSessionId={movingSessionId}
          onSwitch={switchSession}
          onDelete={deleteSession}
          onDeleteFolder={deleteFolder}
          onCreateFolder={createFolder}
          onMoveToFolder={moveSessionToFolder}
          onRename={renameSession}
          onArchive={archiveSession}
          onPin={pinSession}
          onCreate={createSession}
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

        {isSwitching ? (
          <div className="flex flex-1 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            <span className="ml-2 text-sm text-muted-foreground">Loading session...</span>
          </div>
        ) : (
          <ChatPanel
            key={threadId}
            threadId={threadId}
            initialMessages={initialMessages}
            continuationToken={continuationToken}
            onStreamComplete={handleStreamComplete}
            onBranchFromMessage={handleBranch}
          />
        )}
      </div>
    </div>
  )
}
