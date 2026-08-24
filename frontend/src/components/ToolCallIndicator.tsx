import {
  BookOpen,
  Calendar,
  ChevronRight,
  Clock,
  Cloud,
  File,
  FilePen,
  FileText,
  FolderSearch,
  Globe,
  ImagePlus,
  Layers,
  ListTodo,
  MapPin,
  Network,
  Pencil,
  Plug,
  Search,
  Terminal,
  ToggleLeft,
  Trash2,
  Webhook,
  Wrench,
} from 'lucide-react'
import { useState } from 'react'
import { useMcpToolNames } from '@/hooks/useMcpToolNames'
import type { ToolCall } from '@/types/chat'

interface ToolCallIndicatorProps {
  toolCalls?: ToolCall[]
}

const toolDisplayNames: Record<string, { label: string; doneLabel: string; icon: typeof Globe }> = {
  web_search_preview: { label: 'Searching the web...', doneLabel: 'Searched the web', icon: Globe },
  web_search: { label: 'Searching the web...', doneLabel: 'Searched the web', icon: Globe },
  weather_geocode_city: { label: 'Looking up location...', doneLabel: 'Looked up location', icon: MapPin },
  weather_get_current: {
    label: 'Fetching current weather...',
    doneLabel: 'Fetched current weather',
    icon: Cloud,
  },
  weather_get_forecast: { label: 'Fetching weekly forecast...', doneLabel: 'Fetched weekly forecast', icon: Calendar },
  file_read: { label: 'Reading file...', doneLabel: 'Read file', icon: File },
  file_write: { label: 'Writing file...', doneLabel: 'Wrote file', icon: FilePen },
  bash_execute: { label: 'Executing command...', doneLabel: 'Executed command', icon: Terminal },
  file_glob: { label: 'Searching files...', doneLabel: 'Searched files', icon: FolderSearch },
  file_grep: { label: 'Searching content...', doneLabel: 'Searched content', icon: Search },
  generate_image: { label: 'Generating image...', doneLabel: 'Generated image', icon: ImagePlus },
  edit_image: { label: 'Editing image...', doneLabel: 'Edited image', icon: Pencil },
  rag_search: { label: 'Searching documents...', doneLabel: 'Searched documents', icon: Search },
  // Skills tools (MAF SkillsProvider)
  load_skill: { label: 'Loading skill...', doneLabel: 'Loaded skill', icon: BookOpen },
  read_skill_resource: { label: 'Reading resource...', doneLabel: 'Read resource', icon: FileText },
  run_skill_script: { label: 'Running skill script...', doneLabel: 'Ran skill script', icon: Terminal },

  // ---- Harness agent tools (FEAT-0064, PRP-0145) --------------------------
  // MAF's harness assembles these itself (agent_framework._harness); `run_shell`
  // is ChatWalaʻau's own WorkspaceShellTool (CTR-0193). None of them is an MCP
  // tool, and until PRP-0145 every one of them was labelled "MCP: {name}" with a
  // Plug icon by the fallback branch below -- naming a subsystem that has no
  // authority over the call and sending operators to the wrong screen.
  //
  // Harness FILE MEMORY is deliberately qualified rather than called just
  // "memory": FEAT-0056 / FEAT-0057 already own "Agent Memory" as a user-facing
  // concept with its own editor, and this is a different store under
  // CODING_WORKSPACE_DIR/agent-file-memory.
  file_access_ls: { label: 'Listing workspace files...', doneLabel: 'Listed workspace files', icon: FolderSearch },
  file_access_read: { label: 'Reading workspace file...', doneLabel: 'Read workspace file', icon: File },
  file_access_write: { label: 'Writing workspace file...', doneLabel: 'Wrote workspace file', icon: FilePen },
  file_access_replace: { label: 'Editing workspace file...', doneLabel: 'Edited workspace file', icon: FilePen },
  file_access_replace_lines: { label: 'Editing workspace file...', doneLabel: 'Edited workspace file', icon: FilePen },
  file_access_grep: {
    label: 'Searching workspace content...',
    doneLabel: 'Searched workspace content',
    icon: Search,
  },
  file_access_delete: { label: 'Deleting workspace file...', doneLabel: 'Deleted workspace file', icon: Trash2 },
  file_access_store: { label: 'Storing workspace file...', doneLabel: 'Stored workspace file', icon: FilePen },
  file_memory_ls: { label: 'Listing agent memory...', doneLabel: 'Listed agent memory', icon: FolderSearch },
  file_memory_read: { label: 'Reading agent memory...', doneLabel: 'Read agent memory', icon: BookOpen },
  file_memory_write: { label: 'Saving to agent memory...', doneLabel: 'Saved to agent memory', icon: BookOpen },
  file_memory_replace: { label: 'Updating agent memory...', doneLabel: 'Updated agent memory', icon: BookOpen },
  file_memory_replace_lines: {
    label: 'Updating agent memory...',
    doneLabel: 'Updated agent memory',
    icon: BookOpen,
  },
  file_memory_grep: { label: 'Searching agent memory...', doneLabel: 'Searched agent memory', icon: Search },
  file_memory_delete: { label: 'Deleting agent memory...', doneLabel: 'Deleted agent memory', icon: Trash2 },
  todos_add: { label: 'Adding a task...', doneLabel: 'Added a task', icon: ListTodo },
  todos_complete: { label: 'Completing a task...', doneLabel: 'Completed a task', icon: ListTodo },
  todos_remove: { label: 'Removing a task...', doneLabel: 'Removed a task', icon: ListTodo },
  todos_get_all: { label: 'Reading the task list...', doneLabel: 'Read the task list', icon: ListTodo },
  todos_get_remaining: { label: 'Checking remaining tasks...', doneLabel: 'Checked remaining tasks', icon: ListTodo },
  todos_remaining: { label: 'Checking remaining tasks...', doneLabel: 'Checked remaining tasks', icon: ListTodo },
  todos_remaining_message: {
    label: 'Checking remaining tasks...',
    doneLabel: 'Checked remaining tasks',
    icon: ListTodo,
  },
  mode_get: { label: 'Checking agent mode...', doneLabel: 'Checked agent mode', icon: ToggleLeft },
  mode_set: { label: 'Switching agent mode...', doneLabel: 'Switched agent mode', icon: ToggleLeft },
  // Reuses the bash_execute phrasing: to an operator both are "the agent ran a
  // command", and CTR-0031 vs CTR-0193 ownership is not a distinction this
  // surface needs to publish.
  run_shell: { label: 'Running command...', doneLabel: 'Ran command', icon: Terminal },

  // ---- ChatWalaʻau built-in function tools (CTR-0178, PRP-0145) -----------
  // These run in ORDINARY Prompt-lane chats, so before PRP-0145 an operator with
  // no MCP server configured at all could still be told the agent used MCP.
  manage_cron: { label: 'Managing scheduled jobs...', doneLabel: 'Managed scheduled jobs', icon: Clock },
  manage_pipeline: { label: 'Managing pipeline jobs...', doneLabel: 'Managed pipeline jobs', icon: Layers },
  manage_webhook: { label: 'Managing webhooks...', doneLabel: 'Managed webhooks', icon: Webhook },
  manage_memory: { label: 'Updating agent memory...', doneLabel: 'Updated agent memory', icon: BookOpen },
  manage_user_memory: { label: 'Updating your preferences...', doneLabel: 'Updated your preferences', icon: BookOpen },
  query_ontology: { label: 'Querying the ontology...', doneLabel: 'Queried the ontology', icon: Network },
}

function formatJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

/** Tools whose effective options are worth showing on the collapsed line. */
const PARAMETER_TOOLS = new Set(['generate_image', 'edit_image'])

/**
 * Compact "size=1024x1024 · quality=high · format=png" summary for an image call
 * (CTR-0049 / CTR-0120, v0.117.6).
 *
 * While the call is running the request arguments are all we have; once it returns,
 * the result carries `parameters` -- the options that were actually USED, which can
 * differ from the request when a capability retry dropped one. Showing them inline
 * is what makes it possible to tell a display problem from a resolution problem:
 * previously the indicator said only "Generated image" and the selected options were
 * nowhere to be seen.
 */
function imageParameterSummary(toolCall: ToolCall): string | null {
  if (!PARAMETER_TOOLS.has(toolCall.name)) return null
  let source: Record<string, unknown> | null = null
  if (toolCall.result) {
    try {
      const parsed = JSON.parse(toolCall.result)
      if (parsed && typeof parsed.parameters === 'object' && parsed.parameters) source = parsed.parameters
    } catch {
      // streaming / non-JSON result -> fall back to the arguments below
    }
  }
  if (!source && toolCall.args) {
    try {
      const parsed = JSON.parse(toolCall.args)
      if (parsed && typeof parsed === 'object') {
        const { prompt: _prompt, image_filename: _image, ...rest } = parsed as Record<string, unknown>
        source = rest
      }
    } catch {
      return null
    }
  }
  if (!source) return null
  const parts = Object.entries(source)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${v}`)
  return parts.length > 0 ? parts.join(' · ') : null
}

/**
 * Resolve a tool call to its label + icon (CTR-0013 v8, PRP-0145 / UDR-0125 D8).
 *
 * Ordered, and the order is the point:
 *
 *   1. a curated entry            -> the tool's real subsystem, in its own words
 *   2. the tool IS an MCP tool    -> "MCP: {name}" -- now a VERIFIED claim,
 *                                    checked against the live CTR-0121 inventory
 *   3. otherwise                  -> "Tool: {name}" -- an honest unknown
 *
 * Step 2 used to be the unconditional fallback: anything not in the map was
 * declared MCP without anyone checking. That held while the only unmapped tools
 * were MCP tools, and became false as the harness, the workspace shell, and six
 * CTR-0178 built-ins were added -- so operators were told a subsystem with no
 * authority over the call had produced it.
 *
 * Step 1 wins over step 2 deliberately: if a future MCP server exposes a name
 * that collides with a ChatWalaʻau built-in, the agent called the built-in, and
 * that is what the label reports.
 *
 * Step 3 exists because the old code had no step 3. Once a tool is neither
 * curated nor in the inventory, the only true statement is that a tool ran. A new
 * built-in landing here before someone adds its label is a recoverable
 * oversight; landing in a confident wrong subsystem is the defect being fixed.
 */
function resolveDisplay(name: string, mcpToolNames: Set<string>) {
  const curated = toolDisplayNames[name]
  if (curated) return curated
  if (mcpToolNames.has(name)) {
    return { label: `MCP: ${name}...`, doneLabel: `MCP: ${name}`, icon: Plug }
  }
  return { label: `Tool: ${name}...`, doneLabel: `Tool: ${name}`, icon: Wrench }
}

export function ToolCallBlock({ toolCall }: { toolCall: ToolCall }) {
  const [expanded, setExpanded] = useState(false)
  const isRunning = toolCall.status === 'running'
  const mcpToolNames = useMcpToolNames()
  const display = resolveDisplay(toolCall.name, mcpToolNames)
  const Icon = display.icon
  const label = isRunning ? display.label : display.doneLabel
  const hasDetails = toolCall.args || toolCall.result
  const parameters = imageParameterSummary(toolCall)

  return (
    <div className="mb-1">
      <button
        type="button"
        className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        onClick={() => hasDetails && setExpanded(!expanded)}>
        <Icon className={`h-3.5 w-3.5 shrink-0 ${isRunning ? 'animate-pulse' : ''}`} />
        <span>{label}</span>
        {/* v0.117.6: the effective image options, so what was asked for and what was
            used are both visible without expanding the call. */}
        {parameters && (
          <span className="truncate font-mono text-[0.7rem] text-muted-foreground/70" title={parameters}>
            {parameters}
          </span>
        )}
        {hasDetails && (
          <ChevronRight className={`h-3 w-3 shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`} />
        )}
      </button>
      {expanded && hasDetails && (
        <div className="mt-1 ml-5 max-h-60 overflow-y-auto rounded-md bg-muted/50 p-2.5 text-xs leading-relaxed text-muted-foreground">
          {toolCall.args && (
            <div className="mb-2">
              <div className="mb-1 font-medium text-foreground/70">Arguments</div>
              <pre className="whitespace-pre-wrap break-all font-mono text-[0.7rem]">{formatJson(toolCall.args)}</pre>
            </div>
          )}
          {toolCall.result && (
            <div>
              <div className="mb-1 font-medium text-foreground/70">Result</div>
              <pre className="whitespace-pre-wrap break-all font-mono text-[0.7rem]">{formatJson(toolCall.result)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function ToolCallIndicator({ toolCalls }: ToolCallIndicatorProps) {
  if (!toolCalls || toolCalls.length === 0) return null

  return (
    <div className="mb-2 flex flex-col">
      {toolCalls.map((tc) => (
        <ToolCallBlock key={tc.id} toolCall={tc} />
      ))}
    </div>
  )
}
