import { Loader2, Menu } from 'lucide-react'
import { Suspense, useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChatPanel } from '@/components/ChatPanel'
import { CronManager } from '@/components/CronManager'
import { PipelineManager } from '@/components/PipelineManager'
import { SessionSidebar } from '@/components/SessionSidebar'
import { TemporaryChatToggle } from '@/components/TemporaryChatToggle'
import { Button } from '@/components/ui/button'
import { WebhookManager } from '@/components/WebhookManager'
import { useCronAvailable } from '@/hooks/useCronAvailable'
import { useFileExplorerAvailable } from '@/hooks/useFileExplorerAvailable'
import { useOntologyAvailable } from '@/hooks/useOntologyAvailable'
import { usePipelineAvailable } from '@/hooks/usePipelineAvailable'
import { useSession } from '@/hooks/useSession'
import { useTemporaryChat } from '@/hooks/useTemporaryChat'
import { useWebhookAvailable } from '@/hooks/useWebhookAvailable'
import { lazyWithReload } from '@/lib/lazy-with-reload'

// Lazy-loaded so the heavy monaco-editor bundle is fetched only when the File
// Explorer is enabled and first opened (CTR-0137, UDR-0069 D5). lazyWithReload recovers
// from a stale chunk hash after a rebuild/redeploy (PRP-0097 fix) instead of throwing
// "Failed to fetch dynamically imported module".
const FileExplorer = lazyWithReload(() =>
  import('@/components/FileExplorer').then((m) => ({ default: m.FileExplorer })),
)

// Memory Management portal (CTR-0167, PRP-0101). Lazy-loaded because it reuses the
// heavy monaco editor bundle; opened from the sidebar-footer Brain icon.
const MemoryManager = lazyWithReload(() =>
  import('@/components/MemoryManager').then((m) => ({ default: m.MemoryManager })),
)

// Ontology Manager portal (CTR-0173, PRP-0105). Lazy-loaded because it pulls in the
// React Flow + elkjs + monaco bundles; opened from the sidebar-footer Network icon.
const OntologyManager = lazyWithReload(() =>
  import('@/components/OntologyManager').then((m) => ({ default: m.OntologyManager })),
)

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
    // Session list pagination (PRP-0112 Part 4, CTR-0016 v6).
    loadMoreSessions,
    loadFolderSessions,
    isLoadingMoreSessions,
    hasMoreSessions,
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

  // Pipeline Jobs portal (CTR-0148, PRP-0096). Lifted here so the sidebar-footer
  // launcher icon (next to Declarative Agents) opens the same modal instance.
  const pipelineAvailable = usePipelineAvailable()
  const [pipelineOpen, setPipelineOpen] = useState(false)

  // Webhook Gateway portal (CTR-0157, PRP-0097). Lifted here so the sidebar-footer
  // launcher icon (next to Declarative Agents) opens the same modal instance.
  const webhookAvailable = useWebhookAvailable()
  const [webhookOpen, setWebhookOpen] = useState(false)

  // File Explorer overlay (CTR-0137, PRP-0091). Lifted here so both the sidebar-footer
  // launcher icon and the /files slash command open the same overlay instance.
  const fileExplorerAvailable = useFileExplorerAvailable()
  const [filesOpen, setFilesOpen] = useState(false)
  // Bridge a File Explorer image/PDF attach into the composer (PRP-0116, CTR-0137).
  // The File is handed up here; ChatPanel (which owns the thread id) consumes it.
  const [attachFile, setAttachFile] = useState<File | null>(null)

  // Memory Management portal (CTR-0167, PRP-0101). Lifted here so the sidebar-footer
  // launcher icon opens the modal. Always available (identity always exists).
  const [memoryOpen, setMemoryOpen] = useState(false)

  // Ontology Manager portal (CTR-0173, PRP-0105). Lifted here so the sidebar-footer
  // launcher icon (next to Declarative Agents) opens the same overlay instance.
  const ontologyAvailable = useOntologyAvailable()
  const [ontologyOpen, setOntologyOpen] = useState(false)

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
          hasMoreSessions={hasMoreSessions}
          isLoadingMoreSessions={isLoadingMoreSessions}
          onLoadMoreSessions={loadMoreSessions}
          onLoadFolderSessions={loadFolderSessions}
          cronAvailable={cronAvailable}
          onOpenCron={() => setCronOpen(true)}
          fileExplorerAvailable={fileExplorerAvailable}
          onOpenFiles={() => setFilesOpen(true)}
          pipelineAvailable={pipelineAvailable}
          onOpenPipeline={() => setPipelineOpen(true)}
          webhookAvailable={webhookAvailable}
          onOpenWebhook={() => setWebhookOpen(true)}
          onOpenMemory={() => setMemoryOpen(true)}
          ontologyAvailable={ontologyAvailable}
          onOpenOntology={() => setOntologyOpen(true)}
        />
      )}

      {cronAvailable && <CronManager open={cronOpen} onOpenChange={setCronOpen} />}

      {pipelineAvailable && <PipelineManager open={pipelineOpen} onOpenChange={setPipelineOpen} />}

      {webhookAvailable && <WebhookManager open={webhookOpen} onOpenChange={setWebhookOpen} />}

      {fileExplorerAvailable && (
        <Suspense fallback={null}>
          <FileExplorer open={filesOpen} onOpenChange={setFilesOpen} onAttach={setAttachFile} />
        </Suspense>
      )}

      {memoryOpen && (
        <Suspense fallback={null}>
          <MemoryManager open={memoryOpen} onOpenChange={setMemoryOpen} />
        </Suspense>
      )}

      {ontologyAvailable && ontologyOpen && (
        <Suspense fallback={null}>
          <OntologyManager open={ontologyOpen} onOpenChange={setOntologyOpen} />
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
            attachFile={attachFile}
            onAttachConsumed={() => setAttachFile(null)}
            temporary={temp.isTemporary}
          />
        )}
      </div>
    </div>
  )
}
