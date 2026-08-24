import type { ReactNode } from 'react'
import { McpToolNamesContext, useMcpToolNamesState } from '@/hooks/useMcpToolNames'

/**
 * Mounts the verified MCP tool-name set for authenticated surfaces (CTR-0013 v8 /
 * CTR-0121, PRP-0145 / UDR-0125 D8).
 *
 * It mounts INSIDE AuthGuard rather than at the app root, because the CTR-0121
 * inventory is CTR-0083-gated: fetching it around the login screen would only
 * produce a 401 and then never retry, leaving the set permanently empty for the
 * session. AuthGuard renders its children exactly once the surface is allowed to
 * run, so the fetch happens when it can actually succeed, and every tool-call
 * surface (/chat, /popup, /sidebar) is covered by the one mount point.
 *
 * An empty set is a correct state, not a failure state: it downgrades an unmapped
 * tool label to `Tool: {name}` and never to a false `MCP:` claim.
 */
export function McpToolNamesProvider({ children }: { children: ReactNode }) {
  const names = useMcpToolNamesState()
  return <McpToolNamesContext.Provider value={names}>{children}</McpToolNamesContext.Provider>
}
