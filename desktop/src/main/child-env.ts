/**
 * Constructed child-process environment (CTR-0209, UDR-0151 D6).
 *
 * The backend (and everything it spawns) receives an environment BUILT from an allowlist,
 * never the Desktop's inherited one passed through. Pure module.
 */

import { win32 } from 'node:path'

export interface ChildEnvOptions {
  /** Active venv generation. */
  envDir: string
  /** Directory holding the bundled uv.exe (omitted in dev mode). */
  toolsDir?: string
  uvCacheDir: string
  pipCacheDir: string
  tmpDir: string
  /** Base freeze used as a best-effort constraint for later installs. */
  constraintsFile?: string
}

/** Variables passed through unchanged (case-insensitive on Windows). */
export const PASS_THROUGH = [
  'SystemRoot',
  'SystemDrive',
  'windir',
  'ComSpec',
  'PATHEXT',
  'USERPROFILE',
  'APPDATA',
  'LOCALAPPDATA',
  'HOMEDRIVE',
  'HOMEPATH',
  'HOME',
  'USERNAME',
  'USERDOMAIN',
  'COMPUTERNAME',
  'ProgramData',
  'ProgramFiles',
  'ProgramFiles(x86)',
  'ProgramW6432',
  'CommonProgramFiles',
  'CommonProgramFiles(x86)',
  'CommonProgramW6432',
  'PUBLIC',
  'ALLUSERSPROFILE',
  'OS',
  'PROCESSOR_ARCHITECTURE',
  'PROCESSOR_IDENTIFIER',
  'NUMBER_OF_PROCESSORS',
  'LANG',
  'TZ',
  'HTTP_PROXY',
  'HTTPS_PROXY',
  'NO_PROXY',
  'SSL_CERT_FILE',
  'SSL_CERT_DIR',
  'REQUESTS_CA_BUNDLE',
  'CURL_CA_BUNDLE',
]

/** Prefixes passed through (provider credentials for Entra / Azure SDKs). */
export const PASS_THROUGH_PREFIXES = ['AZURE_']

/** Executables whose presence marks a PATH entry as a foreign Python installation. */
export const FOREIGN_PYTHON_MARKERS = ['python.exe', 'python3.exe', 'pip.exe', 'pip3.exe', 'conda.exe']

export type ForeignPythonDirPredicate = (dir: string) => boolean

export function buildChildEnv(
  inherited: Record<string, string | undefined>,
  opts: ChildEnvOptions,
  isForeignPythonDir: ForeignPythonDirPredicate = () => false,
): Record<string, string> {
  const out: Record<string, string> = {}
  const pass = new Set(PASS_THROUGH.map((k) => k.toLowerCase()))
  const prefixes = PASS_THROUGH_PREFIXES.map((k) => k.toLowerCase())

  const inheritedPath: string[] = []
  for (const [key, value] of Object.entries(inherited)) {
    if (value === undefined) continue
    const lower = key.toLowerCase()
    if (lower === 'path') {
      inheritedPath.push(value)
      continue
    }
    if (pass.has(lower) || prefixes.some((prefix) => lower.startsWith(prefix))) out[key] = value
  }

  const seen = new Set<string>()
  const kept: string[] = []
  for (const raw of inheritedPath.join(';').split(';')) {
    const dir = raw.trim()
    if (!dir) continue
    const norm = win32.normalize(dir).replace(/\\+$/, '').toLowerCase()
    if (seen.has(norm)) continue
    seen.add(norm)
    if (isForeignPythonDir(dir)) continue
    kept.push(dir)
  }

  const lead = [win32.join(opts.envDir, 'Scripts')]
  if (opts.toolsDir) lead.push(opts.toolsDir)
  const leadNorm = new Set(lead.map((d) => d.toLowerCase()))
  out.PATH = [...lead, ...kept.filter((d) => !leadNorm.has(d.toLowerCase()))].join(';')

  Object.assign(out, {
    VIRTUAL_ENV: opts.envDir,
    PYTHONNOUSERSITE: '1',
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
    UV_CACHE_DIR: opts.uvCacheDir,
    UV_PYTHON_DOWNLOADS: 'never',
    UV_NO_CONFIG: '1',
    UV_LINK_MODE: 'copy',
    PIP_CACHE_DIR: opts.pipCacheDir,
    PIP_REQUIRE_VIRTUALENV: '1',
    PIP_DISABLE_PIP_VERSION_CHECK: '1',
    PIP_NO_INPUT: '1',
    PIP_CONFIG_FILE: 'NUL',
    TEMP: opts.tmpDir,
    TMP: opts.tmpDir,
  })
  if (opts.constraintsFile) {
    out.PIP_CONSTRAINT = opts.constraintsFile
    out.UV_CONSTRAINT = opts.constraintsFile
  }
  return out
}
