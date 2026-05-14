/**
 * MCP App View component (CTR-0068, PRP-0034).
 *
 * Renders an MCP App View in a double-iframe sandbox within an assistant message.
 * Manages the initialization handshake and postMessage communication.
 *
 * Flow:
 *  1. Fetch sandbox port from /api/mcp-apps/config + View HTML from html_ref
 *  2. Set iframe src to sandbox proxy (triggers proxy load)
 *  3. Sandbox sends "sandbox-proxy-ready" via postMessage
 *  4. Host sends View HTML via "sandbox-resource-ready"
 *  5. View sends "ui/initialize" -> Host responds with context
 *  6. Host sends tool input and result immediately after init response
 */

import { AppWindow, Maximize2, Minimize2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { HOST_STYLE_VARIABLES } from '@/lib/mcp-apps-styles'
import type { McpAppEvent } from '@/types/chat'

interface McpAppViewProps {
  event: McpAppEvent
  toolResult?: string
  toolArgs?: string
}

const PROXY_READY = 'ui/notifications/sandbox-proxy-ready'
const RESOURCE_READY = 'ui/notifications/sandbox-resource-ready'

export function McpAppView({ event, toolResult, toolArgs }: McpAppViewProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const [initialized, setInitialized] = useState(false)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [iframeHeight, setIframeHeight] = useState(400)
  const [sandboxUrl, setSandboxUrl] = useState<string | null>(null)

  // Stable refs for values accessed in postMessage handler
  const htmlRef = useRef<string | null>(null)
  const serverNameRef = useRef(event.server_name)
  const toolArgsRef = useRef(toolArgs)
  const toolResultRef = useRef(toolResult)
  const isFullscreenRef = useRef(isFullscreen)
  const sentToolDataRef = useRef(false)
  serverNameRef.current = event.server_name
  toolArgsRef.current = toolArgs
  toolResultRef.current = toolResult
  isFullscreenRef.current = isFullscreen

  /** Send tool input + result to the View iframe.
   *  Reads from refs so it is safe to call from a stable useEffect. */
  const sendToolData = (win: Window) => {
    if (sentToolDataRef.current) return
    sentToolDataRef.current = true

    const args = toolArgsRef.current
    if (args) {
      try {
        win.postMessage(
          { jsonrpc: '2.0', method: 'ui/notifications/tool-input', params: { arguments: JSON.parse(args) } },
          '*',
        )
      } catch {
        /* not valid JSON */
      }
    }

    const result = toolResultRef.current
    if (result) {
      let content: unknown
      try {
        const parsed = JSON.parse(result)
        content = [{ type: 'text', text: typeof parsed === 'string' ? parsed : JSON.stringify(parsed) }]
      } catch {
        content = [{ type: 'text', text: result }]
      }
      win.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { content } }, '*')
    }
  }

  // Fetch sandbox port + View HTML, then set sandboxUrl to render iframe
  useEffect(() => {
    let cancelled = false
    Promise.all([
      fetch('/api/mcp-apps/config').then((r) => r.json()),
      fetch(event.html_ref).then((r) => {
        if (!r.ok) throw new Error(`Failed to fetch View HTML: ${r.status}`)
        return r.text()
      }),
    ])
      .then(([config, html]) => {
        if (cancelled) return
        htmlRef.current = html
        const port = config.sandbox_port ?? 8081
        const url = new URL(`http://localhost:${port}/sandbox.html`)
        if (event.csp) url.searchParams.set('csp', JSON.stringify(event.csp))
        setSandboxUrl(url.href)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [event.html_ref, event.csp])

  // Single stable postMessage handler — no deps on props that change.
  // All mutable values are read via refs so the handler never goes stale.
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentionally stable — uses refs
  useEffect(() => {
    const handler = (e: MessageEvent) => {
      const iframe = iframeRef.current
      if (!iframe || e.source !== iframe.contentWindow) return
      const data = e.data
      if (!data || typeof data !== 'object') return

      // Sandbox proxy ready -> send View HTML
      if (data.method === PROXY_READY) {
        if (htmlRef.current && iframe.contentWindow) {
          iframe.contentWindow.postMessage(
            {
              jsonrpc: '2.0',
              method: RESOURCE_READY,
              params: { html: htmlRef.current, sandbox: 'allow-scripts allow-same-origin allow-forms' },
            },
            '*',
          )
        }
        return
      }

      // View initializing -> respond with host context + send tool data
      if (data.method === 'ui/initialize') {
        iframe.contentWindow?.postMessage(
          {
            jsonrpc: '2.0',
            id: data.id,
            result: {
              protocolVersion: '2025-03-26',
              hostInfo: { name: 'ChatWalaʻau', version: '0.34.0' },
              hostCapabilities: { openLinks: {}, updateModelContext: { text: {} } },
              hostContext: {
                theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light',
                platform: 'web',
                styles: { variables: HOST_STYLE_VARIABLES },
                containerDimensions: { maxHeight: 600 },
                displayMode: isFullscreenRef.current ? 'fullscreen' : 'inline',
                availableDisplayModes: ['inline', 'fullscreen'],
              },
            },
          },
          '*',
        )
        // Send tool data immediately after init response (same as reference impl)
        if (iframe.contentWindow) sendToolData(iframe.contentWindow)
        setInitialized(true)
        return
      }

      // Also handle notifications/initialized (some Views send this)
      if (data.method === 'notifications/initialized') {
        // Tool data already sent after ui/initialize response, just mark ready
        setInitialized(true)
        return
      }

      // View requests
      if (data.method === 'ui/open-link' && data.params?.url) {
        window.open(data.params.url, '_blank', 'noopener,noreferrer')
        iframe.contentWindow?.postMessage({ jsonrpc: '2.0', id: data.id, result: {} }, '*')
        return
      }
      if (data.method === 'ui/size-change' && data.params) {
        if (typeof data.params.height === 'number' && data.params.height > 0) {
          setIframeHeight(Math.min(data.params.height, 800))
        }
        return
      }
      if (data.method === 'ui/request-display-mode' && data.params) {
        const mode = data.params.mode === 'fullscreen' ? 'fullscreen' : 'inline'
        setIsFullscreen(mode === 'fullscreen')
        iframe.contentWindow?.postMessage({ jsonrpc: '2.0', id: data.id, result: { mode } }, '*')
        return
      }

      // Proxy tool calls / resource reads to backend
      if (data.method === 'tools/call' || data.method === 'resources/read') {
        fetch(`/api/mcp-apps/${serverNameRef.current}/rpc`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ method: data.method, params: data.params }),
        })
          .then((res) => {
            if (!res.ok) throw new Error(`RPC error: ${res.status}`)
            return res.json()
          })
          .then((body) => {
            iframe.contentWindow?.postMessage({ jsonrpc: '2.0', id: data.id, result: body.result ?? body }, '*')
          })
          .catch((err) => {
            iframe.contentWindow?.postMessage(
              { jsonrpc: '2.0', id: data.id, error: { code: -32603, message: err.message } },
              '*',
            )
          })
        return
      }
    }

    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, []) // Empty deps: handler is stable, reads all changing values via refs

  if (error) {
    return (
      <div className="my-2 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
        MCP App error: {error}
      </div>
    )
  }

  if (isFullscreen) {
    return (
      <div className="fixed inset-0 z-[1000] flex flex-col bg-background">
        <div className="flex items-center justify-between border-b px-4 py-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <AppWindow className="h-4 w-4" />
            {event.tool_name}
          </div>
          <Button variant="ghost" size="sm" onClick={() => setIsFullscreen(false)}>
            <Minimize2 className="h-4 w-4" />
          </Button>
        </div>
        <div className="flex-1">
          {sandboxUrl && (
            <iframe
              ref={iframeRef}
              src={sandboxUrl}
              sandbox="allow-scripts allow-same-origin allow-forms"
              style={{ width: '100%', height: '100%', border: 'none' }}
              className="bg-background"
              title={`MCP App: ${event.tool_name}`}
            />
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="my-2 overflow-hidden rounded-lg border">
      <div className="flex items-center justify-between border-b bg-muted/50 px-3 py-1.5">
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <AppWindow className="h-3.5 w-3.5" />
          <span>{event.tool_name}</span>
          {!initialized && sandboxUrl && <span className="animate-pulse text-[10px]">...</span>}
        </div>
        <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => setIsFullscreen(true)}>
          <Maximize2 className="h-3 w-3" />
        </Button>
      </div>
      {sandboxUrl ? (
        <iframe
          ref={iframeRef}
          src={sandboxUrl}
          sandbox="allow-scripts allow-same-origin allow-forms"
          style={{ width: '100%', height: `${iframeHeight}px`, border: 'none', borderRadius: '0 0 0.5rem 0.5rem' }}
          className="bg-background"
          title={`MCP App: ${event.tool_name}`}
        />
      ) : (
        <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">Loading sandbox...</div>
      )}
    </div>
  )
}
