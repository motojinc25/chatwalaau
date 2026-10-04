// CTR-0168 (v0.174.0 defect fix, UDR-0081 amendment) -- which message a step
// starts from.
//
// A step lands the target's top exactly SCROLL_TOP_OFFSET_PX below the scroll
// container's top. Messages are stacked with no gap, so the message ABOVE the
// target then ends exactly on that line. Read as "the topmost message touching
// the reading line", that edge-adjacent message would be taken as the current
// one, and every following Previous skipped a message (Next needed two clicks).
// A message therefore counts only when it reaches more than STEP_EDGE_SLACK_PX
// past the line, which also absorbs sub-pixel scroll positions.
//
// Pure (no DOM, no React) so the rule can be tested on its own.

/** Slack (px) below the reading line before a message counts as "at" it. */
export const STEP_EDGE_SLACK_PX = 4

/** A message's vertical extent, relative to the scroll container's top edge. */
export interface MessageSpan {
  top: number
  bottom: number
}

/**
 * Index of the message the user is reading: the first (document order) whose
 * bottom lies more than `slackPx` below the reading line at `offsetPx`. When
 * every message ends above it, the last one. -1 for no messages.
 */
export function readingIndex(
  spans: readonly MessageSpan[],
  offsetPx: number,
  slackPx: number = STEP_EDGE_SLACK_PX,
): number {
  if (spans.length === 0) return -1
  const line = offsetPx + slackPx
  const index = spans.findIndex((span) => span.bottom > line)
  return index === -1 ? spans.length - 1 : index
}
