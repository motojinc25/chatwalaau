/**
 * Fenced code blocks in a message the user sent (PRP-0267, UDR-0248, CTR-0004 v15).
 *
 * The user body is NOT Markdown (CTR-0012 v2): only a CLOSED fenced code block is
 * recognised, so it can be shown as quiet code without its fence lines. Everything
 * else -- headings, lists, emphasis, inline code, indented code -- stays text.
 *
 * Display only (UDR-0248 D3): Copy, Edit, saving and export keep the stored text.
 * This module is pure and builds no HTML; the segments are rendered as React text.
 */

export type UserSegment = { kind: 'text'; value: string } | { kind: 'code'; info: string; value: string }

// CommonMark: up to 3 spaces of indent, then 3+ backticks or 3+ tildes, then an info string.
const OPEN_FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/

function isClosingFence(line: string, char: string, length: number): boolean {
  const match = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(line)
  return match !== null && match[1][0] === char && match[1].length >= length
}

/** Remove up to `indent` leading spaces, as CommonMark does inside a fenced block. */
function stripIndent(line: string, indent: number): string {
  let i = 0
  while (i < indent && line[i] === ' ') i++
  return line.slice(i)
}

/**
 * Split `text` into text and code segments, in order.
 *
 * A fence with no closing fence is not a code block: its opening line and the rest
 * of the message stay text (UDR-0248 D2). Adjacent text lines are joined back with
 * `\n`; `\r\n` is read as a line break.
 */
export function splitUserFences(text: string): UserSegment[] {
  const lines = text.split(/\r?\n/)
  const segments: UserSegment[] = []
  let textLines: string[] = []

  const flushText = () => {
    if (textLines.length > 0) segments.push({ kind: 'text', value: textLines.join('\n') })
    textLines = []
  }

  let i = 0
  while (i < lines.length) {
    const open = OPEN_FENCE.exec(lines[i])
    // A backtick fence's info string may not contain a backtick (CommonMark).
    if (open && !(open[2][0] === '`' && open[3].includes('`'))) {
      const indent = open[1].length
      const char = open[2][0]
      const length = open[2].length
      let close = -1
      for (let j = i + 1; j < lines.length; j++) {
        if (isClosingFence(lines[j], char, length)) {
          close = j
          break
        }
      }
      if (close !== -1) {
        flushText()
        const body = lines.slice(i + 1, close).map((line) => stripIndent(line, indent))
        segments.push({ kind: 'code', info: open[3].trim().split(/\s+/)[0] ?? '', value: body.join('\n') })
        i = close + 1
        continue
      }
      // Unclosed: no later line can close it either, so the rest is text.
      textLines.push(...lines.slice(i))
      break
    }
    textLines.push(lines[i])
    i++
  }
  flushText()
  return segments
}

const BLANK_RUN = /\n[ \t]*\n(?:[ \t]*\n)+/g

/**
 * Tidy a text segment for display (UDR-0248 D4): a run of two or more blank lines
 * shows as one, and blank lines touching a code block are dropped.
 */
export function tidyTextSegment(value: string, beforeCode: boolean, afterCode: boolean): string {
  let out = value.replace(BLANK_RUN, '\n\n')
  if (afterCode) out = out.replace(/^(?:[ \t]*\n)+/, '')
  if (beforeCode) out = out.replace(/(?:\n[ \t]*)+$/, '')
  return out
}
