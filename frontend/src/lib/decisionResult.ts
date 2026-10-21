/**
 * decision_evaluate results (PRP-0219, CTR-0248, UDR-0201 D7).
 *
 * Parses the tool's normalized JSON into table rows for the chat (operator Q5). Pure, so
 * the reading rules are testable without rendering. The tool never applies a threshold;
 * the rows only PRESENT the probabilities: a `noul` answer reads "Yes" when P(true) is at
 * least 0.5, with the probability of the side shown, and every row carries the top
 * probability so a low-confidence answer can be marked.
 */

export const DECISION_EVALUATE_TOOL = 'decision_evaluate'

/** Below this top probability a row is marked as uncertain (presentation only). */
export const LOW_CONFIDENCE = 0.6

export interface DecisionRow {
  name: string
  type: 'noul' | 'choice' | 'score' | string
  /** What the answer is, in words. */
  answer: string
  /** Probability of the shown answer, 0..1, when known. */
  confidence: number | null
  /** Secondary detail: other options, the score position, or the raw answer. */
  detail: string
  /** True when the answer could not be read (`raw`). */
  unreadable: boolean
}

export interface DecisionTable {
  model: string | null
  demo: boolean
  rows: DecisionRow[]
}

const pct = (p: number) => `${Math.round(p * 100)}%`

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function rowFor(name: string, answer: Record<string, unknown>): DecisionRow {
  const type = typeof answer.type === 'string' ? answer.type : '?'
  if ('raw' in answer) {
    return {
      name,
      type,
      answer: 'Unreadable answer',
      confidence: null,
      detail: JSON.stringify(answer.raw ?? null),
      unreadable: true,
    }
  }
  if (type === 'noul' && typeof answer.probability === 'number') {
    const p = answer.probability
    const yes = p >= 0.5
    return {
      name,
      type,
      answer: yes ? 'Yes' : 'No',
      confidence: yes ? p : 1 - p,
      detail: `P(true) = ${p.toFixed(2)}`,
      unreadable: false,
    }
  }
  if (type === 'choice' && typeof answer.choice === 'string') {
    const probs = isRecord(answer.probabilities) ? answer.probabilities : {}
    const top = typeof probs[answer.choice] === 'number' ? (probs[answer.choice] as number) : null
    const others = Object.entries(probs)
      .filter(([label, p]) => label !== answer.choice && typeof p === 'number')
      .map(([label, p]) => `${label} ${pct(p as number)}`)
    return {
      name,
      type,
      answer: answer.choice,
      confidence: top,
      detail: others.join(', '),
      unreadable: false,
    }
  }
  if (type === 'score' && typeof answer.score === 'number') {
    const levels = typeof answer.levels === 'number' ? answer.levels : null
    const level = typeof answer.nearest_level === 'number' ? answer.nearest_level : null
    const label = typeof answer.nearest_label === 'string' ? answer.nearest_label : ''
    const probs = Array.isArray(answer.probabilities) ? answer.probabilities : null
    const levelP = probs && level !== null && typeof probs[level] === 'number' ? (probs[level] as number) : null
    const position = levels !== null ? `score ${answer.score.toFixed(2)} on 0..${levels - 1}` : `score ${answer.score}`
    return {
      name,
      type,
      answer: level !== null ? `${label} (level ${level})` : label || String(answer.score),
      confidence: levelP,
      detail: position,
      unreadable: false,
    }
  }
  return { name, type, answer: 'Unreadable answer', confidence: null, detail: JSON.stringify(answer), unreadable: true }
}

/** The table for one decision_evaluate result, or null for an error / non-JSON result. */
export function parseDecisionResult(result: string | undefined): DecisionTable | null {
  if (!result) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(result)
  } catch {
    return null
  }
  if (!isRecord(parsed) || 'error' in parsed || !isRecord(parsed.answers)) return null
  const rows = Object.entries(parsed.answers)
    .filter((entry): entry is [string, Record<string, unknown>] => isRecord(entry[1]))
    .map(([name, answer]) => rowFor(name, answer))
  if (rows.length === 0) return null
  return {
    model: typeof parsed.model === 'string' ? parsed.model : null,
    demo: parsed.demo === true,
    rows,
  }
}
