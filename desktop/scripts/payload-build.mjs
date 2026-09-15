/**
 * Build the Desktop payload (CTR-0213 / CTR-0210, UDR-0151 D3). Windows x64 only.
 *
 *   1. version lockstep check (desktop == frontend == backend)
 *   2. frontend `pypi:build`  -> backend/dist/chatwalaau-<v>-py3-none-any.whl (SPA inside)
 *   3. pinned CPython + uv, downloaded once into .cache and verified by SHA-256
 *   4. `uv export --frozen --no-dev` of backend/uv.lock -> hashed runtime requirements
 *   5. `pip download --only-binary :all: --require-hashes` -> win_amd64 wheelhouse
 *      (a dependency without a Windows wheel FAILS HERE, never on a user's first run)
 *   6. pip + chatwalaau appended to the lock with their hashes
 *   7. launcher, licenses, SBOM, manifest.json
 *   8. trial install: a throwaway venv built exactly like the Desktop does it, offline
 *
 * Usage: pnpm payload:build [--skip-wheel] [--skip-trial]
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import {
  copyFileSync,
  createReadStream,
  createWriteStream,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { basename, join, resolve } from 'node:path'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { PINS, RUNTIME_ID } from './pins.mjs'

const DESKTOP = resolve(import.meta.dirname, '..')
const ROOT = resolve(DESKTOP, '..')
const OUT = join(DESKTOP, 'resources', 'generated', 'desktop-payload')
const CACHE = join(DESKTOP, '.cache')
const args = new Set(process.argv.slice(2))

const die = (msg) => {
  console.error(`payload:build ERROR: ${msg}`)
  process.exit(1)
}
const step = (msg) => console.log(`\n== ${msg}`)

function run(exe, argv, opts = {}) {
  const r = spawnSync(exe, argv, { stdio: opts.capture ? 'pipe' : 'inherit', encoding: 'utf8', shell: opts.shell ?? false, cwd: opts.cwd, env: opts.env ?? process.env })
  if (r.status !== 0) die(`${exe} ${argv.join(' ')} failed (${r.status})${opts.capture ? `\n${r.stderr}` : ''}`)
  return r.stdout ?? ''
}

function sha256(file) {
  return new Promise((res, rej) => {
    const h = createHash('sha256')
    createReadStream(file).on('error', rej).on('data', (d) => h.update(d)).on('end', () => res(h.digest('hex')))
  })
}

async function download(url, dest, expected) {
  if (existsSync(dest) && (await sha256(dest)) === expected) return dest
  console.log(`download ${url}`)
  const res = await fetch(url, { redirect: 'follow' })
  if (!res.ok || !res.body) die(`download failed: ${res.status} ${url}`)
  const tmp = `${dest}.part`
  await pipeline(Readable.fromWeb(res.body), createWriteStream(tmp))
  const got = await sha256(tmp)
  if (got !== expected) die(`SHA-256 mismatch for ${url}\n  expected ${expected}\n  got      ${got}`)
  renameSync(tmp, dest)
  return dest
}

const readVersion = {
  json: (f) => JSON.parse(readFileSync(f, 'utf8')).version,
  toml: (f) => readFileSync(f, 'utf8').match(/^version\s*=\s*"([^"]+)"/m)?.[1],
}

const TAR = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe')

// ---- 0. platform + versions ----------------------------------------------------------------
if (process.platform !== 'win32' || process.arch !== 'x64') die('the Windows payload must be built on Windows x64 (wheels are resolved for the host)')
const desktopVersion = readVersion.json(join(DESKTOP, 'package.json'))
const frontendVersion = readVersion.json(join(ROOT, 'frontend', 'package.json'))
const backendVersion = readVersion.toml(join(ROOT, 'backend', 'pyproject.toml'))
if (!(desktopVersion === frontendVersion && frontendVersion === backendVersion)) {
  die(`versions differ: desktop ${desktopVersion}, frontend ${frontendVersion}, backend ${backendVersion} (run version:set --target product)`)
}
console.log(`ChatWalaau Desktop payload ${backendVersion} (${RUNTIME_ID})`)

rmSync(OUT, { recursive: true, force: true })
const dirs = {
  runtime: join(OUT, 'runtime', 'win-x64'),
  tools: join(OUT, 'tools', 'win-x64'),
  launcher: join(OUT, 'launcher'),
  wheels: join(OUT, 'wheelhouse', 'win-x64'),
  locks: join(OUT, 'locks', 'win-x64'),
  licenses: join(OUT, 'licenses'),
  sbom: join(OUT, 'sbom'),
}
for (const d of [...Object.values(dirs), CACHE]) mkdirSync(d, { recursive: true })

// ---- 1. product wheel -----------------------------------------------------------------------
const wheelName = `chatwalaau-${backendVersion}-py3-none-any.whl`
const wheelPath = join(ROOT, 'backend', 'dist', wheelName)
if (!args.has('--skip-wheel')) {
  step('frontend pypi:build (SPA bundled into the wheel)')
  run('pnpm', ['run', 'pypi:build'], { cwd: join(ROOT, 'frontend'), shell: true })
}
if (!existsSync(wheelPath)) die(`missing ${wheelPath}`)

// ---- 2. pinned binaries ---------------------------------------------------------------------
step('pinned CPython and uv')
const pyArchive = await download(PINS.python.url, join(CACHE, PINS.python.payloadName), PINS.python.sha256)
const uvZip = await download(PINS.uv.url, join(CACHE, `uv-${PINS.uv.version}-x86_64-pc-windows-msvc.zip`), PINS.uv.sha256)
copyFileSync(pyArchive, join(dirs.runtime, PINS.python.payloadName))

const uvStage = join(CACHE, `uv-${PINS.uv.version}`)
if (!existsSync(join(uvStage, 'uv.exe'))) {
  mkdirSync(uvStage, { recursive: true })
  run(TAR, ['-xf', uvZip, '-C', uvStage])
}
for (const exe of ['uv.exe', 'uvx.exe']) copyFileSync(join(uvStage, exe), join(dirs.tools, exe))
const UV = join(uvStage, 'uv.exe')

const pyStage = join(CACHE, `stage-${RUNTIME_ID}`)
if (!existsSync(join(pyStage, 'python', 'python.exe'))) {
  rmSync(pyStage, { recursive: true, force: true })
  mkdirSync(pyStage, { recursive: true })
  run(TAR, ['-xzf', pyArchive, '-C', pyStage])
}
const PY = join(pyStage, 'python', 'python.exe')
if (spawnSync(PY, ['-m', 'pip', '--version']).status !== 0) run(PY, ['-m', 'ensurepip', '--default-pip'])
const pipVersion = run(PY, ['-m', 'pip', '--version'], { capture: true }).match(/^pip (\S+)/)?.[1]
if (!pipVersion) die('could not determine the staged pip version')

// ---- 3. lock export + wheelhouse ------------------------------------------------------------
step('export backend/uv.lock (runtime dependencies, hashed)')
const exported = join(CACHE, 'runtime-export.txt')
run(UV, [
  'export',
  '--project',
  join(ROOT, 'backend'),
  '--frozen',
  '--no-dev',
  '--no-emit-project',
  '--format',
  'requirements-txt',
  '--no-header',
  '--output-file',
  exported,
])

step('download win_amd64 wheels (binary only, hash-checked)')
const pipEnv = { ...process.env, PIP_CONFIG_FILE: 'NUL', PIP_DISABLE_PIP_VERSION_CHECK: '1', PYTHONNOUSERSITE: '1' }
run(PY, ['-m', 'pip', 'download', '--only-binary', ':all:', '--require-hashes', '--no-deps', '-r', exported, '-d', dirs.wheels], { env: pipEnv })
run(PY, ['-m', 'pip', 'download', '--only-binary', ':all:', '--no-deps', `pip==${pipVersion}`, '-d', dirs.wheels], { env: pipEnv })
copyFileSync(wheelPath, join(dirs.wheels, wheelName))

const pipWheel = readdirSync(dirs.wheels).find((f) => /^pip-.*\.whl$/i.test(f))
if (!pipWheel) die('pip wheel missing from the wheelhouse')
const lockText = `${readFileSync(exported, 'utf8').trimEnd()}
pip==${pipVersion} \\
    --hash=sha256:${await sha256(join(dirs.wheels, pipWheel))}
chatwalaau==${backendVersion} \\
    --hash=sha256:${await sha256(join(dirs.wheels, wheelName))}
`
const lockFile = join(dirs.locks, 'runtime-requirements.txt')
writeFileSync(lockFile, lockText)

// ---- 4. launcher, licenses, SBOM ------------------------------------------------------------
step('launcher, licenses, SBOM')
for (const f of ['backend_launcher.py', 'desktop_guard.py']) copyFileSync(join(DESKTOP, 'python', f), join(dirs.launcher, f))
copyFileSync(join(ROOT, 'LICENSE.md'), join(dirs.licenses, 'ChatWalaau-LICENSE.md'))
for (const name of ['LICENSE.txt', 'LICENSE']) {
  const f = join(pyStage, 'python', name)
  if (existsSync(f)) {
    copyFileSync(f, join(dirs.licenses, 'CPython-LICENSE.txt'))
    break
  }
}
writeFileSync(
  join(dirs.licenses, 'THIRD-PARTY.md'),
  `# Bundled third-party software\n\n- CPython ${PINS.python.version} (python-build-standalone ${PINS.python.build}) -- PSF License (CPython-LICENSE.txt)\n- uv ${PINS.uv.version} -- Apache-2.0 OR MIT (https://github.com/astral-sh/uv)\n- Python packages in wheelhouse/ -- each wheel carries its own license in its .dist-info; see ../sbom/components.json\n`,
)

const wheels = []
for (const f of readdirSync(dirs.wheels).sort()) wheels.push({ file: `wheelhouse/win-x64/${f}`, sha256: await sha256(join(dirs.wheels, f)) })
const components = wheels.map((w) => {
  const [name, version] = basename(w.file).split('-')
  return { type: 'python-wheel', name, version, file: w.file, sha256: w.sha256 }
})
components.push({ type: 'runtime', name: 'cpython', version: PINS.python.version, build: PINS.python.build, sha256: PINS.python.sha256 })
components.push({ type: 'tool', name: 'uv', version: PINS.uv.version, sha256: PINS.uv.sha256 })
writeFileSync(join(dirs.sbom, 'components.json'), `${JSON.stringify({ product: 'ChatWalaau Desktop', version: backendVersion, components }, null, 2)}\n`)

// ---- 5. manifest ----------------------------------------------------------------------------
step('manifest.json')
const git = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' })
const manifest = {
  schemaVersion: 1,
  desktopVersion,
  backendVersion,
  sourceCommit: git.status === 0 ? git.stdout.trim() : 'unknown',
  platform: 'win32',
  arch: 'x64',
  runtimeId: RUNTIME_ID,
  python: { version: PINS.python.version, build: PINS.python.build, archive: `runtime/win-x64/${PINS.python.payloadName}`, sha256: PINS.python.sha256 },
  uv: { version: PINS.uv.version, file: 'tools/win-x64/uv.exe', sha256: await sha256(join(dirs.tools, 'uv.exe')) },
  tools: [{ file: 'tools/win-x64/uvx.exe', sha256: await sha256(join(dirs.tools, 'uvx.exe')) }],
  lock: { file: 'locks/win-x64/runtime-requirements.txt', sha256: await sha256(lockFile) },
  wheels,
  launcher: [
    { file: 'launcher/backend_launcher.py', sha256: await sha256(join(dirs.launcher, 'backend_launcher.py')) },
    { file: 'launcher/desktop_guard.py', sha256: await sha256(join(dirs.launcher, 'desktop_guard.py')) },
  ],
  generatedAt: new Date().toISOString(),
}
writeFileSync(join(OUT, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`)

// ---- 6. trial install (offline, exactly like EnvironmentManager) ----------------------------
if (!args.has('--skip-trial')) {
  step('trial install: offline venv from the payload')
  const trial = join(CACHE, 'trial-env')
  rmSync(trial, { recursive: true, force: true })
  const env = { ...process.env, UV_NO_CONFIG: '1', UV_PYTHON_DOWNLOADS: 'never', UV_CACHE_DIR: join(CACHE, 'uv-cache'), PIP_CONFIG_FILE: 'NUL' }
  delete env.VIRTUAL_ENV
  run(UV, ['venv', '--no-config', '--offline', '--no-python-downloads', '--python', PY, trial], { env })
  run(
    UV,
    ['pip', 'install', '--no-config', '--offline', '--python', join(trial, 'Scripts', 'python.exe'), '--no-index', '--find-links', dirs.wheels, '--only-binary', ':all:', '--require-hashes', '--link-mode', 'copy', '-r', lockFile],
    { env },
  )
  const v = run(join(trial, 'Scripts', 'python.exe'), ['-c', 'from importlib import metadata; import app; print(metadata.version("chatwalaau"))'], { capture: true, env }).trim()
  if (v !== backendVersion) die(`trial install reports chatwalaau ${v}, expected ${backendVersion}`)
  rmSync(trial, { recursive: true, force: true })
  console.log(`trial install OK (chatwalaau ${v})`)
}

let total = 0
const walk = (d) => {
  for (const f of readdirSync(d)) {
    const full = join(d, f)
    const s = statSync(full)
    if (s.isDirectory()) walk(full)
    else total += s.size
  }
}
walk(OUT)
console.log(`\npayload ready: ${OUT}\n  ${wheels.length} wheels, ${(total / 1024 ** 2).toFixed(0)} MB total`)
