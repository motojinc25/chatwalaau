/**
 * Verify a built payload before packaging (CTR-0213): every file exists and matches its
 * hash, the lock and the wheelhouse agree, the product wheel carries the SPA and the .env
 * template, versions are in lockstep, and the launcher in the payload is the current one.
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream, existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const DESKTOP = resolve(import.meta.dirname, '..')
const OUT = join(DESKTOP, 'resources', 'generated', 'desktop-payload')
const errors = []
const check = (ok, msg) => {
  if (!ok) errors.push(msg)
}

function sha256(file) {
  return new Promise((res, rej) => {
    const h = createHash('sha256')
    createReadStream(file).on('error', rej).on('data', (d) => h.update(d)).on('end', () => res(h.digest('hex')))
  })
}

const manifestFile = join(OUT, 'manifest.json')
if (!existsSync(manifestFile)) {
  console.error('payload:verify ERROR: no payload. Run `pnpm payload:build` first.')
  process.exit(1)
}
const m = JSON.parse(readFileSync(manifestFile, 'utf8'))
const pkg = JSON.parse(readFileSync(join(DESKTOP, 'package.json'), 'utf8'))
check(m.desktopVersion === pkg.version, `manifest desktopVersion ${m.desktopVersion} != package.json ${pkg.version}`)
check(m.backendVersion === pkg.version, `manifest backendVersion ${m.backendVersion} != package.json ${pkg.version} (lockstep)`)

const hashed = [
  { file: m.python.archive, sha256: m.python.sha256 },
  m.uv,
  ...(m.tools ?? []),
  m.lock,
  ...m.wheels,
  ...m.launcher,
]
for (const item of hashed) {
  const f = join(OUT, item.file)
  if (!existsSync(f)) {
    errors.push(`missing ${item.file}`)
    continue
  }
  check((await sha256(f)) === item.sha256, `hash mismatch: ${item.file}`)
}

for (const name of ['backend_launcher.py', 'desktop_guard.py']) {
  const inPayload = join(OUT, 'launcher', name)
  if (existsSync(inPayload)) check((await sha256(inPayload)) === (await sha256(join(DESKTOP, 'python', name))), `stale launcher in payload: ${name} (rebuild the payload)`)
}

const norm = (s) => s.toLowerCase().replace(/[-_.]+/g, '-')
const wheelNames = new Set(readdirSync(join(OUT, 'wheelhouse', 'win-x64')).map((f) => norm(f.split('-')[0])))
const lock = readFileSync(join(OUT, m.lock.file), 'utf8')
let requirements = 0
for (const line of lock.split(/\r?\n/)) {
  const t = line.trim()
  if (!t || t.startsWith('#') || t.startsWith('--')) continue
  const name = t.match(/^([A-Za-z0-9][A-Za-z0-9._-]*)==/)?.[1]
  if (!name) continue
  requirements++
  // A requirement whose marker excludes Windows has no wheel; pip skipped it at build time.
  const marker = t.includes(';') ? t.slice(t.indexOf(';')) : ''
  const excludesWindows = /(sys_platform|platform_system|os_name)\s*(!=\s*['"](win32|Windows|nt)['"]|==\s*['"](linux|darwin|Linux|Darwin|posix)['"])/.test(marker)
  if (!wheelNames.has(norm(name)) && !excludesWindows) {
    errors.push(`no wheel for requirement ${name}`)
  }
}

const productWheel = m.wheels.find((w) => /wheelhouse\/win-x64\/chatwalaau-/.test(w.file))
check(productWheel, 'chatwalaau wheel missing from the manifest')
if (productWheel) {
  const tar = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe')
  const listing = spawnSync(tar, ['-tf', join(OUT, productWheel.file)], { encoding: 'utf8' }).stdout ?? ''
  check(listing.includes('app/_frontend_dist/index.html'), 'the chatwalaau wheel does not contain the SPA (app/_frontend_dist/index.html)')
  check(listing.includes('app/templates/.env.template'), 'the chatwalaau wheel does not contain app/templates/.env.template')
}

if (errors.length) {
  console.error(`payload:verify FAILED (${errors.length}):\n  - ${errors.join('\n  - ')}`)
  process.exit(1)
}
console.log(`payload:verify OK -- ${m.backendVersion}, ${m.wheels.length} wheels, ${requirements} requirements, runtime ${m.runtimeId}`)
