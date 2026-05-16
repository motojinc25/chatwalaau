// CTR-0012 v1.7 / PRP-0056 / UDR-0032 -- CJK Strong / Emphasis Rescue.
//
// CommonMark 0.30 / 0.31 declares the closing `**` (or `*`) run to be
// non-right-flanking when it sits between Unicode punctuation (e.g. `)`,
// `.`, `,`) and a non-whitespace non-punctuation character (typical CJK
// letter glued directly to the closing delimiter). That run cannot
// close strong / emphasis per the spec, so micromark leaves the entire
// `**...**` (or `*...*`) span as a raw text node and the user sees
// literal asterisks.
//
// This plugin rewalks mdast `text` nodes that CommonMark gave up on and
// converts the unclosed-by-spec spans into `strong` / `emphasis` nodes.
// It runs LAST in `remarkPlugins` so that any span CommonMark already
// converted into a `strong` / `emphasis` subtree is left untouched (the
// walker only inspects `text` children; structured emphasis subtrees
// are descended into but their leaf text values contain no boundary
// asterisks and so are inert).
//
// Behavior is bound by UDR-0032: idempotent, single-line, asterisk-only
// (no underscore variants), no descent into `inlineCode` / `code` /
// `html` / `link.url`, fixed-on (no user toggle), silent on failure.

// ----- Minimal local mdast type surface (no @types/mdast runtime dep) -----

interface BaseNode {
  type: string
}

interface TextNode extends BaseNode {
  type: 'text'
  value: string
}

interface StrongNode extends BaseNode {
  type: 'strong'
  children: InlineNode[]
}

interface EmphasisNode extends BaseNode {
  type: 'emphasis'
  children: InlineNode[]
}

interface ParentNode extends BaseNode {
  children: ChildNode[]
}

type InlineNode = TextNode | StrongNode | EmphasisNode | BaseNode
type ChildNode = InlineNode | ParentNode

// Parent node types that may NOT have their direct `text` children rescanned.
// `inlineCode` and `code` (fenced) hold their content in `.value` (no
// `children` array) and so are doubly safe; we still gate by parent
// type as defense-in-depth for any future variant.
const NON_ELIGIBLE_PARENT_TYPES = new Set(['inlineCode', 'code', 'html', 'yaml', 'toml', 'definition'])

// Patterns: strict CommonMark-failing rescue.
//   - Inner content must NOT start or end with whitespace or `*`.
//   - Inner content MUST be single-line (no `\n`).
//   - Inner content length >= 1 char (the boundary class itself satisfies this;
//     the optional middle covers >= 2-char cases).
//   - For STRONG, the inner content MUST NOT contain `**` (a single `*`
//     IS allowed in the middle, matching UDR-0032 rule 5).
//   - For EMPHASIS, the inner content MUST NOT contain `*`.
//
//   Pattern shape:
//     STRONG    /\*\*( head (?: middle tail )? )\*\*/
//     EMPHASIS  /\*  ( head (?: middle tail )? )\*  /
//     head/tail  = [^\s*]   (non-whitespace non-`*`)
//     STRONG middle   = (?:(?!\*\*)[^\n])*    (no `**` substring, no \n)
//     EMPHASIS middle = [^*\n]*               (no `*`,         no \n)
const STRONG_PATTERN = /\*\*([^\s*](?:(?:(?!\*\*)[^\n])*[^\s*])?)\*\*/g
const EMPHASIS_PATTERN = /\*([^\s*](?:[^*\n]*[^\s*])?)\*/g

function findMatches(re: RegExp, text: string): Array<{ start: number; end: number; inner: string }> {
  re.lastIndex = 0
  const matches: Array<{ start: number; end: number; inner: string }> = []
  let m: RegExpExecArray | null
  // biome-ignore lint/suspicious/noAssignInExpressions: idiomatic regex exec loop
  while ((m = re.exec(text)) !== null) {
    matches.push({ start: m.index, end: m.index + m[0].length, inner: m[1] })
  }
  return matches
}

function splitByMatches(
  text: string,
  matches: Array<{ start: number; end: number; inner: string }>,
  wrap: (inner: string) => InlineNode,
): InlineNode[] {
  if (matches.length === 0) return [{ type: 'text', value: text }]
  const out: InlineNode[] = []
  let cursor = 0
  for (const m of matches) {
    if (m.start > cursor) out.push({ type: 'text', value: text.slice(cursor, m.start) })
    out.push(wrap(m.inner))
    cursor = m.end
  }
  if (cursor < text.length) out.push({ type: 'text', value: text.slice(cursor) })
  return out
}

// rewriteText is exported for unit testing in isolation.
export function rewriteText(value: string): InlineNode[] {
  // Pass 1: strong (**...**) replaces matching slices with `strong` nodes.
  const strongMatches = findMatches(STRONG_PATTERN, value)
  const afterStrong = splitByMatches(value, strongMatches, (inner) => ({
    type: 'strong',
    children: [{ type: 'text', value: inner }],
  }))

  // Pass 2: emphasis (*...*) runs only on the leftover `text` segments
  // so strong boundary asterisks are not eaten.
  const result: InlineNode[] = []
  for (const node of afterStrong) {
    if (node.type !== 'text') {
      result.push(node)
      continue
    }
    const textNode = node as TextNode
    const emMatches = findMatches(EMPHASIS_PATTERN, textNode.value)
    if (emMatches.length === 0) {
      result.push(textNode)
      continue
    }
    const expanded = splitByMatches(textNode.value, emMatches, (inner) => ({
      type: 'emphasis',
      children: [{ type: 'text', value: inner }],
    }))
    for (const e of expanded) result.push(e)
  }
  return result
}

function isParent(node: BaseNode): node is ParentNode {
  return Array.isArray((node as ParentNode).children)
}

function isText(node: BaseNode): node is TextNode {
  return node.type === 'text' && typeof (node as TextNode).value === 'string'
}

function walk(node: BaseNode): void {
  if (!isParent(node)) return
  if (NON_ELIGIBLE_PARENT_TYPES.has(node.type)) return

  const children = node.children
  let i = 0
  while (i < children.length) {
    const child = children[i]
    if (isText(child)) {
      const replacement = rewriteText(child.value)
      // No-op when the rewrite produced exactly the original single text node.
      const isNoop =
        replacement.length === 1 && replacement[0].type === 'text' && (replacement[0] as TextNode).value === child.value
      if (!isNoop) {
        children.splice(i, 1, ...(replacement as ChildNode[]))
        i += replacement.length
        continue
      }
    } else {
      // Recurse only into structural parents; non-parent nodes are leaves
      // (e.g., break, thematicBreak, image, html, inlineCode without
      // children) and have nothing to rescan.
      walk(child)
    }
    i += 1
  }
}

// Unified-style plugin: returns a transformer that mutates the tree in place.
// Idempotent and silent-on-failure per UDR-0032.
export function remarkCjkStrongEmphasisRescue() {
  return (tree: BaseNode): void => {
    try {
      walk(tree)
    } catch {
      // UDR-0032 rule 7: silent on failure -- never throw user-visible
      // exceptions from a rendering-side plugin.
    }
  }
}
