/**
 * Privacy Screen redaction (CTR-0190, PRP-0124 / UDR-0107 D2-D4).
 *
 * Substitutes the string a component hands to JSX. This is deliberately NOT a
 * CSS treatment (`filter: blur`, `opacity`, `color: transparent`): a visual
 * overlay leaves the plaintext in the DOM, where range-select-and-copy, in-page
 * find, DevTools, extensions, and the accessibility tree all still read it, and
 * a weak blur is recoverable from a screenshot (UDR-0107 D2).
 *
 * What the substitution preserves, and why (UDR-0107 D3):
 *
 *   preserved: newlines, and the glyph WIDTH class (narrow ASCII/halfwidth vs
 *              wide CJK/fullwidth/emoji). ONLY so the redacted block occupies
 *              the same box as the plaintext -- otherwise toggling Privacy
 *              Screen reflows the page and the presenter loses their place.
 *              Japanese also stays visually Japanese, which reads as "masked",
 *              not as "corrupted".
 *
 *   NOT preserved: intra-line spaces and tabs (they publish word boundaries),
 *              the character class -- digit vs letter vs punctuation (preserving
 *              it publishes "this is an amount / a date / a phone number", the
 *              structure most worth hiding), and letter case (it publishes
 *              acronyms and proper-noun positions).
 *
 * The output does not depend on the plaintext at all beyond its newline and
 * width structure: the glyph stream is derived from (salt, key) alone. Two
 * elements holding identical plaintext under different keys therefore scramble
 * differently, and the same plaintext scrambles differently after a new
 * activation mints a new salt (UDR-0107 D4).
 */

/**
 * Narrow-glyph pool. Mixed letters and digits so the output reads as text
 * rather than as a cipher dump; visually confusable characters (l/I/1, O/0) are
 * left in on purpose -- there is nothing to read, so legibility is not a goal.
 */
const NARROW_POOL = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

/**
 * Wide-glyph pool: hiragana, katakana, and common kanji. Keeps CJK text looking
 * like CJK text at the same advance width.
 */
const WIDE_POOL =
  'あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん' +
  'アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン' +
  '日月火水木金土年時分社名前会議書類情報資料本部長様件先方今後対応確認連絡担当課題進行'

/**
 * East Asian Wide / Fullwidth code point ranges, as [start, end] pairs.
 *
 * A pragmatic subset, not the full Unicode EastAsianWidth table: it covers the
 * scripts and symbol blocks that actually appear in chat content (CJK, kana,
 * Hangul, fullwidth forms, emoji). Anything unlisted is treated as narrow --
 * the failure mode is a slightly narrower replacement glyph, never a leak.
 */
const WIDE_RANGES: ReadonlyArray<readonly [number, number]> = [
  [0x1100, 0x115f], // Hangul Jamo
  [0x2e80, 0x303e], // CJK Radicals, Kangxi, CJK Symbols and Punctuation
  [0x3041, 0x33ff], // Hiragana, Katakana, Bopomofo, Hangul Compat Jamo, CJK Compat
  [0x3400, 0x4dbf], // CJK Unified Ideographs Extension A
  [0x4e00, 0x9fff], // CJK Unified Ideographs
  [0xa000, 0xa4cf], // Yi
  [0xac00, 0xd7a3], // Hangul Syllables
  [0xf900, 0xfaff], // CJK Compatibility Ideographs
  [0xfe30, 0xfe6f], // CJK Compatibility Forms, Small Form Variants
  [0xff00, 0xff60], // Fullwidth Forms
  [0xffe0, 0xffe6], // Fullwidth Signs
  [0x1f300, 0x1faff], // Emoji and pictographs
  [0x20000, 0x3fffd], // CJK Unified Ideographs Extensions B and beyond
]

function isWide(codePoint: number): boolean {
  for (const [start, end] of WIDE_RANGES) {
    if (codePoint >= start && codePoint <= end) return true
  }
  return false
}

/** FNV-1a over a string, for seeding the PRNG from (salt, key). */
function seed32(input: string): number {
  let hash = 2166136261
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

/**
 * mulberry32: a small, fast, seedable PRNG.
 *
 * `Math.random()` is unusable here -- the same (text, key, salt) must yield the
 * same output every call, or the redacted text would change on every React
 * re-render and read as a defect (UDR-0107 D4).
 */
function mulberry32(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let t = Math.imul(state ^ (state >>> 15), 1 | state)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/**
 * Mint a fresh redaction salt. Called on each Privacy Screen activation, so the
 * same plaintext scrambles differently in a later activation (UDR-0107 D4).
 */
export function newRedactionSalt(): string {
  return crypto.randomUUID()
}

/**
 * Replace every character of `text` with a random glyph of the same width
 * class, preserving newlines only.
 *
 * Pure: identical `(text, key, salt)` always returns identical output.
 *
 * @param text  the plaintext to redact
 * @param key   a STABLE per-element identity (thread_id / message.id /
 *              folder.id, plus a field discriminator) -- never the plaintext
 * @param salt  the activation salt from `newRedactionSalt()`
 */
export function redactText(text: string, key: string, salt: string): string {
  if (!text) return text

  const random = mulberry32(seed32(`${salt}|${key}`))
  let out = ''

  // Iterate by CODE POINT, not by UTF-16 unit: indexing a string would split
  // astral pairs (emoji, CJK extension B) into lone surrogates.
  for (const char of text) {
    if (char === '\n' || char === '\r') {
      out += char
      continue
    }
    const codePoint = char.codePointAt(0) ?? 0
    const pool = isWide(codePoint) ? WIDE_POOL : NARROW_POOL
    out += pool[Math.floor(random() * pool.length)]
  }

  return out
}
