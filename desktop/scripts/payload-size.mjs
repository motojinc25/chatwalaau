/**
 * The Desktop payload size budget (CTR-0213, UDR-0238 D5, PRP-0257 Q1).
 *
 * `payload:verify` fails when the payload directory is larger than PAYLOAD_BUDGET_MIB, and
 * `payload:build` / `payload:verify` print the largest wheels, so a dependency that grows
 * silently (claude-agent-sdk doubled between v0.203.0 and v0.218.0) becomes a build error
 * that names it. The budget is raised only by a proposal that names the growth.
 */

import { readdirSync, statSync } from 'node:fs'
import { basename, join } from 'node:path'

export const PAYLOAD_BUDGET_MIB = 280
const MIB = 1024 ** 2

/** Every file under `dir` with its size in bytes. */
export function listFiles(dir) {
  const out = []
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const full = join(d, name)
      const s = statSync(full)
      if (s.isDirectory()) walk(full)
      else out.push({ file: full, size: s.size })
    }
  }
  walk(dir)
  return out
}

/** Total size, the largest wheels and whether the total is within the budget. */
export function budgetReport(files, budgetMiB = PAYLOAD_BUDGET_MIB, top = 10) {
  const total = files.reduce((n, f) => n + f.size, 0)
  const largest = files
    .filter((f) => f.file.endsWith('.whl'))
    .sort((a, b) => b.size - a.size)
    .slice(0, top)
    .map((f) => ({ name: basename(f.file), mib: f.size / MIB }))
  return { totalMiB: total / MIB, budgetMiB, withinBudget: total <= budgetMiB * MIB, largest }
}

export function formatLargest(report) {
  return report.largest.map((w) => `  ${w.mib.toFixed(1).padStart(7)} MiB  ${w.name}`).join('\n')
}
