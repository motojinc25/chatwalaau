/**
 * Ontology history: the shapes of the history / diff / trash API and the labels the
 * History tab shows (CTR-0171 v7, CTR-0173 v7, PRP-0202 / UDR-0184).
 *
 * Pure (type-only imports), so the invariant suite can run it under Node.
 */

import type { GraphTerm, Term } from './ontologyModel'

/** One version: `current` or a backup file name, labelled with the write that produced it. */
export interface HistoryVersion {
  version: string
  revision: string | null
  kind: 'create' | 'import' | 'save' | 'statements' | 'restore' | 'delete' | null
  created_at: string | null
  replaced_at: string | null
  added: number | null
  removed: number | null
  restored_from: string | null
  bytes: number
  format: 'turtle' | 'trig'
  legacy: boolean
}

export interface DiffStatement {
  s: Term
  p: string
  o: Term
  g?: GraphTerm
}

export interface VersionDiff {
  version: string
  against: string | null
  added_count: number
  removed_count: number
  added: DiffStatement[]
  removed: DiffStatement[]
  document: {
    prefixes_added: { prefix: string; iri: string }[]
    prefixes_removed: { prefix: string; iri: string }[]
    base?: [string | null, string | null]
    version?: [string | null, string | null]
  }
  offset: number
  limit: number
}

export interface DeletedOntology {
  id: string
  name: string
  description: string
  deleted_at: string
  expires_at: string | null
  version: string
  bytes: number
  format: 'turtle' | 'trig'
  legacy: boolean
}

export const CURRENT_VERSION = 'current'
export const DIFF_PAGE = 200

const STAMP_RE = /\.bak-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})\d{6}$/

/** The UTC time in a backup name as an ISO string, or null. */
export function backupTime(version: string): string | null {
  const m = STAMP_RE.exec(version)
  return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z` : null
}

/** When a version came to be (its producing write), else when it was replaced. */
export function versionTime(v: HistoryVersion): string | null {
  return v.created_at ?? v.replaced_at ?? (v.version === CURRENT_VERSION ? null : backupTime(v.version))
}

function changes(v: HistoryVersion): number | null {
  return v.added === null || v.removed === null ? null : v.added + v.removed
}

/** What produced a version, in words. */
export function describeVersion(v: HistoryVersion, formatTime: (iso: string) => string = (iso) => iso): string {
  if (v.kind === null) return v.version === CURRENT_VERSION ? 'Current version' : 'Earlier version'
  switch (v.kind) {
    case 'create':
      return 'Created'
    case 'import':
      return 'Imported'
    case 'statements': {
      const n = changes(v)
      return n === null ? 'Saved' : `Saved ${n} change${n === 1 ? '' : 's'}`
    }
    case 'restore': {
      const from = v.restored_from ? backupTime(v.restored_from) : null
      return from ? `Restored the version of ${formatTime(from)}` : 'Restored'
    }
    case 'delete':
      return 'Deleted'
    default:
      return 'Saved'
  }
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** The confirmation line before a restore. */
export function restoreSummary(diff: Pick<VersionDiff, 'added_count' | 'removed_count'>): string {
  const added = `${diff.added_count} statement${diff.added_count === 1 ? '' : 's'} will be added`
  const removed = `${diff.removed_count} removed`
  return `${added} and ${removed}.`
}
