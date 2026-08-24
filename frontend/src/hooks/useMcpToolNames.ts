import { createContext, useCallback, useContext, useEffect, useState } from 'react'

/**
 * The set of tool names that genuinely come from an MCP server (CTR-0013 v8 /
 * CTR-0121, PRP-0145 / UDR-0125 D8).
 *
 * This exists so the chat's tool-activity label can VERIFY an MCP claim instead
 * of assuming one. Before PRP-0145, `ToolCallIndicator` labelled every tool name
 * it did not recognise as `MCP: {name}` -- a fallback branch, not a judgement --
 * which was approximately true when the only unrecognised tools were MCP tools
 * and false for the three subsystems added since. A harness agent's own
 * `run_shell` and `file_access_ls`, and a Prompt-lane `manage_cron`, all reported
 * themselves as MCP to operators who may have had no MCP server configured at
 * all.
 *
 * The names come from the CTR-0121 inventory the MCP manager already consumes;
 * this holds only the flattened NAME set, which is all a label needs.
 *
 * FAILS TOWARD HONESTY: any error, or the window before the first response,
 * leaves the set empty, so an unmapped tool renders as the neutral
 * `Tool: {name}` rather than claiming an origin nobody checked. That is the
 * degraded state UDR-0125 D8 requires -- withholding a fact is recoverable,
 * asserting a wrong one is the defect. Nothing about the label ever waits on the
 * network.
 */

/** Window event the MCP manager fires after a change that can alter the inventory. */
export const MCP_INVENTORY_CHANGED_EVENT = 'chatwalaau:mcp-inventory-changed'

/** Fire-and-forget notice that the MCP inventory may have changed (apply / reload). */
export function notifyMcpInventoryChanged(): void {
  window.dispatchEvent(new CustomEvent(MCP_INVENTORY_CHANGED_EVENT))
}

interface McpToolsResponse {
  servers?: Array<{ tools?: Array<{ name?: string }> }>
}

async function fetchMcpToolNames(signal: AbortSignal): Promise<Set<string>> {
  const res = await fetch('/api/mcp/tools', { signal })
  if (!res.ok) return new Set()
  const data = (await res.json()) as McpToolsResponse
  const names = new Set<string>()
  for (const server of data.servers ?? []) {
    for (const tool of server.tools ?? []) {
      if (tool.name) names.add(tool.name)
    }
  }
  return names
}

export const McpToolNamesContext = createContext<Set<string>>(new Set())

/** Read the verified MCP tool-name set. Empty until (and unless) the fetch succeeds. */
export function useMcpToolNames(): Set<string> {
  return useContext(McpToolNamesContext)
}

/** Owns the fetch + refresh lifecycle; consumed by McpToolNamesProvider. */
export function useMcpToolNamesState(): Set<string> {
  const [names, setNames] = useState<Set<string>>(() => new Set())

  const load = useCallback((signal: AbortSignal) => {
    fetchMcpToolNames(signal)
      .then((next) => {
        if (!signal.aborted) setNames(next)
      })
      .catch(() => {
        // Non-blocking by design: an unreachable or gated inventory leaves the
        // set empty, which downgrades a label to `Tool: {name}` and never to a
        // false `MCP:` claim.
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    // The inventory changes on exactly two operator actions (CTR-0121 apply,
    // CTR-0061 reload); both notify through this event.
    const onChanged = () => load(controller.signal)
    window.addEventListener(MCP_INVENTORY_CHANGED_EVENT, onChanged)
    return () => {
      window.removeEventListener(MCP_INVENTORY_CHANGED_EVENT, onChanged)
      controller.abort()
    }
  }, [load])

  return names
}
