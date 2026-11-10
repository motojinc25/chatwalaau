import type { ReactNode } from 'react'
import { matchRanges } from '@/lib/textMatch'

/**
 * Marks every match of the query inside `text` (UDR-0230 D7).
 *
 * `redacted` BYPASSES the highlight entirely (CTR-0190, UDR-0107 D7). This is
 * not cosmetic: the highlight is placed at the true match offset, so wrapping a
 * slice of already-scrambled text in <mark> would publish exactly WHERE the
 * query matched inside otherwise-illegible content -- the one thing the
 * redaction was meant to hide.
 */
export function HighlightedText({ text, query, redacted }: { text: string; query: string; redacted: boolean }) {
  if (redacted) return <>{text}</>
  const ranges = matchRanges(text, query)
  if (ranges.length === 0) return <>{text}</>

  const parts: ReactNode[] = []
  let at = 0
  for (const { start, end } of ranges) {
    parts.push(
      text.slice(at, start),
      <mark key={start} className="bg-yellow-200 dark:bg-yellow-800">
        {text.slice(start, end)}
      </mark>,
    )
    at = end
  }
  parts.push(text.slice(at))
  return <>{parts}</>
}
