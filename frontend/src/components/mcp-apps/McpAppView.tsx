/**
 * MCP App View component (CTR-0068 v2, PRP-0034, PRP-0240).
 *
 * Renders an MCP App View in a double-iframe sandbox within an assistant message. The
 * host side of the MCP Apps protocol (stable specification 2026-01-26) is the official
 * AppBridge (@modelcontextprotocol/ext-apps, UDR-0019 D8), constructed with no MCP
 * client: ChatWalaʻau's own handlers proxy the View's requests to the backend. This
 * module is lazy-loaded, so the SDK is fetched only when a View is shown.
 *
 * Flow:
 *  1. Fetch the sandbox config (port, running) and the View HTML from html_ref
 *  2. Render ONE iframe on the sandbox origin (<page host>:<port>, UDR-0019 D15) and
 *     connect an AppBridge over a PostMessageTransport to it
 *  3. Sandbox sends sandbox-proxy-ready -> the bridge sends sandbox-resource-ready (HTML)
 *  4. View sends ui/initialize -> the bridge answers with host info, capabilities, context
 *  5. On ui/notifications/initialized: tool input, then the full CallToolResult
 *  6. Theme / display-mode changes -> host-context-changed; unmount -> resource-teardown
 */

import { AppBridge, type McpUiHostContext, PostMessageTransport } from '@modelcontextprotocol/ext-apps/app-bridge'
import { AppWindow, Maximize2, Minimize2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { contextText, dropViewModelContext, setViewModelContext, useMcpAppHost } from '@/lib/mcp-app-host'
import { HOST_STYLE_VARIABLES } from '@/lib/mcp-apps-styles'
import type { McpAppEvent } from '@/types/chat'

interface McpAppViewProps {
  event: McpAppEvent
  toolResult?: string
  toolArgs?: string
}

const SANDBOX_PERMISSIONS = 'allow-scripts allow-same-origin allow-forms'
const INLINE_MAX_HEIGHT = 600
const INLINE_HEIGHT_CAP = 800
const TEARDOWN_TIMEOUT_MS = 1000

type DisplayMode = 'inline' | 'fullscreen'

function currentTheme(): 'light' | 'dark' {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

function hostContext(displayMode: DisplayMode): McpUiHostContext {
  return {
    theme: currentTheme(),
    platform: 'web',
    styles: { variables: HOST_STYLE_VARIABLES as NonNullable<McpUiHostContext['styles']>['variables'] },
    displayMode,
    availableDisplayModes: ['inline', 'fullscreen'],
    containerDimensions:
      displayMode === 'fullscreen'
        ? { height: window.innerHeight, width: window.innerWidth }
        : { maxHeight: INLINE_MAX_HEIGHT },
    locale: navigator.language,
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    userAgent: 'ChatWalaʻau',
  }
}

/** The View's tool result: the full CallToolResult when the backend sent it (UDR-0019 D12),
 *  else the text result the chat holds, as one text block. */
function toolResultParams(event: McpAppEvent, toolResult?: string): Record<string, unknown> | null {
  if (event.tool_result && typeof event.tool_result === 'object') return event.tool_result
  if (!toolResult) return null
  try {
    const parsed = JSON.parse(toolResult)
    return { content: [{ type: 'text', text: typeof parsed === 'string' ? parsed : JSON.stringify(parsed) }] }
  } catch {
    return { content: [{ type: 'text', text: toolResult }] }
  }
}

function toolArguments(toolArgs?: string): Record<string, unknown> {
  if (!toolArgs) return {}
  try {
    const parsed = JSON.parse(toolArgs)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

/** The sandbox URL on the page's own host, so a browser on another machine reaches it. */
function sandboxUrlFor(port: number, csp: McpAppEvent['csp']): string {
  const url = new URL(`${window.location.protocol}//${window.location.hostname}:${port}/sandbox.html`)
  if (csp) url.searchParams.set('csp', JSON.stringify(csp))
  return url.href
}

async function rpc(serverName: string, method: string, params: unknown): Promise<Record<string, unknown>> {
  const res = await fetch(`/api/mcp-apps/${encodeURIComponent(serverName)}/rpc`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ method, params }),
  })
  if (!res.ok) {
    let detail = `RPC error: ${res.status}`
    try {
      const body = await res.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      /* not JSON */
    }
    throw new Error(detail)
  }
  const body = await res.json()
  return (body.result ?? body) as Record<string, unknown>
}

export default function McpAppView({ event, toolResult, toolArgs }: McpAppViewProps) {
  const host = useMcpAppHost()
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const bridgeRef = useRef<AppBridge | null>(null)
  const [initialized, setInitialized] = useState(false)
  const [displayMode, setDisplayMode] = useState<DisplayMode>('inline')
  const [error, setError] = useState<string | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [iframeHeight, setIframeHeight] = useState(400)
  const [sandboxUrl, setSandboxUrl] = useState<string | null>(null)
  const [html, setHtml] = useState<string | null>(null)

  // Latest values for the bridge handlers, which are bound once per View.
  const latest = useRef({ event, toolResult, toolArgs, host, displayMode })
  latest.current = { event, toolResult, toolArgs, host, displayMode }

  // Sandbox config + View HTML.
  useEffect(() => {
    let cancelled = false
    Promise.all([
      fetch('/api/mcp-apps/config').then((r) => r.json()),
      fetch(event.html_ref).then((r) => {
        if (!r.ok) throw new Error(`Failed to fetch View HTML: ${r.status}`)
        return r.text()
      }),
    ])
      .then(([config, viewHtml]) => {
        if (cancelled) return
        if (config.sandbox_running === false) {
          setUnavailable(true)
          return
        }
        setHtml(viewHtml)
        setSandboxUrl(sandboxUrlFor(config.sandbox_port ?? 8081, event.csp))
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [event.html_ref, event.csp])

  // One AppBridge per View, connected before the sandbox page announces itself.
  useEffect(() => {
    const win = iframeRef.current?.contentWindow
    if (!sandboxUrl || !html || !win) return
    const callId = latest.current.event.call_id
    const bridge = new AppBridge(
      null,
      { name: 'ChatWalaʻau', version: __APP_VERSION__ },
      {
        openLinks: {},
        serverTools: {},
        serverResources: {},
        message: { text: {} },
        updateModelContext: { text: {}, structuredContent: {} },
      },
      { hostContext: hostContext(latest.current.displayMode) },
    )
    bridgeRef.current = bridge

    bridge.addEventListener('sandboxready', () => {
      void bridge.sendSandboxResourceReady({ html, sandbox: SANDBOX_PERMISSIONS })
    })
    bridge.addEventListener('initialized', () => {
      setInitialized(true)
      const { event: ev, toolArgs: args, toolResult: text } = latest.current
      void bridge.sendToolInput({ arguments: toolArguments(args) }).then(() => {
        const result = toolResultParams(ev, text)
        // The View exists only after its tool ran; no result means the run ended without one.
        if (result) void bridge.sendToolResult(result as Parameters<AppBridge['sendToolResult']>[0])
        else void bridge.sendToolCancelled({ reason: 'The tool call ended without a result.' })
      })
    })
    bridge.addEventListener('sizechange', ({ height }) => {
      if (typeof height === 'number' && height > 0) setIframeHeight(Math.min(height, INLINE_HEIGHT_CAP))
    })

    bridge.onopenlink = async ({ url }) => {
      if (!/^https?:\/\//i.test(url)) return { isError: true }
      window.open(url, '_blank', 'noopener,noreferrer')
      return {}
    }
    // Only the modes the host offers; otherwise the current mode is returned (spec).
    bridge.onrequestdisplaymode = async ({ mode }) => {
      if (mode !== 'fullscreen' && mode !== 'inline') return { mode: latest.current.displayMode }
      setDisplayMode(mode)
      return { mode }
    }
    bridge.onmessage = async ({ content }) => {
      const text = contextText(content, undefined)
      const current = latest.current
      if (!text || !current.host) return { isError: true }
      const accepted = current.host.sendViewMessage(text, current.event.tool_name)
      return accepted ? {} : { isError: true }
    }
    bridge.onupdatemodelcontext = async ({ content, structuredContent }) => {
      const current = latest.current
      if (current.host?.threadId) {
        setViewModelContext(
          current.host.threadId,
          callId,
          `${current.event.server_name}/${current.event.tool_name}`,
          contextText(content, structuredContent),
        )
      }
      return {}
    }
    bridge.oncalltool = async (params) =>
      (await rpc(latest.current.event.server_name, 'tools/call', params)) as Awaited<
        ReturnType<NonNullable<AppBridge['oncalltool']>>
      >
    bridge.onreadresource = async (params) =>
      (await rpc(latest.current.event.server_name, 'resources/read', params)) as Awaited<
        ReturnType<NonNullable<AppBridge['onreadresource']>>
      >

    void bridge.connect(new PostMessageTransport(win, win)).catch((err: Error) => setError(err.message))

    return () => {
      bridgeRef.current = null
      dropViewModelContext(callId)
      // resource-teardown before the View goes away (best effort, bounded).
      const done = bridge
        .teardownResource({}, { timeout: TEARDOWN_TIMEOUT_MS })
        .catch(() => undefined)
        .finally(() => void bridge.close())
      void done
    }
  }, [sandboxUrl, html])

  // host-context-changed: display mode, window size in fullscreen, and theme.
  useEffect(() => {
    if (!initialized) return
    const push = () => bridgeRef.current?.setHostContext(hostContext(displayMode))
    push()
    const observer = new MutationObserver(push)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    window.addEventListener('resize', push)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', push)
    }
  }, [initialized, displayMode])

  if (error) {
    return (
      <div className="my-2 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
        MCP App error: {error}
      </div>
    )
  }

  if (unavailable) {
    return (
      <div className="my-2 rounded-md border px-3 py-2 text-sm text-muted-foreground">
        The MCP App for {event.tool_name} cannot be shown: its sandbox is not running on this server. The tool result
        above is unchanged.
      </div>
    )
  }

  const fullscreen = displayMode === 'fullscreen'
  return (
    <div
      className={
        fullscreen ? 'fixed inset-0 z-1000 flex flex-col bg-background' : 'my-2 overflow-hidden rounded-lg border'
      }
      data-testid="mcp-app-view"
      data-display-mode={displayMode}>
      <div
        className={
          fullscreen
            ? 'flex items-center justify-between border-b px-4 py-2'
            : 'flex items-center justify-between border-b bg-muted/50 px-3 py-1.5'
        }>
        <div
          className={
            fullscreen
              ? 'flex items-center gap-2 text-sm font-medium'
              : 'flex items-center gap-1.5 text-xs text-muted-foreground'
          }>
          <AppWindow className={fullscreen ? 'h-4 w-4' : 'h-3.5 w-3.5'} />
          <span>{event.tool_name}</span>
          {!initialized && sandboxUrl && <span className="animate-pulse text-[10px]">...</span>}
        </div>
        <Button
          variant="ghost"
          size="sm"
          className={fullscreen ? undefined : 'h-6 w-6 p-0'}
          aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          onClick={() => setDisplayMode(fullscreen ? 'inline' : 'fullscreen')}>
          {fullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-3 w-3" />}
        </Button>
      </div>
      {sandboxUrl ? (
        <div className={fullscreen ? 'flex-1' : undefined}>
          <iframe
            ref={iframeRef}
            src={sandboxUrl}
            sandbox={SANDBOX_PERMISSIONS}
            style={
              fullscreen
                ? { width: '100%', height: '100%', border: 'none' }
                : { width: '100%', height: `${iframeHeight}px`, border: 'none', borderRadius: '0 0 0.5rem 0.5rem' }
            }
            className="bg-background"
            title={`MCP App: ${event.tool_name}`}
          />
        </div>
      ) : (
        <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">Loading sandbox...</div>
      )}
    </div>
  )
}
