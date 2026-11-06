/**
 * Harness project folders (PRP-0244, CTR-0194 v3, CTR-0197 v3, UDR-0225 D1).
 *
 * A harness conversation works in ONE folder of the coding workspace ("" is the workspace
 * root). The choice belongs to the conversation: it rides every harness message as AG-UI
 * state.harness_project and the backend stores it on the session record (harness_project),
 * so a reopened chat shows it again. A new chat starts at the workspace root (Q2); the five
 * most recently used folders of this browser are one click away (localStorage).
 */
import type { WorkspaceFolderListing } from '@/components/WorkspaceFolderPicker'

export interface ProjectFolderEntry {
  name: string
  /** The rules files the folder holds ("AGENTS.md", "CLAUDE.md"). */
  rules: string[]
}

export interface ProjectFolderListing extends WorkspaceFolderListing {
  entries: ProjectFolderEntry[]
  /** The rules files of the folder shown. */
  rules: string[]
}

/** What applied in a turn (CTR-0219 snapshot `project`); never the rules text. */
export interface HarnessProjectReport {
  root: string
  rules_enabled: boolean
  rules: { name: string; chars: number; truncated: boolean; duplicate?: boolean }[]
}

const RECENT_KEY = 'chatwalaau:harness-recent-projects'
export const MAX_RECENT_PROJECTS = 5

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const body = await res.json()
      const detail = body?.detail
      if (detail && typeof detail === 'object' && detail.message) message = String(detail.message)
      else if (typeof detail === 'string') message = detail
    } catch {
      // keep the status text
    }
    throw new Error(message)
  }
  return (await res.json()) as T
}

export const harnessProjectApi = {
  /** One level of workspace folders (folders only), with their rules files. */
  list: (dir: string) =>
    call<ProjectFolderListing>(`/api/harness-agents/project-folders${dir ? `?dir=${encodeURIComponent(dir)}` : ''}`),
  /** Create one empty folder `name` in `dir`; resolves to its workspace-relative path. */
  create: (dir: string, name: string) =>
    call<{ path: string }>('/api/harness-agents/project-folders', {
      method: 'POST',
      body: JSON.stringify({ dir, name }),
    }).then((r) => r.path),
}

/** The label of a project folder: its path, or "workspace root". */
export function projectLabel(project: string): string {
  return project ? project : 'workspace root'
}

export function getRecentProjects(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY)
    const parsed = raw ? (JSON.parse(raw) as unknown) : []
    return Array.isArray(parsed)
      ? parsed.filter((v): v is string => typeof v === 'string').slice(0, MAX_RECENT_PROJECTS)
      : []
  } catch {
    return []
  }
}

/** Put `project` first in this browser's recent list (at most five, no duplicates). */
export function rememberRecentProject(project: string): string[] {
  const next = [project, ...getRecentProjects().filter((p) => p !== project)].slice(0, MAX_RECENT_PROJECTS)
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    // localStorage may be unavailable; the choice still applies to this conversation.
  }
  return next
}

/** Parse the `project` object of a harness_progress value (CTR-0219 v2). */
export function parseProjectReport(raw: unknown): HarnessProjectReport | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const r = raw as Record<string, unknown>
  if (typeof r.root !== 'string') return undefined
  const rules = Array.isArray(r.rules)
    ? r.rules
        .filter(
          (x): x is Record<string, unknown> =>
            !!x && typeof x === 'object' && typeof (x as Record<string, unknown>).name === 'string',
        )
        .map((x) => ({
          name: String(x.name),
          chars: typeof x.chars === 'number' ? x.chars : 0,
          truncated: x.truncated === true,
          ...(x.duplicate === true ? { duplicate: true } : {}),
        }))
    : []
  return { root: r.root, rules_enabled: r.rules_enabled === true, rules }
}

/** "Rules: AGENTS.md, CLAUDE.md" / "no rules files" / "rules off" for the progress line. */
export function rulesSummary(report: HarnessProjectReport): string {
  if (!report.rules_enabled) return 'rules off'
  const applied = report.rules.filter((r) => !r.duplicate)
  if (applied.length === 0) return 'no rules files'
  return `Rules: ${applied.map((r) => (r.truncated ? `${r.name} (cut)` : r.name)).join(', ')}`
}
