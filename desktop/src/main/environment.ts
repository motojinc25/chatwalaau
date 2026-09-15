/**
 * EnvironmentManager (CTR-0210, UDR-0151 D3/D4/D5/D10).
 *
 * Turns the bundled payload into a ready venv generation, offline:
 *   verify payload -> extract the pinned CPython once -> uv venv at the FINAL path ->
 *   uv pip install --offline --no-index --require-hashes -> isolation check ->
 *   record the base freeze -> mark ready -> atomically switch the active pointer.
 * A venv is never moved; a new base lock (every product update) is a new generation.
 */

import { createHash, randomBytes } from 'node:crypto'
import {
  copyFileSync,
  createReadStream,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statfsSync,
  writeFileSync,
} from 'node:fs'
import { win32 as p } from 'node:path'
import { buildChildEnv, FOREIGN_PYTHON_MARKERS } from './child-env'
import { writeJsonAtomic } from './config'
import { DesktopError, type ErrorCode, MIN_FREE_BYTES, type Phase } from './constants'
import type { RotatingLog } from './log'
import {
  type ActiveEnvironment,
  computeEnvId,
  diffFreeze,
  type EnvironmentState,
  type HashedFile,
  type IsolationReport,
  type PayloadManifest,
  validateManifest,
  verifyIsolation,
} from './manifest'
import { baseFreezeFile, environmentStateFile, isStrictlyInside, type Layout, venvPython } from './paths'
import { type RunResult, runProcess } from './spawn'

export type Progress = (phase: Phase, message: string) => void

export interface ReadyEnvironment {
  envId: string
  envDir: string
  python: string
  runtimeDir: string
  toolsDir: string
  baseFreeze: string
  manifest: PayloadManifest
}

export interface PendingReinstall {
  fromEnvId: string
  packages: string[]
  recordedAt: string
}

export interface ReinstallResult {
  installed: string[]
  failed: { pkg: string; reason: string }[]
}

const ISOLATION_SCRIPT = [
  'import json, site, sys, sysconfig',
  'from importlib import metadata',
  'print(json.dumps({"executable": sys.executable, "prefix": sys.prefix, "base_prefix": sys.base_prefix,',
  '  "user_site": site.ENABLE_USER_SITE, "purelib": sysconfig.get_path("purelib"),',
  '  "platlib": sysconfig.get_path("platlib"), "version": metadata.version("chatwalaau")}))',
].join('\n')

/** A requirement a user may reinstall: a name, optional extras, optional exact pin. Nothing starting with '-'. */
const REQUIREMENT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?(===?[A-Za-z0-9.+!_-]+)?$/

export function isForeignPythonDir(dir: string): boolean {
  try {
    return FOREIGN_PYTHON_MARKERS.some((marker) => existsSync(p.join(dir, marker)))
  } catch {
    return false
  }
}

export function sha256File(file: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256')
    createReadStream(file)
      .on('error', reject)
      .on('data', (chunk) => hash.update(chunk))
      .on('end', () => resolve(hash.digest('hex')))
  })
}

function readJson<T>(file: string): T | null {
  try {
    return JSON.parse(readFileSync(file, 'utf8')) as T
  } catch {
    return null
  }
}

function classify(result: RunResult): ErrorCode {
  const text = `${result.stderr}\n${result.stdout}`
  if (/no space left|not enough space|ENOSPC|os error 112/i.test(text)) return 'DISK_FULL'
  if (/hash/i.test(text) && /mismatch|does not match/i.test(text)) return 'PAYLOAD_INVALID'
  if (/access is denied|os error 5\b|EPERM|EACCES/i.test(text)) return 'PERMISSION_DENIED'
  if (/certificate|ssl|tls/i.test(text)) return 'TLS_PROXY_ERROR'
  if (/no matching distribution|not find a version|no solution found/i.test(text)) return 'DEPENDENCY_CONFLICT'
  return 'ENV_CREATE_FAILED'
}

export function makeLayoutDirs(layout: Layout): void {
  for (const dir of [
    layout.desktopDir,
    layout.runtimeRoot,
    layout.envRoot,
    layout.stateDir,
    layout.cacheUv,
    layout.cachePip,
    layout.tmpDir,
    layout.logsDir,
    layout.profileDir,
    layout.workspaceDir,
  ]) {
    mkdirSync(dir, { recursive: true })
  }
}

/**
 * First-run profile (UDR-0151 D5): create `.env` from the product's own template ONLY when
 * absent, pointing CODING_WORKSPACE_DIR at profile\workspace. An existing .env is never
 * touched. Returns true when a new .env was written.
 */
export function ensureProfile(layout: Layout, templatePath: string | null): boolean {
  mkdirSync(layout.workspaceDir, { recursive: true })
  const envFile = p.join(layout.profileDir, '.env')
  if (existsSync(envFile)) return false
  let text = templatePath && existsSync(templatePath) ? readFileSync(templatePath, 'utf8') : ''
  const line = `CODING_WORKSPACE_DIR=${layout.workspaceDir}`
  const re = /^#?[ \t]*CODING_WORKSPACE_DIR=.*$/m
  text = re.test(text) ? text.replace(re, line) : `${text}${text.endsWith('\n') || !text ? '' : '\n'}${line}\n`
  const header = '# Created by ChatWalaau Desktop on first run from the product template.\n# Edit freely; the Desktop never rewrites an existing .env.\n'
  writeFileSync(envFile, header + text, 'utf8')
  return true
}

export class EnvironmentManager {
  constructor(
    private readonly layout: Layout,
    private readonly payloadDir: string,
    private readonly log: RotatingLog,
    private readonly buildTimeoutMs: number,
  ) {}

  readManifest(): PayloadManifest {
    const file = p.join(this.payloadDir, 'manifest.json')
    if (!existsSync(file)) {
      throw new DesktopError('PAYLOAD_INVALID', 'The installation is incomplete (desktop-payload/manifest.json is missing). Reinstall ChatWalaʻau.')
    }
    let raw: unknown
    try {
      raw = JSON.parse(readFileSync(file, 'utf8'))
    } catch {
      throw new DesktopError('PAYLOAD_INVALID', 'desktop-payload/manifest.json is not valid JSON. Reinstall ChatWalaʻau.')
    }
    return validateManifest(raw)
  }

  private payload(rel: string): string {
    return p.join(this.payloadDir, rel)
  }

  private toolsDir(m: PayloadManifest): string {
    return p.dirname(this.payload(m.uv.file))
  }

  private uvEnv(m: PayloadManifest, envDir: string): Record<string, string> {
    return buildChildEnv(
      process.env,
      {
        envDir,
        toolsDir: this.toolsDir(m),
        uvCacheDir: this.layout.cacheUv,
        pipCacheDir: this.layout.cachePip,
        tmpDir: this.layout.tmpDir,
      },
      isForeignPythonDir,
    )
  }

  /** The backend's environment for a ready generation (CTR-0209). */
  childEnv(ready: ReadyEnvironment): Record<string, string> {
    return buildChildEnv(
      process.env,
      {
        envDir: ready.envDir,
        toolsDir: ready.toolsDir,
        uvCacheDir: this.layout.cacheUv,
        pipCacheDir: this.layout.cachePip,
        tmpDir: this.layout.tmpDir,
        constraintsFile: existsSync(ready.baseFreeze) ? ready.baseFreeze : undefined,
      },
      isForeignPythonDir,
    )
  }

  private readyEnvironment(m: PayloadManifest, envId: string): ReadyEnvironment {
    const envDir = p.join(this.layout.envRoot, envId)
    return {
      envId,
      envDir,
      python: venvPython(envDir),
      runtimeDir: p.join(this.layout.runtimeRoot, m.runtimeId),
      toolsDir: this.toolsDir(m),
      baseFreeze: baseFreezeFile(this.layout, envId),
      manifest: m,
    }
  }

  async ensure(progress: Progress): Promise<ReadyEnvironment> {
    progress('PRECHECK', 'Checking the installation')
    // An x64 process is what we ship. Windows on ARM runs it under emulation and still
    // reports x64 here, which is why arm64 devices pass this guard (UDR-0151 D14).
    if (process.platform !== 'win32' || process.arch !== 'x64') {
      throw new DesktopError(
        'UNSUPPORTED_PLATFORM',
        `ChatWalaʻau Desktop is an x64 Windows application (found ${process.platform}/${process.arch}). On Windows on ARM, install the same x64 build -- it runs under emulation.`,
      )
    }
    const m = this.readManifest()
    makeLayoutDirs(this.layout)

    const envId = computeEnvId(m.runtimeId, m.lock.sha256)
    const target = this.readyEnvironment(m, envId)
    const active = readJson<ActiveEnvironment>(this.layout.activeFile)
    const state = readJson<EnvironmentState>(environmentStateFile(this.layout, envId))
    if (
      active?.envId === envId &&
      state?.status === 'ready' &&
      existsSync(target.python) &&
      existsSync(p.join(target.runtimeDir, 'python.exe'))
    ) {
      this.log.write(`environment ${envId} is ready`)
      return target
    }

    this.checkDiskSpace()
    progress('PRECHECK', 'Verifying the installer payload')
    await this.verifyPayload(m)
    progress('PREPARE_RUNTIME', 'Preparing the bundled Python runtime')
    await this.ensureRuntime(m, target.runtimeDir)
    progress('PREPARE_ENV', 'Creating the Python environment. This runs once after installing or updating and can take several minutes.')
    await this.buildGeneration(m, target, active)
    return target
  }

  private checkDiskSpace(): void {
    try {
      const s = statfsSync(this.layout.desktopDir)
      const free = Number(s.bavail) * Number(s.bsize)
      if (free < MIN_FREE_BYTES) {
        throw new DesktopError('DISK_FULL', `At least 3 GB of free disk space is needed (found ${(free / 1024 ** 3).toFixed(1)} GB).`)
      }
    } catch (err) {
      if (err instanceof DesktopError) throw err
    }
  }

  private async verifyPayload(m: PayloadManifest): Promise<void> {
    // Wheels are verified by uv itself (--require-hashes against the hashed lock).
    const items: HashedFile[] = [
      m.lock,
      { file: m.python.archive, sha256: m.python.sha256 },
      { file: m.uv.file, sha256: m.uv.sha256 },
      ...m.launcher,
    ]
    for (const item of items) {
      const file = this.payload(item.file)
      if (!existsSync(file)) throw new DesktopError('PAYLOAD_INVALID', `The installation is incomplete: ${item.file} is missing. Reinstall ChatWalaʻau.`)
      if ((await sha256File(file)) !== item.sha256) {
        throw new DesktopError('PAYLOAD_INVALID', `The installation is damaged: ${item.file} does not match its checksum. Reinstall ChatWalaʻau.`)
      }
    }
  }

  private async ensureRuntime(m: PayloadManifest, runtimeDir: string): Promise<void> {
    const marker = p.join(runtimeDir, '.chatwalaau-runtime')
    if (existsSync(p.join(runtimeDir, 'python.exe')) && existsSync(marker)) return
    const staging = p.join(this.layout.tmpDir, `runtime-${randomBytes(4).toString('hex')}`)
    mkdirSync(staging, { recursive: true })
    try {
      const tar = p.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe')
      const r = await runProcess(tar, ['-xzf', this.payload(m.python.archive), '-C', staging], { timeoutMs: this.buildTimeoutMs })
      if (r.code !== 0) throw new DesktopError(classify(r), 'Could not extract the bundled Python runtime.', r.stderr.slice(-4000))
      const extracted = p.join(staging, 'python')
      if (!existsSync(p.join(extracted, 'python.exe'))) throw new DesktopError('PAYLOAD_INVALID', 'The bundled Python archive has an unexpected layout.')
      if (existsSync(runtimeDir)) {
        if (!isStrictlyInside(runtimeDir, this.layout.runtimeRoot)) throw new DesktopError('PERMISSION_DENIED', 'Refusing to replace a runtime outside the data root.')
        rmSync(runtimeDir, { recursive: true, force: true })
      }
      // The runtime is moved into place ONCE, before any venv refers to it; venvs never move.
      renameSync(extracted, runtimeDir)
      writeFileSync(marker, `${m.runtimeId}\n`, 'utf8')
      this.log.write(`runtime ${m.runtimeId} extracted`)
    } finally {
      rmSync(staging, { recursive: true, force: true })
    }
  }

  private async uv(m: PayloadManifest, envDir: string, args: string[], what: string): Promise<RunResult> {
    const r = await runProcess(this.payload(m.uv.file), args, {
      env: this.uvEnv(m, envDir),
      timeoutMs: this.buildTimeoutMs,
      onOutput: (chunk) => this.log.write(chunk.trimEnd()),
    })
    if (r.timedOut) throw new DesktopError('ENV_CREATE_FAILED', `${what} timed out.`)
    if (r.code !== 0) throw new DesktopError(classify(r), `${what} failed.`, `${r.stderr}\n${r.stdout}`.slice(-4000))
    return r
  }

  private async buildGeneration(m: PayloadManifest, target: ReadyEnvironment, active: ActiveEnvironment | null): Promise<void> {
    const { envId, envDir, python, runtimeDir } = target
    const stateFile = environmentStateFile(this.layout, envId)
    if (existsSync(envDir)) {
      if (!isStrictlyInside(envDir, this.layout.envRoot)) throw new DesktopError('PERMISSION_DENIED', 'Refusing to delete outside the data root.')
      rmSync(envDir, { recursive: true, force: true })
    }
    const state: EnvironmentState = {
      schemaVersion: 1,
      envId,
      status: 'preparing',
      runtimeId: m.runtimeId,
      backendVersion: m.backendVersion,
      desktopVersion: m.desktopVersion,
      envPath: envDir,
      basePython: p.join(runtimeDir, 'python.exe'),
      pythonVersion: m.python.version,
      uvVersion: m.uv.version,
      lockSha256: m.lock.sha256,
      createdAt: new Date().toISOString(),
    }
    writeJsonAtomic(stateFile, state)
    try {
      await this.uv(m, envDir, ['venv', '--no-config', '--offline', '--no-python-downloads', '--python', state.basePython, envDir], 'Creating the venv')
      const wheelhouse = this.payload(p.dirname(m.wheels[0].file))
      await this.uv(
        m,
        envDir,
        [
          'pip',
          'install',
          '--no-config',
          '--offline',
          '--python',
          python,
          '--no-index',
          '--find-links',
          wheelhouse,
          '--only-binary',
          ':all:',
          '--require-hashes',
          '--link-mode',
          'copy',
          '--compile-bytecode',
          '-r',
          this.payload(m.lock.file),
        ],
        'Installing the bundled packages',
      )
      // `python3` resolves to the venv too (the venv launcher reads pyvenv.cfg next to itself).
      copyFileSync(python, p.join(envDir, 'Scripts', 'python3.exe'))

      const report = await this.isolationReport(python, this.uvEnv(m, envDir))
      const failures = verifyIsolation(report, { envDir, runtimeDir, backendVersion: m.backendVersion })
      if (failures.length) throw new DesktopError('ENV_CREATE_FAILED', 'The new Python environment failed its isolation check.', failures.join('\n'))

      const freeze = await this.uv(m, envDir, ['pip', 'freeze', '--no-config', '--offline', '--python', python], 'Recording the base packages')
      writeFileSync(target.baseFreeze, freeze.stdout, 'utf8')

      if (active && active.envId !== envId) await this.recordUserPackages(m, active.envId)

      writeJsonAtomic(stateFile, { ...state, status: 'ready', checkedAt: new Date().toISOString() })
      const pointer: ActiveEnvironment = {
        envId,
        previousEnvId: active && active.envId !== envId ? active.envId : active?.previousEnvId,
        launchedOk: false,
        switchedAt: new Date().toISOString(),
      }
      writeJsonAtomic(this.layout.activeFile, pointer)
      this.log.write(`environment ${envId} ready`)
    } catch (err) {
      writeJsonAtomic(stateFile, { ...state, status: 'broken', error: err instanceof Error ? err.message : String(err) })
      throw err
    }
  }

  async isolationReport(python: string, env: Record<string, string>): Promise<IsolationReport> {
    const r = await runProcess(python, ['-c', ISOLATION_SCRIPT], { env, timeoutMs: 60_000 })
    const last = r.stdout.trim().split(/\r?\n/).pop() ?? ''
    try {
      return JSON.parse(last) as IsolationReport
    } catch {
      throw new DesktopError('ENV_CREATE_FAILED', 'The isolation check could not run.', `${r.stderr}\n${r.stdout}`.slice(-4000))
    }
  }

  /** Inventory of packages added to a generation beyond its base freeze (UDR-0151 D10). */
  private async recordUserPackages(m: PayloadManifest, fromEnvId: string): Promise<void> {
    try {
      const prevDir = p.join(this.layout.envRoot, fromEnvId)
      const prevBase = baseFreezeFile(this.layout, fromEnvId)
      if (!existsSync(venvPython(prevDir)) || !existsSync(prevBase)) return
      const r = await runProcess(this.payload(m.uv.file), ['pip', 'freeze', '--no-config', '--offline', '--python', venvPython(prevDir)], {
        env: this.uvEnv(m, prevDir),
        timeoutMs: 120_000,
      })
      if (r.code !== 0) return
      const packages = diffFreeze(r.stdout, readFileSync(prevBase, 'utf8'))
      if (packages.length) {
        const pending: PendingReinstall = { fromEnvId, packages, recordedAt: new Date().toISOString() }
        writeJsonAtomic(this.layout.pendingReinstallFile, pending)
        this.log.write(`recorded ${packages.length} user-added package(s) from ${fromEnvId}`)
      }
    } catch (err) {
      this.log.write(`could not record user-added packages: ${String(err)}`)
    }
  }

  /** After the first successful launch of the active generation, retire the others. */
  markLaunchedOk(): void {
    const active = readJson<ActiveEnvironment>(this.layout.activeFile)
    if (!active || active.launchedOk) return
    writeJsonAtomic(this.layout.activeFile, { ...active, launchedOk: true })
    for (const name of readdirSync(this.layout.envRoot)) {
      if (name === active.envId) continue
      const dir = p.join(this.layout.envRoot, name)
      if (!isStrictlyInside(dir, this.layout.envRoot)) continue
      try {
        rmSync(dir, { recursive: true, force: true })
        const stateFile = environmentStateFile(this.layout, name)
        const st = readJson<EnvironmentState>(stateFile)
        if (st) writeJsonAtomic(stateFile, { ...st, status: 'retired' })
        this.log.write(`retired environment ${name}`)
      } catch (err) {
        this.log.write(`could not retire environment ${name}: ${String(err)}`)
      }
    }
  }

  /** "Rebuild environment": keep the profile, rebuild the venv at its final path on next ensure(). */
  async requestRebuild(): Promise<void> {
    const active = readJson<ActiveEnvironment>(this.layout.activeFile)
    if (!active) return
    try {
      const m = this.readManifest()
      const envDir = p.join(this.layout.envRoot, active.envId)
      const base = baseFreezeFile(this.layout, active.envId)
      if (existsSync(venvPython(envDir)) && existsSync(base)) {
        const r = await runProcess(this.payload(m.uv.file), ['pip', 'freeze', '--no-config', '--offline', '--python', venvPython(envDir)], {
          env: this.uvEnv(m, envDir),
          timeoutMs: 120_000,
        })
        const packages = r.code === 0 ? diffFreeze(r.stdout, readFileSync(base, 'utf8')) : []
        if (packages.length) {
          writeJsonAtomic(this.layout.pendingReinstallFile, { fromEnvId: active.envId, packages, recordedAt: new Date().toISOString() })
        }
      }
    } catch (err) {
      this.log.write(`rebuild inventory failed: ${String(err)}`)
    }
    const stateFile = environmentStateFile(this.layout, active.envId)
    const st = readJson<EnvironmentState>(stateFile)
    if (st) writeJsonAtomic(stateFile, { ...st, status: 'broken', error: 'rebuild requested' })
    rmSync(this.layout.activeFile, { force: true })
    this.log.write(`rebuild requested for ${active.envId}`)
  }

  readPendingReinstall(): PendingReinstall | null {
    const pending = readJson<PendingReinstall>(this.layout.pendingReinstallFile)
    return pending && Array.isArray(pending.packages) ? pending : null
  }

  clearPendingReinstall(): void {
    rmSync(this.layout.pendingReinstallFile, { force: true })
  }

  /** Reinstall user-added packages into the active generation (network; constrained by the base freeze). */
  async reinstall(ready: ReadyEnvironment, packages: string[]): Promise<ReinstallResult> {
    const result: ReinstallResult = { installed: [], failed: [] }
    const env = this.childEnv(ready)
    delete env.PIP_CONSTRAINT
    delete env.UV_CONSTRAINT
    for (const pkg of packages) {
      if (!REQUIREMENT_RE.test(pkg)) {
        result.failed.push({ pkg, reason: 'not a plain requirement' })
        continue
      }
      const r = await runProcess(
        this.payload(ready.manifest.uv.file),
        ['pip', 'install', '--no-config', '--python', ready.python, '--only-binary', ':all:', '--constraint', ready.baseFreeze, pkg],
        { env, timeoutMs: 10 * 60_000, onOutput: (c) => this.log.write(c.trimEnd()) },
      )
      if (r.code === 0) result.installed.push(pkg)
      else result.failed.push({ pkg, reason: classify(r) })
    }
    return result
  }
}
