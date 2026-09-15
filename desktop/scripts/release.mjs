/**
 * Upload the Desktop installer to the product's GitHub Release (CTR-0213 -> CTR-0212).
 *
 * The release `v<version>` must already exist (created by frontend `github:release`,
 * CTR-0079). Manual, like every publish step (UDR-0006). Uses the gh CLI; GH_TOKEN is
 * honoured by gh when set.
 */

import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const DESKTOP = resolve(import.meta.dirname, '..')
const REPO = 'motojinc25/chatwalaau'
const version = JSON.parse(readFileSync(join(DESKTOP, 'package.json'), 'utf8')).version
const tag = `v${version}`
const dist = join(DESKTOP, 'dist')
const files = [`ChatWalaau-Setup-${version}.exe`, `ChatWalaau-Setup-${version}.exe.blockmap`, 'latest.yml'].map((f) => join(dist, f))

const missing = files.filter((f) => !existsSync(f))
if (missing.length) {
  console.error(`release ERROR: missing build output (run \`pnpm dist\`):\n  ${missing.join('\n  ')}`)
  process.exit(1)
}
const latest = readFileSync(join(dist, 'latest.yml'), 'utf8')
if (!latest.includes(`version: ${version}`)) {
  console.error(`release ERROR: dist/latest.yml is not for ${version}`)
  process.exit(1)
}

const view = spawnSync('gh', ['release', 'view', tag, '--repo', REPO], { stdio: 'ignore' })
if (view.status !== 0) {
  console.error(`release ERROR: GitHub release ${tag} not found in ${REPO}. Run \`pnpm github:release\` in frontend/ first.`)
  process.exit(1)
}
const up = spawnSync('gh', ['release', 'upload', tag, ...files, '--repo', REPO, '--clobber'], { stdio: 'inherit' })
if (up.status !== 0) process.exit(up.status ?? 1)
console.log(`uploaded Desktop ${version} to ${REPO} release ${tag}`)
