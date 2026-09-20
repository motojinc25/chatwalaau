import { createContext, useContext } from 'react'
import type { CommandEntry } from '@/lib/slashCommands'

/**
 * What a phone gets on the chat surface (PRP-0171, UDR-0153 D3/D4/D10).
 *
 * ENTRY_POLICY decides, per chat-hosted entry, whether it is rendered on a narrow
 * viewport. `EntryId` is a closed union and the table is a `Record` over it, so an
 * entry that is not classified does not compile. A `wide-only` entry is NOT RENDERED
 * on a narrow viewport (never CSS-hidden), so its probes and lazy chunks do not load.
 * Gating hides the ENTRY, never the feature behind it.
 *
 * Later stages move ids from `wide-only` to `all` together with that destination's
 * narrow layout, and change nothing else here.
 */
export type EntryId =
  // sidebar header / rows
  | 'sidebar.new'
  | 'sidebar.search'
  | 'sidebar.signOut'
  | 'sidebar.sessionMenu'
  | 'sidebar.moveToFolder'
  | 'sidebar.folderReorder'
  // sidebar footer
  | 'sidebar.about'
  | 'sidebar.ontology'
  | 'sidebar.usage'
  | 'sidebar.agents'
  | 'sidebar.webhook'
  | 'sidebar.pipeline'
  | 'sidebar.files'
  | 'sidebar.cron'
  | 'sidebar.memory'
  | 'sidebar.appSettings'
  // composer toolbar
  | 'toolbar.model'
  | 'toolbar.modelOptions'
  | 'toolbar.contextWindow'
  | 'toolbar.runTargetLabel'
  | 'toolbar.runTargetAction'
  | 'toolbar.structuredOutput'
  | 'toolbar.imageOutput'
  | 'toolbar.mcpTools'
  | 'toolbar.skills'
  // composer
  | 'attach.file'
  | 'attach.paint'
  | 'attach.templates'
  | 'composer.voice'
  | 'slash.help'
  | 'slash.cron'
  | 'slash.files'
  // message
  | 'message.copy'
  | 'message.edit'
  | 'message.retry'
  | 'message.regenerate'
  | 'message.branch'
  | 'message.delete'
  | 'message.tts'
  | 'message.memoryLike'
  | 'message.tokenUsage'
  | 'message.saveAsTemplate'
  | 'message.paintEdit'
  | 'message.maskEdit'
  | 'message.workflowDiagram'
  | 'fileLink.open'
  // page header
  | 'header.privacy'
  | 'header.temporary'

export type EntryPolicy = 'all' | 'wide-only'

export const ENTRY_POLICY: Record<EntryId, EntryPolicy> = {
  'sidebar.new': 'all',
  'sidebar.search': 'all',
  'sidebar.signOut': 'all',
  'sidebar.sessionMenu': 'all',
  'sidebar.moveToFolder': 'all',
  'sidebar.folderReorder': 'wide-only',
  'sidebar.about': 'all',
  'sidebar.ontology': 'wide-only',
  // Token Usage Dashboard (PRP-0173): a ~90% modal, an out-of-scope screen on a phone.
  'sidebar.usage': 'wide-only',
  'sidebar.agents': 'wide-only',
  'sidebar.webhook': 'wide-only',
  'sidebar.pipeline': 'wide-only',
  'sidebar.files': 'wide-only',
  'sidebar.cron': 'wide-only',
  'sidebar.memory': 'wide-only',
  'sidebar.appSettings': 'wide-only',
  'toolbar.model': 'all',
  'toolbar.modelOptions': 'all',
  'toolbar.contextWindow': 'all',
  'toolbar.runTargetLabel': 'all',
  // PRP-0176 / UDR-0158 D1: the action exists on both tiers; only its DESTINATION
  // differs (the wide manager modal, or the narrow run-target picker, CTR-0216).
  'toolbar.runTargetAction': 'all',
  'toolbar.structuredOutput': 'wide-only',
  'toolbar.imageOutput': 'wide-only',
  'toolbar.mcpTools': 'wide-only',
  'toolbar.skills': 'wide-only',
  'attach.file': 'all',
  'attach.paint': 'wide-only',
  'attach.templates': 'wide-only',
  'composer.voice': 'all',
  'slash.help': 'wide-only',
  'slash.cron': 'wide-only',
  'slash.files': 'wide-only',
  'message.copy': 'all',
  'message.edit': 'all',
  'message.retry': 'all',
  'message.regenerate': 'all',
  'message.branch': 'all',
  'message.delete': 'all',
  'message.tts': 'all',
  'message.memoryLike': 'all',
  'message.tokenUsage': 'all',
  'message.saveAsTemplate': 'wide-only',
  'message.paintEdit': 'wide-only',
  'message.maskEdit': 'wide-only',
  'message.workflowDiagram': 'wide-only',
  'fileLink.open': 'wide-only',
  'header.privacy': 'all',
  'header.temporary': 'all',
}

/**
 * Builtin slash commands offered on a narrow viewport (UDR-0153 D3). An ALLOWLIST:
 * a builtin added later stays off phones until it is classified here. Prompt- and
 * skill-sourced commands only expand text and are always offered.
 */
export const NARROW_SLASH_BUILTINS: readonly string[] = ['model', 'prompt', 'skill']

export interface ChatSurfaceTier {
  /** True only under the full-page /chat surface (ChatPage provides it). */
  managed: boolean
  narrow: boolean
  touchPrimary: boolean
}

/**
 * Default = today's presentation. Only ChatPage provides a tier, so the compact
 * ChatPanel on /popup and /sidebar (out of PRP-0171 scope) is never affected.
 */
export const WIDE_SURFACE: ChatSurfaceTier = { managed: false, narrow: false, touchPrimary: false }

export const ChatSurfaceTierContext = createContext<ChatSurfaceTier>(WIDE_SURFACE)

export function useChatSurfaceTier(): ChatSurfaceTier {
  return useContext(ChatSurfaceTierContext)
}

export function isEntryVisible(tier: Pick<ChatSurfaceTier, 'narrow'>, id: EntryId): boolean {
  return !tier.narrow || ENTRY_POLICY[id] === 'all'
}

export function useEntryVisible(id: EntryId): boolean {
  return isEntryVisible(useChatSurfaceTier(), id)
}

export function isCommandOfferedOnNarrow(cmd: Pick<CommandEntry, 'source' | 'token'>): boolean {
  return cmd.source !== 'builtin' || NARROW_SLASH_BUILTINS.includes(cmd.token.toLowerCase())
}
