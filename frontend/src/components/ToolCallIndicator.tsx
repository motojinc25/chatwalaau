import {
  BookOpen,
  Calendar,
  ChevronRight,
  Cloud,
  File,
  FilePen,
  FileText,
  FolderSearch,
  Globe,
  ImagePlus,
  MapPin,
  Pencil,
  Plug,
  Search,
  Terminal,
} from 'lucide-react'
import { useState } from 'react'
import type { ToolCall } from '@/types/chat'

interface ToolCallIndicatorProps {
  toolCalls?: ToolCall[]
}

const toolDisplayNames: Record<string, { label: string; doneLabel: string; icon: typeof Globe }> = {
  web_search_preview: { label: 'Searching the web...', doneLabel: 'Searched the web', icon: Globe },
  web_search: { label: 'Searching the web...', doneLabel: 'Searched the web', icon: Globe },
  get_coords_by_city: { label: 'Looking up location...', doneLabel: 'Looked up location', icon: MapPin },
  get_current_weather_by_coords: {
    label: 'Fetching current weather...',
    doneLabel: 'Fetched current weather',
    icon: Cloud,
  },
  get_weather_next_week: { label: 'Fetching weekly forecast...', doneLabel: 'Fetched weekly forecast', icon: Calendar },
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
}

function formatJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

export function ToolCallBlock({ toolCall }: { toolCall: ToolCall }) {
  const [expanded, setExpanded] = useState(false)
  const isRunning = toolCall.status === 'running'
  const display = toolDisplayNames[toolCall.name] ?? {
    label: `MCP: ${toolCall.name}...`,
    doneLabel: `MCP: ${toolCall.name}`,
    icon: Plug,
  }
  const Icon = display.icon
  const label = isRunning ? display.label : display.doneLabel
  const hasDetails = toolCall.args || toolCall.result

  return (
    <div className="mb-1">
      <button
        type="button"
        className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        onClick={() => hasDetails && setExpanded(!expanded)}>
        <Icon className={`h-3.5 w-3.5 ${isRunning ? 'animate-pulse' : ''}`} />
        <span>{label}</span>
        {hasDetails && <ChevronRight className={`h-3 w-3 transition-transform ${expanded ? 'rotate-90' : ''}`} />}
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
