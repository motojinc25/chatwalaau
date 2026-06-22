/**
 * Slash command client logic (CTR-0128 / CTR-0129, PRP-0088, UDR-0066).
 *
 * Pure, dependency-free helpers for: detection (command vs path), completion
 * context resolution, and dynamic argument substitution. Dispatch itself is
 * wired in ChatInput; the AG-UI contract (CTR-0009) is unchanged -- /prompt and
 * /skill expand to ordinary message text (UDR-0066 D1).
 */

export interface CommandEntry {
  token: string
  source: 'builtin' | 'prompt' | 'skill' | string
  description: string
  category: string
  aliases: string[]
  args_hint: string
  ref: string
}

export interface CommandsInventory {
  commands: CommandEntry[]
  collisions: { token: string; sources: string[] }[]
}

/** A command token (and each alias) without the leading slash. */
export const TOKEN_RE = /^[A-Za-z][A-Za-z0-9_-]*$/

/**
 * Strict, on-submit detection (UDR-0066 D3): is the whole input a command?
 * Returns the head token + the raw argument remainder, or null. Registry
 * membership is checked separately by the caller -- this only enforces the
 * syntactic shape, so `/Users/foo/file.md`, `//x`, and `/ x` return null.
 */
export function parseCommandInput(text: string): { token: string; argStr: string } | null {
  const trimmed = text.replace(/^\s+/, '')
  if (!trimmed.startsWith('/') || trimmed.startsWith('//')) return null
  const m = trimmed.match(/^\/([A-Za-z][A-Za-z0-9_-]*)(?:[ \t]+([\s\S]*))?$/)
  if (!m) return null
  return { token: m[1], argStr: m[2] ?? '' }
}

/** Resolve a token (case-insensitive) to a command entry, honoring aliases. */
export function resolveCommand(inv: CommandsInventory | null, token: string): CommandEntry | null {
  if (!inv) return null
  const lower = token.toLowerCase()
  return (
    inv.commands.find((c) => c.token.toLowerCase() === lower || c.aliases.some((a) => a.toLowerCase() === lower)) ??
    null
  )
}

export type CompletionKind = 'command' | 'value' | 'file'

export interface CompletionContext {
  kind: CompletionKind
  /** The partial token being completed. */
  query: string
  /** Start index of `query` within the text (for replacement). */
  start: number
  /** For 'value': the resolved command word (model | skill | prompt | alias). */
  command?: string
}

/**
 * Resolve the completion context at the caret (UDR-0066 D5). Order matters:
 * an `@file` token can appear anywhere (including after command args), so it is
 * checked first; then the head command token; then a first-argument value for
 * model/skill/prompt. Returns null when no completion should be offered.
 */
export function getCompletionContext(text: string, caret: number): CompletionContext | null {
  const head = text.slice(0, caret)

  // @file -- a `@` token preceded by start-of-line or whitespace.
  const fileMatch = head.match(/(^|\s)@(\S*)$/)
  if (fileMatch) {
    const query = fileMatch[2]
    return { kind: 'file', query, start: caret - query.length }
  }

  // Command -- `/word` at the very start, no space yet.
  const cmdMatch = head.match(/^\/([A-Za-z0-9_-]*)$/)
  if (cmdMatch) {
    const query = cmdMatch[1]
    return { kind: 'command', query, start: caret - query.length }
  }

  // Value -- `/command <partial>` while typing the FIRST argument.
  const valMatch = head.match(/^\/([A-Za-z][A-Za-z0-9_-]*)[ \t]+(\S*)$/)
  if (valMatch) {
    const query = valMatch[2]
    return { kind: 'value', query, start: caret - query.length, command: valMatch[1] }
  }

  return null
}

/**
 * Tokenize an argument string: split on whitespace, but a double-quoted span is
 * a SINGLE token so multi-word values are supported (CTR-0129).
 */
export function tokenizeArgs(argStr: string): string[] {
  const tokens: string[] = []
  const re = /"([^"]*)"|(\S+)/g
  let m: RegExpExecArray | null
  // biome-ignore lint/suspicious/noAssignInExpressions: standard regex exec loop
  while ((m = re.exec(argStr)) !== null) {
    tokens.push(m[1] !== undefined ? m[1] : m[2])
  }
  return tokens
}

/**
 * Substitute placeholders in a template/invocation body (CTR-0129):
 *   $0           -> the command word
 *   $1, $2, ...  -> positional args (multi-word via quotes)
 *   ${N}         -> brace form (disambiguates `${1}files`)
 *   $ARGUMENTS   -> the verbatim argument remainder
 *   $$ / \$      -> a literal `$`
 * A missing positional yields the empty string.
 */
export function substituteArguments(body: string, commandWord: string, argStr: string): string {
  const tokens = tokenizeArgs(argStr)
  const positional = [commandWord, ...tokens]
  return body.replace(
    /\$\$|\\\$|\$ARGUMENTS(?![A-Za-z0-9_])|\$\{(\d+)\}|\$(\d+)/g,
    (match, braceIdx: string | undefined, bareIdx: string | undefined) => {
      if (match === '$$' || match === '\\$') return '$'
      if (match.startsWith('$ARGUMENTS')) return argStr
      const idx = Number(braceIdx ?? bareIdx)
      return positional[idx] ?? ''
    },
  )
}

/** The default skill invocation when a skill declares no body template. */
export function defaultSkillInvocation(skillName: string, argStr: string): string {
  const tail = argStr.trim() ? ` ${argStr.trim()}` : ''
  return `Use the ${skillName} skill.${tail}`
}
