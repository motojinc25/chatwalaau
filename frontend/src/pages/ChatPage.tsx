import { Loader2, Menu } from 'lucide-react'
import { Suspense, useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChatPanel } from '@/components/ChatPanel'
import { CronManager } from '@/components/CronManager'
import { DeclarativeAgentManager } from '@/components/DeclarativeAgentManager'
import { PipelineManager } from '@/components/PipelineManager'
import { PrivacyScreenToggle } from '@/components/PrivacyScreenToggle'
import { SessionSidebar } from '@/components/SessionSidebar'
import { TemporaryChatToggle } from '@/components/TemporaryChatToggle'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { WebhookManager } from '@/components/WebhookManager'
import { type WorkspaceLinkContextValue, WorkspaceLinkProvider } from '@/components/WorkspaceFileLink'
import { useCronAvailable } from '@/hooks/useCronAvailable'
import { useFileExplorerProbe } from '@/hooks/useFileExplorerAvailable'
import { useOntologyAvailable } from '@/hooks/useOntologyAvailable'
import { usePipelineAvailable } from '@/hooks/usePipelineAvailable'
import { useSession } from '@/hooks/useSession'
import { useTemporaryChat } from '@/hooks/useTemporaryChat'
import { useViewportTier } from '@/hooks/useViewportTier'
import { useWebhookAvailable } from '@/hooks/useWebhookAvailable'
import { lazyWithReload } from '@/lib/lazy-with-reload'
import { type ChatSurfaceTier, ChatSurfaceTierContext, isEntryVisible } from '@/lib/narrowSurface'
import { cn } from '@/lib/utils'

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
    renameFolder,
    updateFolderColor,
    reorderFolders,
    forkSession,
    moveSessionToFolder,
    renameSession,
    regenerateTitle,
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

  // Chat surface tier (PRP-0171, UDR-0153 D1/D2). This page is the ONLY provider, so
  // the compact ChatPanel on /popup and /sidebar keeps the wide default untouched.
  const { narrow, touchPrimary } = useViewportTier()
  const surface = useMemo<ChatSurfaceTier>(() => ({ managed: true, narrow, touchPrimary }), [narrow, touchPrimary])

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
  const fileExplorerProbe = useFileExplorerProbe()
  const fileExplorerAvailable = fileExplorerProbe === true
  const [filesOpen, setFilesOpen] = useState(false)
  // Workspace file references in chat (CTR-0207, PRP-0166, UDR-0150 D8): "Open" on a
  // PDF / image reference asks the File Explorer to open that path in its existing
  // viewer. The nonce makes a repeat request for the same path observable.
  const [fileOpenRequest, setFileOpenRequest] = useState<{ path: string; nonce: number } | null>(null)
  const workspaceLinks = useMemo<WorkspaceLinkContextValue>(
    () => ({
      available: fileExplorerProbe,
      // Narrow viewport: no "Open" (the File Explorer is wide-only); Download stays.
      onOpen:
        fileExplorerAvailable && isEntryVisible(surface, 'fileLink.open')
          ? (path: string) => setFileOpenRequest((prev) => ({ path, nonce: (prev?.nonce ?? 0) + 1 }))
          : undefined,
    }),
    [fileExplorerProbe, fileExplorerAvailable, surface],
  )
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

  const sessionSidebar = (
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
      onRenameFolder={renameFolder}
      onUpdateFolderColor={updateFolderColor}
      onReorderFolders={reorderFolders}
      onMoveToFolder={moveSessionToFolder}
      onRename={renameSession}
      onRegenerateTitle={regenerateTitle}
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
  )

  const topRightToggles = (
    <>
      <PrivacyScreenToggle />
      <TemporaryChatToggle isTemporary={temp.isTemporary} onEnter={temp.enter} onExit={temp.exit} />
    </>
  )

  return (
    <ChatSurfaceTierContext.Provider value={surface}>
      <WorkspaceLinkProvider value={workspaceLinks}>
        <div className={cn('flex', narrow ? 'h-dvh' : 'h-screen')}>
          {narrow ? (
            // Narrow viewport: the sidebar is an overlay drawer (PRP-0171, CTR-0016).
            <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
              <SheetContent title="Chats" className="w-[85vw] max-w-[307px]">
                {sessionSidebar}
              </SheetContent>
            </Sheet>
          ) : (
            sidebarOpen && sessionSidebar
          )}

          {cronAvailable && <CronManager open={cronOpen} onOpenChange={setCronOpen} />}

          {pipelineAvailable && <PipelineManager open={pipelineOpen} onOpenChange={setPipelineOpen} />}

          {webhookAvailable && <WebhookManager open={webhookOpen} onOpenChange={setWebhookOpen} />}

          {fileExplorerAvailable && (
            <Suspense fallback={null}>
              <FileExplorer
                open={filesOpen}
                onOpenChange={setFilesOpen}
                onAttach={setAttachFile}
                openRequest={fileOpenRequest}
              />
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

          <div className={cn('relative flex flex-1 flex-col', narrow && 'min-h-0 min-w-0')}>
            {narrow ? (
              // Narrow viewport: one header row instead of controls floating over the
              // first message (PRP-0171, CTR-0004). The toggles are unchanged components.
              <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b px-2">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-10 w-10"
                  onClick={() => setSidebarOpen(true)}
                  aria-label="Open sessions">
                  <Menu className="h-5 w-5" />
                </Button>
                <div className="flex items-center gap-1">{topRightToggles}</div>
              </div>
            ) : (
              <>
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

                {/*
              Top-right controls. Privacy Screen (CTR-0190, PRP-0124) sits to the LEFT
              of Temporary Chat (CTR-0107, PRP-0076); both are the full-page /chat
              surface only, and both may be active at once (their active pills use
              distinct colors so they stay distinguishable -- UDR-0107 D11).
            */}
                <div className="absolute right-3 top-3 z-20 flex items-center gap-1">{topRightToggles}</div>
              </>
            )}

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
                // Narrow viewport: /cron and /files are consumed and open nothing (UDR-0153 D4).
                onSlashCron={isEntryVisible(surface, 'slash.cron') ? () => setCronOpen(true) : undefined}
                onSlashFiles={isEntryVisible(surface, 'slash.files') ? () => setFilesOpen(true) : undefined}
                attachFile={attachFile}
                onAttachConsumed={() => setAttachFile(null)}
                temporary={temp.isTemporary}
              />
            )}
          </div>

          {/* Declarative Agents & Workflows modal + its open-request listener (CTR-0144).
          Mounted HERE, outside every conditional, because the request arrives on a
          window event (UDR-0111 D6) and a window event has no failure signal: while
          this lived inside the collapsible sidebar, closing the sidebar deleted the
          listener and the composer's run-target button silently did nothing
          (PRP-0134 / UDR-0115 D1/D3). Renders null until it is opened or its
          availability probe succeeds, so an unconfigured deployment costs nothing. */}
          <DeclarativeAgentManager />
        </div>
      </WorkspaceLinkProvider>
    </ChatSurfaceTierContext.Provider>
  )
}
