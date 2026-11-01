/**
 * MCP App host services shared by the chat and its Views (CTR-0068 v2, PRP-0240).
 *
 * - `McpAppHostContext`: what a View's `ui/message` needs from the chat that renders it
 *   (UDR-0019 D13): send the text as a new user turn, refused while a run is streaming.
 * - The model-context store: each View's last `ui/update-model-context` (last one wins),
 *   taken once by the next user turn and sent as AG-UI `RunAgentInput.context`. It is
 *   never shown in the chat and never persisted as a message.
 */

import { createContext, useContext } from 'react'

export interface McpAppHost {
  /** The chat the Views belong to; null for a chat without a thread (no context kept). */
  threadId: string | null
  /** Send a View's message as a user turn. False when refused (a run is streaming). */
  sendViewMessage: (text: string, source: string) => boolean
}

export const McpAppHostContext = createContext<McpAppHost | null>(null)

export function useMcpAppHost(): McpAppHost | null {
  return useContext(McpAppHostContext)
}

/** AG-UI `Context` entry (RunAgentInput.context). */
export interface AguiContextEntry {
  description: string
  value: string
}

/** The description prefix the backend recognizes (app/mcp_apps/context.py). */
export const MCP_APP_CONTEXT_PREFIX = 'mcp_app:'

interface PendingContext {
  threadId: string
  source: string
  value: string
}

const pending = new Map<string, PendingContext>()

/** Keep (or with an empty value, drop) a View's latest model context, keyed by its call id. */
export function setViewModelContext(threadId: string, callId: string, source: string, value: string): void {
  if (value.trim()) pending.set(callId, { threadId, source, value })
  else pending.delete(callId)
}

/** Forget a View's model context (the View was removed from the page). */
export function dropViewModelContext(callId: string): void {
  pending.delete(callId)
}

/** Take this chat's pending View contexts for the next user turn (each is sent once). */
export function takeViewModelContext(threadId: string | null): AguiContextEntry[] {
  if (!threadId) return []
  const out: AguiContextEntry[] = []
  for (const [callId, entry] of pending) {
    if (entry.threadId !== threadId) continue
    out.push({ description: `${MCP_APP_CONTEXT_PREFIX}${entry.source}`, value: entry.value })
    pending.delete(callId)
  }
  return out
}

/** Text of MCP content blocks plus structured content, for the model (UDR-0019 D13). */
export function contextText(content: unknown, structuredContent: unknown): string {
  const parts: string[] = []
  if (Array.isArray(content)) {
    for (const block of content) {
      if (block && typeof block === 'object' && (block as { type?: unknown }).type === 'text') {
        const text = (block as { text?: unknown }).text
        if (typeof text === 'string' && text.trim()) parts.push(text)
      }
    }
  }
  if (structuredContent && typeof structuredContent === 'object') parts.push(JSON.stringify(structuredContent))
  return parts.join('\n')
}
