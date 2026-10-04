// CTR-0012 v1.12 / PRP-0197 / UDR-0179 D2 -- Heading sections.
//
// react-markdown hands the `h1` / `h2` / `h3` components the heading and its
// inline children only: the paragraphs, lists and code that "belong" to a
// heading are its SIBLINGS. A section can therefore only be folded if it is
// built in the tree first. This plugin wraps each top-level H1 to H3 heading,
// together with everything up to the next top-level heading of the same or a
// higher level, into one `section` element whose FIRST child is the heading.
//
// Rules (UDR-0179 D2):
//   - Only direct children of the hast root are inspected. A heading inside a
//     blockquote, a list item or a table cell never opens a section.
//   - H4 to H6 never open a section; they stay in the enclosing one.
//   - Content before the first heading (a preamble) stays at the root.
//   - Fewer than MIN_FOLD_HEADINGS top-level H1 to H3 headings: the tree is
//     returned unchanged, so a short answer renders exactly as before.
//   - Each heading and its section carry `dataFoldKey` = the heading's 0-based
//     ordinal among the top-level H1 to H3 headings, in document order.
//   - Idempotent: a root that already holds a fold section is left alone.
//
// Pure and synchronous; no dependency (plain hast traversal).

// ----- Minimal local hast type surface (no @types/hast runtime dep) -----

export interface HastNode {
  type: string
  tagName?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
  value?: string
}

/** A message needs at least this many top-level H1 to H3 headings to fold (PRP-0197 Q2). */
export const MIN_FOLD_HEADINGS = 2

const HEADING_LEVELS: Readonly<Record<string, number>> = { h1: 1, h2: 2, h3: 3 }

/** 1 to 3 for an `h1` / `h2` / `h3` element, otherwise 0. */
export function foldHeadingLevel(node: HastNode): number {
  if (node.type !== 'element' || !node.tagName) return 0
  return HEADING_LEVELS[node.tagName] ?? 0
}

function isFoldSection(node: HastNode): boolean {
  return node.type === 'element' && node.tagName === 'section' && node.properties?.dataFoldKey !== undefined
}

interface OpenSection {
  level: number
  section: HastNode
}

/** Rewrite `root.children` into nested fold sections, in place. */
export function sectionizeHeadings(root: HastNode): void {
  const children = root.children ?? []
  if (children.some(isFoldSection)) return
  const headingCount = children.filter((child) => foldHeadingLevel(child) > 0).length
  if (headingCount < MIN_FOLD_HEADINGS) return

  const out: HastNode[] = []
  const stack: OpenSection[] = []
  let ordinal = 0

  const append = (node: HastNode) => {
    const open = stack[stack.length - 1]
    if (open) open.section.children?.push(node)
    else out.push(node)
  }

  for (const child of children) {
    const level = foldHeadingLevel(child)
    if (level === 0) {
      append(child)
      continue
    }
    while (stack.length > 0 && stack[stack.length - 1].level >= level) stack.pop()
    const key = String(ordinal++)
    // The very first element of the message keeps its `first:mt-0` behaviour,
    // which the heading can no longer infer once it is the first child of
    // EVERY section (see FoldableHeading).
    const first = stack.length === 0 && !out.some((node) => node.type === 'element')
    child.properties = { ...child.properties, dataFoldKey: key, ...(first ? { dataFoldFirst: 'true' } : {}) }
    const section: HastNode = {
      type: 'element',
      tagName: 'section',
      properties: { dataFoldKey: key, dataFoldLevel: String(level) },
      children: [child],
    }
    append(section)
    stack.push({ level, section })
  }

  root.children = out
}

/** rehype plugin: add it LAST, and only when folding is on (UDR-0179 D3). */
export default function rehypeHeadingSections() {
  return (tree: HastNode) => {
    sectionizeHeadings(tree)
  }
}
