// CTR-0012 v1.12 / PRP-0197 / UDR-0179 -- Collapsible heading sections.
//
// `rehypeHeadingSections` builds the `section` elements; this module renders
// them and holds the fold state.
//
//   - D1 The fold is a pure view state of ONE rendered assistant message: it
//     lives in React state only (no session field, no storage, no request) and
//     never touches `message.content`, so Copy / TTS / Edit / export are
//     unaffected. Reopening the chat shows every section expanded again.
//   - D4 The heading stays an h1 / h2 / h3. The toggle is a SEPARATE chevron
//     button inside it (WAI-ARIA accordion pattern); the heading text is never
//     wrapped in the button, so links and inline code in a heading stay valid
//     and clicking or selecting heading text behaves as before.
//   - D5 A collapsed body is `hidden="until-found"`, so find-in-page still
//     reaches it; the browser fires `beforematch` and the section expands.
//     React 19 types `hidden` as boolean and has no onBeforeMatch prop, so both
//     go through a ref. Browsers without support treat it as plain `hidden`.
//   - D6 Every section starts expanded; a section is keyed by its heading's
//     ordinal. Collapse all folds every section at every level.
//   - D7 The state reaches sections through HeadingFoldContext, never through
//     MarkdownRenderer props, so a toggle never re-runs the memoized
//     remark / rehype / KaTeX pipeline (UDR-0050).

import { ChevronRight } from 'lucide-react'
import {
  Children,
  type ComponentProps,
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react'
import { cn } from '@/lib/utils'

export interface HeadingFoldState {
  /** Per-message prefix so ids never collide between messages on one page. */
  idPrefix: string
  /** Number of fold sections currently rendered in the message. */
  sectionCount: number
  /** True when at least one section exists and every one is collapsed. */
  allCollapsed: boolean
  isCollapsed: (key: string) => boolean
  toggle: (key: string) => void
  expand: (key: string) => void
  collapseAll: () => void
  expandAll: () => void
  /** Called by a rendered section; returns its unregister function. */
  register: (key: string) => () => void
}

export const HeadingFoldContext = createContext<HeadingFoldState | null>(null)

function withKey(set: ReadonlySet<string>, key: string): ReadonlySet<string> {
  if (set.has(key)) return set
  const next = new Set(set)
  next.add(key)
  return next
}

function withoutKey(set: ReadonlySet<string>, key: string): ReadonlySet<string> {
  if (!set.has(key)) return set
  const next = new Set(set)
  next.delete(key)
  return next
}

/** The fold state of one assistant message (owned by ChatMessageItem). */
export function useHeadingFold(): HeadingFoldState {
  const idPrefix = useId()
  const [sectionKeys, setSectionKeys] = useState<ReadonlySet<string>>(() => new Set())
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set())

  const register = useCallback((key: string) => {
    setSectionKeys((prev) => withKey(prev, key))
    return () => {
      setSectionKeys((prev) => withoutKey(prev, key))
      // A section that disappears (an edit) drops its fold state with it.
      setCollapsed((prev) => withoutKey(prev, key))
    }
  }, [])
  const toggle = useCallback(
    (key: string) => setCollapsed((prev) => (prev.has(key) ? withoutKey(prev, key) : withKey(prev, key))),
    [],
  )
  const expand = useCallback((key: string) => setCollapsed((prev) => withoutKey(prev, key)), [])
  const collapseAll = useCallback(() => setCollapsed(new Set(sectionKeys)), [sectionKeys])
  const expandAll = useCallback(() => setCollapsed(new Set()), [])

  return useMemo(() => {
    let allCollapsed = sectionKeys.size > 0
    for (const key of sectionKeys) {
      if (!collapsed.has(key)) {
        allCollapsed = false
        break
      }
    }
    return {
      idPrefix,
      sectionCount: sectionKeys.size,
      allCollapsed,
      isCollapsed: (key: string) => collapsed.has(key),
      toggle,
      expand,
      collapseAll,
      expandAll,
      register,
    }
  }, [idPrefix, sectionKeys, collapsed, toggle, expand, collapseAll, expandAll, register])
}

const bodyId = (prefix: string, key: string) => `${prefix}-fold-body-${key}`
const headingTextId = (prefix: string, key: string) => `${prefix}-fold-heading-${key}`

type FoldSectionProps = ComponentProps<'section'> & { 'data-fold-key'?: string }

/** Renders a `section` built by rehypeHeadingSections: heading + foldable body. */
export function FoldSection({ children, ...props }: FoldSectionProps) {
  const fold = useContext(HeadingFoldContext)
  const foldKey = props['data-fold-key']
  const bodyRef = useRef<HTMLDivElement>(null)
  const collapsed = fold !== null && foldKey !== undefined && fold.isCollapsed(foldKey)
  const register = fold?.register
  const expand = fold?.expand

  useEffect(() => {
    if (!register || foldKey === undefined) return
    return register(foldKey)
  }, [register, foldKey])

  useEffect(() => {
    const el = bodyRef.current
    if (!el) return
    if (collapsed) el.setAttribute('hidden', 'until-found')
    else el.removeAttribute('hidden')
  }, [collapsed])

  useEffect(() => {
    const el = bodyRef.current
    if (!el || !expand || foldKey === undefined) return
    const onBeforeMatch = () => expand(foldKey)
    el.addEventListener('beforematch', onBeforeMatch)
    return () => el.removeEventListener('beforematch', onBeforeMatch)
  }, [expand, foldKey])

  if (!fold || foldKey === undefined) return <section {...props}>{children}</section>

  // rehypeHeadingSections puts the heading FIRST; everything after it is the body.
  const [heading, ...body] = Children.toArray(children)
  return (
    <section {...props}>
      {heading}
      {/* The last element's bottom margin is dropped here as the renderer root
          does for the message; it collapses into the next heading's top margin
          anyway, so spacing between sections is unchanged. */}
      <div ref={bodyRef} id={bodyId(fold.idPrefix, foldKey)} className="[&>*:last-child]:mb-0">
        {body}
      </div>
    </section>
  )
}

type HeadingTag = 'h1' | 'h2' | 'h3'

type FoldableHeadingProps = ComponentProps<'h1'> & {
  as: HeadingTag
  'data-fold-key'?: string
  'data-fold-first'?: string
  children?: ReactNode
}

/**
 * An h1 / h2 / h3 with the CTR-0012 v1.6 classes. When it opens a fold section,
 * a chevron button sits in the gutter to its left (it never shifts the heading
 * text) and an ellipsis marks the collapsed state.
 *
 * `className` is the heading's density classes WITHOUT `first:mt-0`: a fold
 * heading is the first child of every section, so `first:` would strip the top
 * margin of every heading. The plugin marks the message's first element
 * instead (`data-fold-first`).
 */
export function FoldableHeading({ as: Tag, className, children, ...props }: FoldableHeadingProps) {
  const fold = useContext(HeadingFoldContext)
  const foldKey = props['data-fold-key']

  if (!fold || foldKey === undefined) {
    return (
      <Tag className={cn(className, 'first:mt-0')} {...props}>
        {children}
      </Tag>
    )
  }

  const collapsed = fold.isCollapsed(foldKey)
  const first = props['data-fold-first'] === 'true'
  return (
    // A collapsed `until-found` body is still a zero-height box that blocks margin
    // collapsing, so the heading's own bottom margin would add to the next heading's
    // top margin; dropping it keeps the usual gap between headings.
    <Tag className={cn(className, first && 'mt-0', collapsed && 'mb-0', 'group/fold relative')} {...props}>
      <button
        type="button"
        onClick={() => fold.toggle(foldKey)}
        aria-expanded={!collapsed}
        aria-controls={bodyId(fold.idPrefix, foldKey)}
        aria-describedby={headingTextId(fold.idPrefix, foldKey)}
        aria-label={collapsed ? 'Expand section' : 'Collapse section'}
        title={collapsed ? 'Expand section' : 'Collapse section'}
        className={cn(
          // Visually 12 px, inside the avatar gap; the ::after extends the hit area.
          "absolute top-0 -left-3 flex h-[1lh] w-3 items-center justify-center rounded-sm text-muted-foreground transition-opacity after:absolute after:-inset-x-2 after:-inset-y-1 after:content-[''] hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          collapsed ? 'opacity-100' : 'opacity-0 group-hover/fold:opacity-100 [@media(hover:none)]:opacity-60',
        )}>
        <ChevronRight
          aria-hidden="true"
          className={cn('h-3 w-3 transition-transform motion-reduce:transition-none', !collapsed && 'rotate-90')}
        />
      </button>
      <span id={headingTextId(fold.idPrefix, foldKey)}>{children}</span>
      {collapsed && (
        <span aria-hidden="true" className="ml-1.5 font-normal text-muted-foreground">
          …
        </span>
      )}
    </Tag>
  )
}
