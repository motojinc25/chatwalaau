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
  | 'sidebar.mcpTools'
  | 'sidebar.skills'
  // composer control row (CTR-0221)
  | 'toolbar.runTargetAction'
  // composer
  | 'attach.file'
  | 'attach.paint'
  | 'attach.templates'
  | 'composer.voice'
  | 'composer.live'
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
  // PRP-0185 / UDR-0167 D13: MOVED here from the composer toolbar, and wide-only like
  // every other management launcher in this footer -- a ~90% modal is an out-of-scope
  // screen on a phone (UDR-0153 D3). The move also takes them off /popup and /sidebar,
  // which mount ChatPanel but not SessionSidebar; the capabilities themselves and their
  // APIs (CTR-0121 / CTR-0123) are untouched on every surface.
  'sidebar.mcpTools': 'wide-only',
  'sidebar.skills': 'wide-only',
  // PRP-0184 / UDR-0166 D1: `toolbar.model`, `toolbar.modelOptions` and
  // `toolbar.structuredOutput` are GONE from this table with the controls they gated.
  // Model, reasoning effort and structured output are configured on the run-target, so
  // there is no composer entry left to admit or withhold per tier.
  // PRP-0186 / UDR-0168: `toolbar.contextWindow` and `toolbar.runTargetLabel` are GONE
  // from this table with the strip they described. Both had ALREADY been dead --
  // declared here and referenced nowhere -- and the strip they belonged to no longer
  // exists: occupancy is the composer's outline state and the run-target is an icon in
  // its control row.
  //
  // The action survives and is still `all`, but it now gates the composer's run-target
  // ICON rather than a labelled chip above the composer, and its destination no longer
  // depends on the tier (UDR-0168 D7 amends UDR-0158 D1).
  'toolbar.runTargetAction': 'all',
  // PRP-0185 / UDR-0167 D11/D13: `toolbar.imageOutput`, `toolbar.mcpTools` and
  // `toolbar.skills` are GONE from this table with the controls they gated, exactly as
  // PRP-0184 removed the model / options / structured-output entries. Image output
  // options are the Built-in agent's configuration (Core agent card) and the two
  // managers are sidebar-footer entries, so there is no composer entry left to admit
  // or withhold per tier.
  'attach.file': 'all',
  'attach.paint': 'wide-only',
  'attach.templates': 'wide-only',
  'composer.voice': 'all',
  // PRP-0188 (UDR-0170 D11): Live conversation is offered on phones too; the secure-
  // context, server, run-target and /chat-only checks are the caller's (CTR-0228).
  'composer.live': 'all',
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
