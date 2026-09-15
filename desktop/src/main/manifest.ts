/**
 * Payload manifest and environment state documents (CTR-0210). Pure module.
 */

import { createHash } from 'node:crypto'
import { win32 } from 'node:path'
import { DesktopError } from './constants'

export interface HashedFile {
  file: string
  sha256: string
}

export interface PayloadManifest {
  schemaVersion: 1
  desktopVersion: string
  backendVersion: string
  sourceCommit: string
  platform: 'win32'
  arch: 'x64'
  runtimeId: string
  python: { version: string; build: string; archive: string; sha256: string }
  uv: { version: string; file: string; sha256: string }
  lock: HashedFile
  wheels: HashedFile[]
  launcher: HashedFile[]
  generatedAt: string
}

export type EnvironmentStatus = 'preparing' | 'ready' | 'broken' | 'retired'

export interface EnvironmentState {
  schemaVersion: 1
  envId: string
  status: EnvironmentStatus
  runtimeId: string
  backendVersion: string
  desktopVersion: string
  envPath: string
  basePython: string
  pythonVersion: string
  uvVersion: string
  lockSha256: string
  createdAt: string
  checkedAt?: string
  error?: string
}

export interface ActiveEnvironment {
  envId: string
  previousEnvId?: string
  launchedOk: boolean
  switchedAt: string
}

export interface IsolationReport {
  executable: string
  prefix: string
  base_prefix: string
  user_site: boolean | null
  purelib: string
  platlib: string
  version: string
}

const SHA256_RE = /^[0-9a-f]{64}$/
const VERSION_RE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/

function fail(message: string): never {
  throw new DesktopError('PAYLOAD_INVALID', `Payload manifest is invalid: ${message}`)
}

function isHashedFile(v: unknown): v is HashedFile {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return typeof o.file === 'string' && o.file.length > 0 && typeof o.sha256 === 'string' && SHA256_RE.test(o.sha256)
}

/** Validate an untrusted parsed manifest. Throws DesktopError(PAYLOAD_INVALID). */
export function validateManifest(raw: unknown): PayloadManifest {
  if (typeof raw !== 'object' || raw === null) fail('not an object')
  const m = raw as Record<string, unknown>
  if (m.schemaVersion !== 1) fail('unsupported schemaVersion')
  for (const key of ['desktopVersion', 'backendVersion'] as const) {
    if (typeof m[key] !== 'string' || !VERSION_RE.test(m[key] as string)) fail(`bad ${key}`)
  }
  if (m.platform !== 'win32' || m.arch !== 'x64') fail('platform/arch must be win32/x64')
  if (typeof m.runtimeId !== 'string' || !/^[A-Za-z0-9._-]+$/.test(m.runtimeId)) fail('bad runtimeId')
  const py = m.python as Record<string, unknown> | undefined
  if (!py || typeof py.archive !== 'string' || typeof py.sha256 !== 'string' || !SHA256_RE.test(py.sha256)) {
    fail('bad python entry')
  }
  const uv = m.uv as Record<string, unknown> | undefined
  if (!uv || typeof uv.file !== 'string' || typeof uv.sha256 !== 'string' || !SHA256_RE.test(uv.sha256)) {
    fail('bad uv entry')
  }
  if (!isHashedFile(m.lock)) fail('bad lock entry')
  if (!Array.isArray(m.wheels) || m.wheels.length === 0 || !m.wheels.every(isHashedFile)) fail('bad wheels')
  if (!Array.isArray(m.launcher) || !m.launcher.every(isHashedFile)) fail('bad launcher')
  const files = [py.archive as string, uv.file as string, (m.lock as HashedFile).file]
  for (const f of [...files, ...(m.wheels as HashedFile[]).map((w) => w.file), ...(m.launcher as HashedFile[]).map((l) => l.file)]) {
    if (win32.isAbsolute(f) || f.split(/[\\/]/).includes('..')) fail(`path escapes the payload: ${f}`)
  }
  return raw as PayloadManifest
}

/** envId = sha256(runtimeId + base lock hash)[:12] (UDR-0151 D4). */
export function computeEnvId(runtimeId: string, lockSha256: string): string {
  return createHash('sha256').update(`${runtimeId}\n${lockSha256}`).digest('hex').slice(0, 12)
}

function samePath(a: string, b: string): boolean {
  return win32.resolve(a).replace(/\\+$/, '').toLowerCase() === win32.resolve(b).replace(/\\+$/, '').toLowerCase()
}

function inside(child: string, parent: string): boolean {
  const rel = win32.relative(win32.resolve(parent).toLowerCase(), win32.resolve(child).toLowerCase())
  return rel === '' || (!rel.startsWith('..') && !win32.isAbsolute(rel))
}

/**
 * Isolation check of a venv generation (RES-0004 section 6.3). Returns the list of failed
 * conditions; empty means isolated. Prefix-based on purpose: a venv interpreter's realpath
 * may point into the base runtime, which is not a failure.
 */
export function verifyIsolation(
  report: IsolationReport,
  expected: { envDir: string; runtimeDir: string; backendVersion: string },
): string[] {
  const failures: string[] = []
  if (!samePath(report.prefix, expected.envDir)) failures.push(`sys.prefix is ${report.prefix}, expected ${expected.envDir}`)
  if (samePath(report.prefix, report.base_prefix)) failures.push('sys.prefix equals sys.base_prefix (not a venv)')
  if (!samePath(report.base_prefix, expected.runtimeDir)) {
    failures.push(`sys.base_prefix is ${report.base_prefix}, expected ${expected.runtimeDir}`)
  }
  if (!inside(report.purelib, expected.envDir)) failures.push(`purelib ${report.purelib} is outside the venv`)
  if (!inside(report.platlib, expected.envDir)) failures.push(`platlib ${report.platlib} is outside the venv`)
  if (report.user_site === true) failures.push('user site-packages is enabled')
  if (report.version !== expected.backendVersion) {
    failures.push(`chatwalaau ${report.version} installed, manifest says ${expected.backendVersion}`)
  }
  return failures
}

function canonicalName(line: string): string | null {
  const t = line.trim()
  if (!t || t.startsWith('#') || t.startsWith('-')) return null
  const m = t.match(/^([A-Za-z0-9][A-Za-z0-9._-]*)/)
  return m ? m[1].toLowerCase().replace(/[-_.]+/g, '-') : null
}

/**
 * Packages present in `current` whose distribution name is absent from `base` -- the
 * user-added inventory of a generation (UDR-0151 D10). Returns the `current` lines as-is.
 */
export function diffFreeze(current: string, base: string): string[] {
  const baseNames = new Set(base.split(/\r?\n/).map(canonicalName).filter((n): n is string => n !== null))
  const added: string[] = []
  for (const line of current.split(/\r?\n/)) {
    const name = canonicalName(line)
    if (name && !baseNames.has(name)) added.push(line.trim())
  }
  return added
}
