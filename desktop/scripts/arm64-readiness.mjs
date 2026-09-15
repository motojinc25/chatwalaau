/**
 * Windows arm64 readiness probe (RES-0006, UDR-0151 D14).
 *
 * The Desktop ships an x64 build only. A NATIVE arm64 build is blocked by exactly one
 * thing: UDR-0151 D3 requires an OFFLINE, hash-locked, binary-only environment build, so
 * every native dependency must already publish a `win_arm64` wheel at the locked version.
 *
 * This script answers "is that true yet?" from `backend/uv.lock` alone -- no network, no
 * payload build. A package is considered NATIVE when the lock carries a `win_amd64` (or
 * `win32`) wheel for it; it is READY when the same version also carries a `win_arm64` one.
 *
 * Exit code 0 = ready (0 missing), 1 = still blocked. Nothing else in the build reads it:
 * the release gates must not depend on upstream publishing schedules.
 *
 *   node scripts/arm64-readiness.mjs [--quiet]
 */

import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const LOCK = join(DESKTOP, '..', 'backend', 'uv.lock')
const quiet = process.argv.includes('--quiet')

/** Minimal reader for the `[[package]]` blocks of uv.lock (name, version, wheel URLs). */
function readLock(text) {
  const packages = []
  let current = null
  let inWheels = false
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (line === '[[package]]') {
      current = { name: '', version: '', wheels: [] }
      packages.push(current)
      inWheels = false
      continue
    }
    if (!current) continue
    if (line.startsWith('[') && line !== '[[package]]') inWheels = false
    if (line === 'wheels = [') {
      inWheels = true
      continue
    }
    if (inWheels) {
      if (line === ']') {
        inWheels = false
        continue
      }
      const url = /url = "([^"]+)"/.exec(line)
      if (url) current.wheels.push(url[1].split('/').pop())
      continue
    }
    const name = /^name = "([^"]+)"$/.exec(line)
    if (name) current.name = name[1]
    const version = /^version = "([^"]+)"$/.exec(line)
    if (version) current.version = version[1]
  }
  return packages.filter((p) => p.name && p.version)
}

const packages = readLock(readFileSync(LOCK, 'utf8'))
const native = packages
  .map((p) => ({
    name: p.name,
    version: p.version,
    amd64: p.wheels.filter((w) => /-win_amd64\.whl$/.test(w) || /-win32\.whl$/.test(w)),
    arm64: p.wheels.filter((w) => /-win_arm64\.whl$/.test(w)),
  }))
  .filter((p) => p.amd64.length > 0)
  .sort((a, b) => a.name.localeCompare(b.name))

const missing = native.filter((p) => p.arm64.length === 0)

if (!quiet) {
  console.log(`source: ${LOCK}`)
  console.log(`native (win_amd64/win32) packages: ${native.length}`)
  console.log(`  with win_arm64 wheels ......... ${native.length - missing.length}`)
  console.log(`  MISSING win_arm64 ............. ${missing.length}`)
  if (missing.length) {
    console.log('\nA native arm64 build is blocked by these packages (locked version):')
    for (const p of missing) console.log(`  ${p.name.padEnd(34)} ${p.version}`)
  }
}

if (missing.length === 0) {
  console.log('\nREADY: every native dependency publishes a win_arm64 wheel.')
  console.log('Next: RES-0006 gate items 2 (arm64 hardware) and 3 (arm64 build host or')
  console.log('verified cross-resolution), then a PRP for the native target.')
  process.exit(0)
}

console.log(`\nBLOCKED: ${missing.length} package(s) upstream. Windows on ARM stays on the x64`)
console.log('build under emulation (RES-0006, UDR-0151 D14). Re-run after a dependency bump.')
process.exit(1)
