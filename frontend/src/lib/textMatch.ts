// Where a search query occurs in displayed text (PRP-0249, UDR-0230 D7). Offsets are computed on
// the displayed string itself, because lower-casing a copy can change its length.

export interface MatchRange {
  start: number
  end: number
}

/** The stored rows are whitespace-normalized, the full text is not: a space matches any white space. */
function queryPattern(query: string): RegExp | null {
  const terms = query.trim().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return null
  return new RegExp(terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+'), 'giu')
}

export function matchRanges(text: string, query: string): MatchRange[] {
  const pattern = queryPattern(query)
  if (!pattern || !text) return []
  return Array.from(text.matchAll(pattern), (m) => ({ start: m.index, end: m.index + m[0].length }))
}

/** A short piece of `text` around its first match, or null when the query does not occur in it. */
export function matchExcerpt(text: string, query: string, before = 30, after = 60): string | null {
  const first = matchRanges(text, query)[0]
  if (!first) return null
  const start = Math.max(0, first.start - before)
  const end = Math.min(text.length, first.end + after)
  return `${start > 0 ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`
}
