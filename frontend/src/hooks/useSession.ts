import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  type ActivityEntry,
  type ChatMessage,
  DEFAULT_FOLDER_COLOR,
  FOLDER_COLORS,
  type FolderColor,
  type ImageRef,
  type ImportResult,
  type ReasoningBlock,
  type SessionFolder,
  type SessionSummary,
  type ToolCall,
  type UsageInfo,
} from '@/types/chat'

const STORAGE_KEY = 'chatwalaau-thread-id'

// Session list pagination (CTR-0015, PRP-0112 Part 4 / UDR-0091 D3+D13).
// A frontend constant on purpose: no operator decision depends on the page size,
// so it is deliberately NOT an environment variable.
const SESSION_PAGE_SIZE = 30

// Selects the sessions that belong to no folder. A bare empty `folder_id=` cannot
// express this (indistinguishable from "not supplied"), hence the sentinel; it
// matches ROOT_FOLDER_SENTINEL in app/session/router.py.
const ROOT_FOLDER = '__root__'

// PRP-0055 follow-up: keep the sidebar open after a session pick on
// desktop viewports. On narrow viewports the sidebar is an overlay
// covering most of the chat area, so auto-close is still the right
// behavior there. The project does not yet ship a mobile layout, but
// this gate sets the responsive baseline so the desktop fix does not
// regress a future mobile mode.
const DESKTOP_BREAKPOINT_PX = 768

function shouldAutoCloseSidebar(): boolean {
  if (typeof window === 'undefined') return false
  return !window.matchMedia(`(min-width: ${DESKTOP_BREAKPOINT_PX}px)`).matches
}

function normalizeSessionSummaries(data: unknown): SessionSummary[] {
  if (!Array.isArray(data)) return []
  return data.map((session) => ({
    ...(session as SessionSummary),
    folder_id: (session as SessionSummary).folder_id ?? null,
    auto_title_pending: (session as SessionSummary).auto_title_pending === true,
  }))
}

function normalizeFolderColor(value: unknown): FolderColor {
  return typeof value === 'string' && (FOLDER_COLORS as readonly string[]).includes(value)
    ? (value as FolderColor)
    : DEFAULT_FOLDER_COLOR
}

function normalizeFolder(input: unknown): SessionFolder | null {
  if (!input || typeof input !== 'object') return null
  const candidate = input as Partial<SessionFolder>
  if (typeof candidate.id !== 'string' || typeof candidate.name !== 'string') return null
  return {
    id: candidate.id,
    name: candidate.name,
    color: normalizeFolderColor(candidate.color),
    order: typeof candidate.order === 'number' ? candidate.order : 0,
    created_at: typeof candidate.created_at === 'string' ? candidate.created_at : '',
    updated_at: typeof candidate.updated_at === 'string' ? candidate.updated_at : '',
  }
}

function normalizeFolders(data: unknown): SessionFolder[] {
  const items = Array.isArray(data)
    ? data
    : data && typeof data === 'object' && Array.isArray((data as { folders?: unknown[] }).folders)
      ? (data as { folders: unknown[] }).folders
      : []
  return items.map(normalizeFolder).filter((folder): folder is SessionFolder => folder !== null)
}

/**
 * Convert MAF Message format to ChatMessage for display.
 * MAF stores: { role, contents: [{ type: "text", text: "..." }] }
 * Legacy sessions may use "text_content" / "reasoning_content" type names.
 */
function convertMafMessages(mafMessages: Record<string, unknown>[]): ChatMessage[] {
  const result: ChatMessage[] = []
  for (const msg of mafMessages) {
    const role = msg.role as string
    if (role !== 'user' && role !== 'assistant') continue

    const contents = msg.contents as Record<string, unknown>[] | undefined
    let text = ''
    const reasoningBlocks: ReasoningBlock[] = []
    const images: ImageRef[] = []
    if (contents) {
      for (const c of contents) {
        if ((c.type === 'text' || c.type === 'text_content') && typeof c.text === 'string') {
          text += c.text
        } else if ((c.type === 'text_reasoning' || c.type === 'reasoning_content') && typeof c.text === 'string') {
          reasoningBlocks.push({
            id: (c.id as string) ?? crypto.randomUUID(),
            content: c.text,
            status: 'done',
          })
        } else if (c.type === 'image_url' && typeof c.uri === 'string') {
          images.push({ uri: c.uri as string, media_type: (c.media_type as string) || '' })
        }
      }
    }

    const rawToolCalls = msg.tool_calls as Record<string, unknown>[] | undefined
    const toolCalls: ToolCall[] = []
    if (rawToolCalls) {
      for (const tc of rawToolCalls) {
        toolCalls.push({
          id: (tc.id as string) ?? crypto.randomUUID(),
          name: (tc.name as string) ?? 'unknown',
          status: 'completed',
          ...(typeof tc.args === 'string' ? { args: tc.args } : {}),
          ...(typeof tc.result === 'string' ? { result: tc.result } : {}),
        })
      }
    }

    const rawUsage = msg.usage as Record<string, unknown> | undefined
    let usage: UsageInfo | undefined
    if (rawUsage && typeof rawUsage === 'object') {
      usage = rawUsage as UsageInfo
    }

    // Restore activity_log for correct rendering order (CTR-0060, PRP-0031)
    const rawActivityLog = msg.activity_log as Record<string, unknown>[] | undefined
    let activityLog: ActivityEntry[] | undefined
    if (rawActivityLog && rawActivityLog.length > 0) {
      activityLog = rawActivityLog.map((e) => ({
        type: e.type as 'reasoning' | 'toolCall',
        id: e.id as string,
      }))
    }

    result.push({
      id: (msg.message_id as string) ?? crypto.randomUUID(),
      role: role as 'user' | 'assistant',
      content: text,
      createdAt: new Date().toISOString(),
      ...(reasoningBlocks.length > 0 ? { reasoningBlocks } : {}),
      ...(images.length > 0 ? { images } : {}),
      ...(toolCalls.length > 0 ? { toolCalls } : {}),
      ...(activityLog ? { activityLog } : {}),
      ...(usage ? { usage } : {}),
      // Restore per-message model / reasoning from the saved usage object so the
      // action-bar label survives reload (CTR-0030, PRP-0071). Absent -> omitted
      // (the label is simply not rendered for legacy messages, UDR-0047 D6).
      ...(usage?.model ? { model: usage.model } : {}),
      ...(usage?.reasoning ? { reasoning: usage.reasoning } : {}),
      ...(usage?.verbosity ? { verbosity: usage.verbosity } : {}),
      // Restore the structured-output flag so a reloaded JSON answer still renders
      // as a code block (CTR-0118 / CTR-0012 v11, PRP-0082, UDR-0058 D9).
      ...(usage?.structured ? { structured: true } : {}),
    })
  }
  return result
}

export function useSession() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const sessionParam = searchParams.get('session')

  // If URL has ?session=xxx, use it. Otherwise start fresh.
  const [threadId, setThreadId] = useState<string>(() => {
    if (sessionParam) return sessionParam
    const newId = crypto.randomUUID()
    localStorage.setItem(STORAGE_KEY, newId)
    return newId
  })
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [folders, setFolders] = useState<SessionFolder[]>([])
  const [initialMessages, setInitialMessages] = useState<ChatMessage[]>([])
  const [continuationToken, setContinuationToken] = useState<Record<string, unknown> | null>(null)
  const [isSwitching, setIsSwitching] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [isCreatingFolder, setIsCreatingFolder] = useState(false)
  const [deletingFolderId, setDeletingFolderId] = useState<string | null>(null)
  const [updatingFolderId, setUpdatingFolderId] = useState<string | null>(null)
  const [movingSessionId, setMovingSessionId] = useState<string | null>(null)
  // Session Import in progress (PRP-0084, CTR-0016 v4). Drives the sidebar
  // animated indicator while a bundle uploads and the server validates it.
  const [isImporting, setIsImporting] = useState(false)
  // CTR-0110 WebSocket connection health (UDR-0053 D16). True only while the
  // /ws/notifications socket is open and authenticated; gates the Auto Session
  // Title list-refresh fallback so a healthy push channel performs no polling.
  const [wsConnected, setWsConnected] = useState(false)
  const abortRef = useRef<(() => void) | null>(null)
  const switchedRef = useRef(false)
  // Session list pagination state (PRP-0112 Part 4). Counts live in refs because
  // the loaders read them without wanting to be re-created on every change.
  const rootLoadedCountRef = useRef(0)
  const loadingMoreRef = useRef(false)
  const loadedFolderIdsRef = useRef<Set<string>>(new Set())
  const [rootTotal, setRootTotal] = useState(0)
  const [isLoadingMoreSessions, setIsLoadingMoreSessions] = useState(false)

  // Persist threadId to localStorage and sync URL
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, threadId)
  }, [threadId])

  // ---- Session list loading (CTR-0015 / CTR-0016 v6, PRP-0112 Part 4) --------
  //
  // The list is now loaded INCREMENTALLY (UDR-0091 D3). `sessions` holds the
  // union of what has actually been loaded: the root ("Chats") pages fetched so
  // far, plus the sessions of every folder the user has expanded (UDR-0091 D4 --
  // a folder is fetched complete, never paginated, because a half-loaded folder
  // would silently misrepresent its contents).
  //
  // The server returns each list ALREADY SORTED (pinned first, then updated_at
  // desc -- UDR-0091 D1). Nothing here re-sorts: a client holding one page cannot
  // produce a globally correct order, which is exactly why the responsibility
  // moved to the server.

  /** Replace/merge a freshly fetched slice into the union, keyed by thread_id. */
  const mergeSessions = useCallback((incoming: SessionSummary[], replaceScope?: (s: SessionSummary) => boolean) => {
    setSessions((prev) => {
      const kept = replaceScope ? prev.filter((s) => !replaceScope(s)) : prev
      const byId = new Map(kept.map((s) => [s.thread_id, s]))
      for (const s of incoming) byId.set(s.thread_id, s)
      return [...byId.values()]
    })
  }, [])

  const fetchSessionPage = useCallback(async (params: { limit?: number; offset?: number; folderId?: string }) => {
    const qs = new URLSearchParams()
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    if (params.folderId !== undefined) qs.set('folder_id', params.folderId)
    const query = qs.toString()
    const res = await fetch(`/api/sessions${query ? `?${query}` : ''}`)
    if (!res.ok) return null
    const items = normalizeSessionSummaries(await res.json())
    // Total count rides in a header so the response body stays a bare array
    // (UDR-0091 D3) and no existing consumer breaks.
    const totalRaw = res.headers.get('X-Total-Count')
    const total = totalRaw === null ? items.length : Number.parseInt(totalRaw, 10)
    return { items, total: Number.isNaN(total) ? items.length : total }
  }, [])

  /**
   * Reload the root ("Chats") list from the top.
   *
   * Refetches as many root sessions as are currently loaded (never fewer than one
   * page), so a refresh triggered by a rename / pin / delete does not yank a user
   * who has scrolled deep back up to page one.
   */
  const refreshSessions = useCallback(async () => {
    try {
      const loadedRoot = rootLoadedCountRef.current
      const limit = Math.max(SESSION_PAGE_SIZE, loadedRoot)
      const page = await fetchSessionPage({ limit, offset: 0, folderId: ROOT_FOLDER })
      if (!page) return
      rootLoadedCountRef.current = page.items.length
      setRootTotal(page.total)
      // Replace the whole root scope; folder-scoped entries are left untouched.
      mergeSessions(page.items, (s) => !s.folder_id)
    } catch {
      // ignore fetch errors
    }
  }, [fetchSessionPage, mergeSessions])

  /** Append the next page of root chats (infinite scroll). */
  const loadMoreSessions = useCallback(async () => {
    if (loadingMoreRef.current) return
    loadingMoreRef.current = true
    setIsLoadingMoreSessions(true)
    try {
      const page = await fetchSessionPage({
        limit: SESSION_PAGE_SIZE,
        offset: rootLoadedCountRef.current,
        folderId: ROOT_FOLDER,
      })
      if (page) {
        rootLoadedCountRef.current += page.items.length
        setRootTotal(page.total)
        mergeSessions(page.items)
      }
    } catch {
      // ignore fetch errors
    } finally {
      loadingMoreRef.current = false
      setIsLoadingMoreSessions(false)
    }
  }, [fetchSessionPage, mergeSessions])

  /** Fetch one folder's sessions COMPLETE (UDR-0091 D4); called when it expands. */
  const loadFolderSessions = useCallback(
    async (folderId: string) => {
      if (loadedFolderIdsRef.current.has(folderId)) return
      loadedFolderIdsRef.current.add(folderId)
      try {
        const page = await fetchSessionPage({ folderId })
        if (page) mergeSessions(page.items, (s) => s.folder_id === folderId)
        else loadedFolderIdsRef.current.delete(folderId)
      } catch {
        loadedFolderIdsRef.current.delete(folderId)
      }
    },
    [fetchSessionPage, mergeSessions],
  )

  const refreshFolders = useCallback(async () => {
    try {
      const res = await fetch('/api/sessions/folders')
      if (res.ok) {
        setFolders(normalizeFolders(await res.json()))
      } else if (res.status === 404) {
        setFolders([])
      }
    } catch {
      // ignore fetch errors
    }
  }, [])

  useEffect(() => {
    refreshSessions()
  }, [refreshSessions])

  useEffect(() => {
    refreshFolders()
  }, [refreshFolders])

  // Server -> client notifications over WebSocket (CTR-0110, PRP-0077). The
  // Auto Session Title background task pushes a `session_title` event when it
  // finalizes a title, so the sidebar updates in real time without a refetch
  // (UDR-0053 D11, amended). Bounded reconnect: stops after a few failures so a
  // deployment where the WS cannot authenticate (API_KEY-only LAN) does not
  // reconnect forever -- titles still appear on the next list refresh / reload.
  useEffect(() => {
    let socket: WebSocket | null = null
    let closed = false
    let attempts = 0
    let timer: number | undefined

    const connect = () => {
      if (closed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${proto}://${window.location.host}/ws/notifications`)
      socket.onopen = () => {
        attempts = 0
        // Primary path is live (UDR-0053 D16): the fallback poll stays off.
        setWsConnected(true)
      }
      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as { type?: string; thread_id?: string; title?: string }
          if (msg.type === 'session_title' && typeof msg.thread_id === 'string') {
            setSessions((prev) =>
              prev.map((s) =>
                s.thread_id === msg.thread_id
                  ? { ...s, title: typeof msg.title === 'string' ? msg.title : s.title, auto_title_pending: false }
                  : s,
              ),
            )
          }
        } catch {
          // ignore malformed frames
        }
      }
      socket.onerror = () => {
        socket?.close()
      }
      socket.onclose = () => {
        // The push channel is down (UDR-0053 D16): re-enable the fallback poll.
        setWsConnected(false)
        if (closed || attempts >= 5) return
        attempts += 1
        timer = window.setTimeout(connect, Math.min(1000 * attempts, 5000))
      }
    }

    connect()
    return () => {
      closed = true
      setWsConnected(false)
      if (timer) window.clearTimeout(timer)
      socket?.close()
    }
  }, [])

  // Self-healing fallback for the Auto Session Title spinner (PRP-0077,
  // CTR-0109, UDR-0053 D16). The CTR-0110 WebSocket push is the SOLE primary,
  // real-time path; when it is connected the `session_title` event clears the
  // pending row directly, so we do NOT poll. Only when the socket is NOT
  // connected (e.g. an API_KEY-only LAN deployment where the WS cannot
  // authenticate, or a dev proxy gap) would a pending row otherwise spin
  // forever -- so poll the list once every few seconds. The backend clears
  // `auto_title_pending` on finalize (always, even on failure), so this
  // terminates as soon as the title resolves and never loops indefinitely. When
  // the socket reconnects, `wsConnected` flips and this effect tears down the
  // timer, returning steady-state polling to zero.
  useEffect(() => {
    if (wsConnected) return
    if (!sessions.some((s) => s.auto_title_pending)) return
    const t = window.setTimeout(() => {
      refreshSessions()
    }, 4000)
    return () => window.clearTimeout(t)
  }, [wsConnected, sessions, refreshSessions])

  // Load initial messages when URL has ?session= parameter (page load only).
  // switchSession already loads data before navigating, so skip the re-fetch.
  useEffect(() => {
    if (!sessionParam) return
    if (switchedRef.current) {
      switchedRef.current = false
      return
    }
    let cancelled = false
    async function load() {
      try {
        const res = await fetch(`/api/sessions/${sessionParam}`)
        if (!res.ok || cancelled) return
        const data = await res.json()
        const msgs = convertMafMessages(data.messages ?? [])
        if (!cancelled) {
          setInitialMessages(msgs)
          setContinuationToken((data.continuation_token as Record<string, unknown>) ?? null)
        }
      } catch {
        if (!cancelled) setInitialMessages([])
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [sessionParam])

  const registerAbort = useCallback((abortFn: () => void) => {
    abortRef.current = abortFn
  }, [])

  const createSession = useCallback(() => {
    abortRef.current?.()
    const newId = crypto.randomUUID()
    setInitialMessages([])
    setContinuationToken(null)
    setThreadId(newId)
    navigate('/chat', { replace: true })
    if (shouldAutoCloseSidebar()) setSidebarOpen(false)
  }, [navigate])

  const switchSession = useCallback(
    async (targetThreadId: string) => {
      if (targetThreadId === threadId) {
        if (shouldAutoCloseSidebar()) setSidebarOpen(false)
        return
      }

      abortRef.current?.()
      setIsSwitching(true)

      try {
        const res = await fetch(`/api/sessions/${targetThreadId}`)
        if (res.ok) {
          const data = await res.json()
          const msgs = convertMafMessages(data.messages ?? [])
          setInitialMessages(msgs)
          setContinuationToken((data.continuation_token as Record<string, unknown>) ?? null)
        } else {
          setInitialMessages([])
          setContinuationToken(null)
        }
      } catch {
        setInitialMessages([])
      }

      setThreadId(targetThreadId)
      switchedRef.current = true
      navigate(`/chat?session=${targetThreadId}`, { replace: true })
      setIsSwitching(false)
      if (shouldAutoCloseSidebar()) setSidebarOpen(false)
    },
    [threadId, navigate],
  )

  const deleteSession = useCallback(
    async (targetThreadId: string) => {
      try {
        const res = await fetch(`/api/sessions/${targetThreadId}`, { method: 'DELETE' })
        if (res.ok) {
          setSessions((prev) => prev.filter((s) => s.thread_id !== targetThreadId))
          if (targetThreadId === threadId) {
            createSession()
          }
        }
      } catch {
        // ignore
      }
    },
    [threadId, createSession],
  )

  const forkSession = useCallback(
    async (sourceThreadId: string, upToIndex: number): Promise<string | null> => {
      try {
        const res = await fetch(`/api/sessions/${sourceThreadId}/fork`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ up_to_index: upToIndex }),
        })
        if (!res.ok) {
          // Surface the failure instead of swallowing it so a branch click
          // never silently does nothing (CTR-0015 fork; v0.68.2).
          console.error(`Branch failed: POST /api/sessions/${sourceThreadId}/fork returned ${res.status}`)
          return null
        }
        const data = await res.json()
        const newThreadId = data.new_thread_id as string
        await switchSession(newThreadId)
        await refreshSessions()
        return newThreadId
      } catch (err) {
        console.error('Branch failed:', err)
        return null
      }
    },
    [switchSession, refreshSessions],
  )

  const renameSession = useCallback(async (targetThreadId: string, title: string) => {
    try {
      const res = await fetch(`/api/sessions/${targetThreadId}/rename`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      })
      if (res.ok) {
        const data = await res.json()
        setSessions((prev) =>
          prev.map((s) => (s.thread_id === targetThreadId ? { ...s, title: data.title as string } : s)),
        )
      }
    } catch {
      // ignore
    }
  }, [])

  const archiveSession = useCallback(
    async (targetThreadId: string) => {
      try {
        const res = await fetch(`/api/sessions/${targetThreadId}/archive`, { method: 'POST' })
        if (res.ok) {
          setSessions((prev) => prev.filter((s) => s.thread_id !== targetThreadId))
          if (targetThreadId === threadId) {
            createSession()
          }
        }
      } catch {
        // ignore
      }
    },
    [threadId, createSession],
  )

  const pinSession = useCallback(async (targetThreadId: string, pinned: boolean) => {
    try {
      const res = await fetch(`/api/sessions/${targetThreadId}/pin`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pinned }),
      })
      if (res.ok) {
        const data = await res.json()
        setSessions((prev) =>
          prev.map((s) =>
            s.thread_id === targetThreadId ? { ...s, pinned_at: (data.pinned_at as string | null) ?? null } : s,
          ),
        )
      }
    } catch {
      // ignore
    }
  }, [])

  const createFolder = useCallback(
    async (name: string, color: FolderColor = DEFAULT_FOLDER_COLOR) => {
      const trimmed = name.trim()
      if (!trimmed) return false

      const tempFolderId = `temp-${crypto.randomUUID()}`
      const now = new Date().toISOString()
      const optimisticFolder: SessionFolder = {
        id: tempFolderId,
        name: trimmed,
        color,
        // New folders append to the end of the order; the server assigns the
        // authoritative value and the refresh/replace below reconciles it.
        order: Number.MAX_SAFE_INTEGER,
        created_at: now,
        updated_at: now,
      }

      setIsCreatingFolder(true)
      setFolders((prev) => [...prev, optimisticFolder])

      try {
        const res = await fetch('/api/sessions/folders', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: trimmed, color }),
        })
        if (!res.ok) throw new Error('Failed to create folder')

        const payload = await res.json()
        const folder = normalizeFolder(payload) ?? normalizeFolder((payload as { folder?: unknown }).folder)
        if (folder) {
          setFolders((prev) => prev.map((item) => (item.id === tempFolderId ? folder : item)))
        } else {
          await refreshFolders()
        }
        return true
      } catch {
        setFolders((prev) => prev.filter((folder) => folder.id !== tempFolderId))
        return false
      } finally {
        setIsCreatingFolder(false)
      }
    },
    [refreshFolders],
  )

  const updateFolderColor = useCallback(
    async (folderId: string, color: FolderColor) => {
      const previousFolders = folders
      setUpdatingFolderId(folderId)
      setFolders((prev) => prev.map((folder) => (folder.id === folderId ? { ...folder, color } : folder)))

      try {
        const res = await fetch(`/api/sessions/folders/${folderId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ color }),
        })
        if (!res.ok) throw new Error('Failed to update folder color')

        const folder = normalizeFolder(await res.json())
        if (folder) {
          setFolders((prev) => prev.map((item) => (item.id === folderId ? folder : item)))
        }
        return true
      } catch {
        setFolders(previousFolders)
        return false
      } finally {
        setUpdatingFolderId(null)
      }
    },
    [folders],
  )

  const reorderFolders = useCallback(
    async (orderedIds: string[]) => {
      const previousFolders = folders
      // Optimistically reflect the new order locally.
      const byId = new Map(previousFolders.map((folder) => [folder.id, folder]))
      const reordered = orderedIds
        .map((id) => byId.get(id))
        .filter((folder): folder is SessionFolder => folder !== undefined)
        .map((folder, index) => ({ ...folder, order: index }))
      setFolders(reordered)

      try {
        const res = await fetch('/api/sessions/folders/order', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder_ids: orderedIds }),
        })
        if (!res.ok) throw new Error('Failed to reorder folders')

        setFolders(normalizeFolders(await res.json()))
        return true
      } catch {
        setFolders(previousFolders)
        return false
      }
    },
    [folders],
  )

  const deleteFolder = useCallback(
    async (folderId: string) => {
      const previousFolders = folders
      const previousSessions = sessions

      setDeletingFolderId(folderId)
      setFolders((prev) => prev.filter((folder) => folder.id !== folderId))
      setSessions((prev) =>
        prev.map((session) => (session.folder_id === folderId ? { ...session, folder_id: null } : session)),
      )

      try {
        const res = await fetch(`/api/sessions/folders/${folderId}`, { method: 'DELETE' })
        if (!res.ok) throw new Error('Failed to delete folder')

        await Promise.all([refreshFolders(), refreshSessions()])
        return true
      } catch {
        setFolders(previousFolders)
        setSessions(previousSessions)
        return false
      } finally {
        setDeletingFolderId(null)
      }
    },
    [folders, refreshFolders, refreshSessions, sessions],
  )

  const moveSessionToFolder = useCallback(
    async (targetThreadId: string, folderId: string | null) => {
      const previousSessions = sessions

      setMovingSessionId(targetThreadId)
      setSessions((prev) =>
        prev.map((session) => (session.thread_id === targetThreadId ? { ...session, folder_id: folderId } : session)),
      )

      try {
        const res = await fetch(`/api/sessions/${targetThreadId}/folder`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder_id: folderId }),
        })
        if (!res.ok) throw new Error('Failed to move session')

        await Promise.all([refreshFolders(), refreshSessions()])
        return true
      } catch {
        setSessions(previousSessions)
        return false
      } finally {
        setMovingSessionId(null)
      }
    },
    [refreshFolders, refreshSessions, sessions],
  )

  // Session Export (PRP-0084, CTR-0015 v1.15 / CTR-0016 v4). Downloads the chat
  // as a self-contained ZIP bundle (session JSON + its uploads). Read-only GET.
  const exportSession = useCallback(async (targetThreadId: string) => {
    try {
      const res = await fetch(`/api/sessions/${targetThreadId}/export`)
      if (!res.ok) return
      const blob = await res.blob()
      const disposition = res.headers.get('Content-Disposition') ?? ''
      const match = disposition.match(/filename="?([^"]+)"?/)
      const filename = match ? match[1] : `chatwalaau-chat-${targetThreadId}.zip`
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch {
      // ignore download errors
    }
  }, [])

  // Session Import (PRP-0084, CTR-0015 v1.15 / CTR-0016 v4). Uploads a ZIP
  // bundle; the server validates it and creates a NEW session. On success the
  // list refreshes and we switch to the imported chat.
  const importSession = useCallback(
    async (file: File): Promise<ImportResult> => {
      setIsImporting(true)
      try {
        const form = new FormData()
        form.append('file', file)
        const res = await fetch('/api/sessions/import', { method: 'POST', body: form })
        if (!res.ok) {
          // Surface the server's reason (CTR-0016 v5); previously swallowed.
          let error = `Import failed (HTTP ${res.status}).`
          try {
            const body = (await res.json()) as { detail?: unknown }
            if (typeof body?.detail === 'string' && body.detail) error = body.detail
          } catch {
            // non-JSON error body -> keep the generic message
          }
          return { ok: false, error }
        }
        const summary = (await res.json()) as SessionSummary
        await refreshSessions()
        if (summary && typeof summary.thread_id === 'string') {
          await switchSession(summary.thread_id)
        }
        return { ok: true, warnings: Array.isArray(summary?.warnings) ? summary.warnings : [] }
      } catch {
        return { ok: false, error: 'Import failed (network error).' }
      } finally {
        setIsImporting(false)
      }
    },
    [refreshSessions, switchSession],
  )

  return {
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
    refreshFolders,
    registerAbort,
    // Session list pagination (PRP-0112 Part 4, CTR-0016 v6).
    loadMoreSessions,
    loadFolderSessions,
    isLoadingMoreSessions,
    /** More root chats exist on the server than are currently loaded. */
    hasMoreSessions: sessions.filter((s) => !s.folder_id).length < rootTotal,
  }
}
