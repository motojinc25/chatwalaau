import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  type ActivityEntry,
  type ChatMessage,
  DEFAULT_FOLDER_COLOR,
  FOLDER_COLORS,
  type FolderColor,
  type ImageRef,
  type ReasoningBlock,
  type SessionFolder,
  type SessionSummary,
  type ToolCall,
  type UsageInfo,
} from '@/types/chat'

const STORAGE_KEY = 'chatwalaau-thread-id'

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
  const abortRef = useRef<(() => void) | null>(null)
  const switchedRef = useRef(false)

  // Persist threadId to localStorage and sync URL
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, threadId)
  }, [threadId])

  const refreshSessions = useCallback(async () => {
    try {
      const res = await fetch('/api/sessions')
      if (res.ok) {
        setSessions(normalizeSessionSummaries(await res.json()))
      }
    } catch {
      // ignore fetch errors
    }
  }, [])

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
        if (closed || attempts >= 5) return
        attempts += 1
        timer = window.setTimeout(connect, Math.min(1000 * attempts, 5000))
      }
    }

    connect()
    return () => {
      closed = true
      if (timer) window.clearTimeout(timer)
      socket?.close()
    }
  }, [])

  // Self-healing fallback for the Auto Session Title spinner (PRP-0077,
  // CTR-0109). The CTR-0110 WebSocket push is the primary, real-time path, but
  // if it cannot connect (e.g. an API_KEY-only LAN deployment, or a dev proxy
  // gap) a pending row would otherwise spin forever. While any session is
  // pending, poll the list once every few seconds; the backend clears
  // `auto_title_pending` on finalize (always, even on failure), so this
  // terminates as soon as the title resolves and never loops indefinitely.
  useEffect(() => {
    if (!sessions.some((s) => s.auto_title_pending)) return
    const t = window.setTimeout(() => {
      refreshSessions()
    }, 4000)
    return () => window.clearTimeout(t)
  }, [sessions, refreshSessions])

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
    refreshFolders,
    registerAbort,
  }
}
