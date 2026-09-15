/**
 * desktop-config.json (CTR-0209): the Desktop's own settings. Never holds secrets and
 * never duplicates a key owned by the profile's .env or app_settings.jsonc.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { DEFAULT_TIMEOUTS } from './constants'

export interface WindowBounds {
  x: number
  y: number
  width: number
  height: number
}

export interface DesktopConfig {
  schemaVersion: 1
  lastPort?: number
  lastSandboxPort?: number
  autoUpdate: { enabled: boolean; autoDownload: boolean }
  timeouts: { environmentBuildMs: number; backendReadyMs: number; gracefulStopMs: number }
  window?: { bounds?: WindowBounds }
}

export function defaultConfig(): DesktopConfig {
  return {
    schemaVersion: 1,
    autoUpdate: { enabled: true, autoDownload: true },
    timeouts: { ...DEFAULT_TIMEOUTS },
  }
}

function positiveInt(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isInteger(v) && v > 0 ? v : fallback
}

function port(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isInteger(v) && v > 1023 && v < 65536 ? v : undefined
}

/** Merge an untrusted parsed document over the defaults, dropping anything malformed. */
export function normalizeConfig(raw: unknown): DesktopConfig {
  const d = defaultConfig()
  if (typeof raw !== 'object' || raw === null) return d
  const r = raw as Record<string, unknown>
  const au = (r.autoUpdate ?? {}) as Record<string, unknown>
  const t = (r.timeouts ?? {}) as Record<string, unknown>
  const cfg: DesktopConfig = {
    schemaVersion: 1,
    lastPort: port(r.lastPort),
    lastSandboxPort: port(r.lastSandboxPort),
    autoUpdate: {
      enabled: typeof au.enabled === 'boolean' ? au.enabled : d.autoUpdate.enabled,
      autoDownload: typeof au.autoDownload === 'boolean' ? au.autoDownload : d.autoUpdate.autoDownload,
    },
    timeouts: {
      environmentBuildMs: positiveInt(t.environmentBuildMs, d.timeouts.environmentBuildMs),
      backendReadyMs: positiveInt(t.backendReadyMs, d.timeouts.backendReadyMs),
      gracefulStopMs: positiveInt(t.gracefulStopMs, d.timeouts.gracefulStopMs),
    },
  }
  const w = (r.window ?? {}) as Record<string, unknown>
  const b = w.bounds as Record<string, unknown> | undefined
  if (b && ['x', 'y', 'width', 'height'].every((k) => typeof b[k] === 'number')) {
    cfg.window = { bounds: { x: b.x as number, y: b.y as number, width: b.width as number, height: b.height as number } }
  }
  return cfg
}

export function loadConfig(file: string): DesktopConfig {
  if (!existsSync(file)) return defaultConfig()
  try {
    return normalizeConfig(JSON.parse(readFileSync(file, 'utf8')))
  } catch {
    return defaultConfig()
  }
}

/** Atomic write: temp file + rename. */
export function writeJsonAtomic(file: string, value: unknown): void {
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.tmp`
  writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
  renameSync(tmp, file)
}

export function saveConfig(file: string, cfg: DesktopConfig): void {
  writeJsonAtomic(file, cfg)
}
