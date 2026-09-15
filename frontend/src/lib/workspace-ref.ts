/**
 * Workspace File Reference (CTR-0207, PRP-0166, UDR-0150 D1/D2/D4).
 *
 * A file the agent made is referenced in assistant Markdown as
 * `workspace:<workspace-relative POSIX path>`. `sandbox:<path>` -- the link form
 * of OpenAI-hosted code sandboxes, carried in by skills written for that host --
 * is accepted as an INPUT alias and normalized to the same path (D2).
 *
 * This module is dependency-free on purpose: it is the ONE client-side
 * implementation of the grammar, and tests/invariants/test_prp0166_*.py runs it
 * against the SAME case table (tests/fixtures/workspace_refs.json) as the Python
 * mirror in backend app.workspace.refs, so the two cannot drift.
 *
 * Client-side rejection is UX, not security: the server jail (CTR-0031, reached
 * through CTR-0136 /raw) remains the only authority on what is in the workspace.
 */

const REF_SCHEMES = ['workspace:', 'sandbox:'] as const

/** File extensions the File Explorer opens in a viewer tab (mirrors its tabKind). */
const PREVIEW_EXTS = new Set(['pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'avif'])

function schemeOf(url: string): string | undefined {
  const lower = url.toLowerCase()
  return REF_SCHEMES.find((s) => lower.startsWith(s))
}

/** True when `url` uses a workspace-reference scheme, whether or not its path is valid. */
export function isWorkspaceRefUrl(url: string | null | undefined): boolean {
  return !!url && schemeOf(url) !== undefined
}

/**
 * Normalize a reference to a workspace-relative POSIX path, or return null.
 *
 * Tolerated: a leading `/`, `//` or `./` (stripped), `\` (converted to `/`), and
 * percent-escapes (decoded once). Rejected: an empty path, a `..` segment, a
 * drive letter, a NUL, and a malformed escape. Any other URL is not a reference.
 */
export function normalizeWorkspaceRef(url: string | null | undefined): string | null {
  if (!url) return null
  const scheme = schemeOf(url)
  if (!scheme) return null
  let raw = url.slice(scheme.length)
  try {
    raw = decodeURIComponent(raw)
  } catch {
    return null
  }
  if (raw.includes('\0')) return null
  raw = raw.replace(/\\/g, '/').replace(/^(?:\.?\/)+/, '')
  if (/^[A-Za-z]:/.test(raw)) return null
  const segments = raw.split('/').filter((s) => s !== '' && s !== '.')
  if (segments.length === 0 || segments.includes('..')) return null
  return segments.join('/')
}

// A URL scheme (`https:`, `mailto:`, and also a Windows drive letter `C:`).
const HAS_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/
// A final segment that names a file: `.pdf`, `.png`, `.tar.gz` -> yes; `readme` -> no.
const HAS_EXTENSION = /\.[A-Za-z0-9]{1,10}$/

/**
 * Normalize an explicit link / image TARGET to a workspace path, or return null
 * (CTR-0207 v2, PRP-0168, UDR-0150 D9).
 *
 * Extends the scheme forms with the shape a model most often writes when it
 * ignores the guidance: a RELATIVE path in a Markdown link, such as
 * `[PDF](output/pdf/hello_world.pdf)`. That is safe to adopt because a relative
 * link has no other meaning in a chat answer -- the browser would resolve it
 * against `/chat` and 404 -- so the choice is between a control and a dead link.
 *
 * Deliberately NOT adopted, each because it has another meaning or cannot be
 * resolved here: a ROOTED path (`/api/uploads/...` is a real app route), any
 * scheme (`https:`, `file:`, `mailto:`), a Windows absolute path (the SPA does
 * not know the workspace's absolute path), a query or fragment, and a target
 * whose last segment has no extension. Bare paths in PROSE are still never
 * linked -- this applies only to a target the model wrote as a link.
 */
export function normalizeWorkspaceLinkTarget(url: string | null | undefined): string | null {
  if (!url) return null
  if (isWorkspaceRefUrl(url)) return normalizeWorkspaceRef(url)
  const raw = url.trim()
  if (!raw || HAS_SCHEME.test(raw) || raw.startsWith('/') || raw.includes('?') || raw.includes('#')) return null
  let decoded: string
  try {
    decoded = decodeURIComponent(raw)
  } catch {
    return null
  }
  if (decoded.includes('\0')) return null
  const cleaned = decoded.replace(/\\/g, '/').replace(/^(?:\.\/)+/, '')
  if (cleaned.startsWith('/')) return null
  const segments = cleaned.split('/').filter((s) => s !== '' && s !== '.')
  if (segments.length === 0 || segments.includes('..')) return null
  if (!HAS_EXTENSION.test(segments[segments.length - 1])) return null
  return segments.join('/')
}

/** The file name of a normalized reference. */
export function workspaceRefName(path: string): string {
  const i = path.lastIndexOf('/')
  return i < 0 ? path : path.slice(i + 1)
}

/** True when the File Explorer can show this file in a viewer tab (PDF / image). */
export function isPreviewableRef(path: string): boolean {
  const name = workspaceRefName(path)
  const i = name.lastIndexOf('.')
  return i >= 0 && PREVIEW_EXTS.has(name.slice(i + 1).toLowerCase())
}

/**
 * Wrap react-markdown's URL transform so exactly the two reference schemes survive.
 *
 * Every other URL keeps the library default, which is what blanks `javascript:`.
 * A preserved reference is NEVER written into an href / src (UDR-0150 D4): the
 * renderer dispatches it to WorkspaceFileLink, which resolves it by fetch.
 */
export function makeWorkspaceUrlTransform(fallback: (url: string) => string): (url: string) => string {
  return (url: string) => (isWorkspaceRefUrl(url) ? url : fallback(url))
}
