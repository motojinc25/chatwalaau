export interface ToolCall {
  id: string
  name: string
  status: 'running' | 'completed'
  args?: string
  result?: string
}

export interface ReasoningBlock {
  id: string
  content: string
  status: 'thinking' | 'done'
}

export interface ImageRef {
  uri: string
  media_type: string
}

export interface UsageInfo {
  input_token_count?: number
  output_token_count?: number
  total_token_count?: number
  max_context_tokens?: number
}

export type ActivityEntry = { type: 'reasoning'; id: string } | { type: 'toolCall'; id: string }

export interface McpAppEvent {
  server_name: string
  tool_name: string
  resource_uri: string
  html_ref: string
  csp?: Record<string, string[]>
  permissions?: Record<string, unknown>
  call_id: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: string
  toolCalls?: ToolCall[]
  reasoningBlocks?: ReasoningBlock[]
  activityLog?: ActivityEntry[]
  images?: ImageRef[]
  usage?: UsageInfo
  mcpApp?: McpAppEvent
  /** Model that generated this assistant message (CTR-0071, PRP-0035) */
  model?: string
}

export interface PromptTemplate {
  id: string
  name: string
  description: string
  category: string
  body: string
  created_at: string
  updated_at: string
}

export interface SessionFolder {
  id: string
  name: string
  created_at: string
  updated_at: string
}

export interface SessionSummary {
  thread_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
  image_count: number
  pinned_at: string | null
  folder_id: string | null
  source?: 'ag-ui' | 'openai-api'
}
