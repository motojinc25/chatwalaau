/**
 * Diagnostics export (RES-0004 section 13). User-initiated only; never includes .env,
 * conversation content, the environment block or the per-launch token.
 */

import { writeFileSync } from 'node:fs'
import { arch, release } from 'node:os'
import { app, type BrowserWindow, dialog, type SaveDialogOptions } from 'electron'
import { redact } from './log'
import { isPortFree } from './ports'
import { runProcess } from './spawn'

export interface DiagnosticsContext {
  phase: string
  lastError?: { code: string; message: string }
  devMode: boolean
  signedBuild: boolean
  dataRoot: string
  profileDir: string
  logsDir: string
  envId?: string | null
  python?: string
  backendVersion?: string
  isolation?: unknown
  origin?: string
  port?: number
  sandboxPort?: number
}

async function which(name: string, env?: Record<string, string>): Promise<string | null> {
  const r = await runProcess('where.exe', [name], { env, timeoutMs: 5_000 })
  return r.code === 0 ? (r.stdout.split(/\r?\n/)[0]?.trim() ?? null) || null : null
}

export async function collectDiagnostics(ctx: DiagnosticsContext, childEnv?: Record<string, string>): Promise<Record<string, unknown>> {
  const tools: Record<string, string | null> = {}
  // Resolved with the BACKEND's PATH: this is what the agent's tools will find.
  for (const t of ['python', 'python3', 'pip', 'uv', 'uvx', 'git', 'node', 'npx', 'az']) tools[t] = await which(t, childEnv)
  const sandboxListening = ctx.sandboxPort ? !(await isPortFree(ctx.sandboxPort)) : null
  return {
    generatedAt: new Date().toISOString(),
    app: {
      version: app.getVersion(),
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      node: process.versions.node,
      packaged: app.isPackaged,
      devMode: ctx.devMode,
      signedBuild: ctx.signedBuild,
    },
    os: { platform: process.platform, release: release(), arch: arch() },
    phase: ctx.phase,
    lastError: ctx.lastError ?? null,
    environment: { envId: ctx.envId ?? null, python: ctx.python ?? null, backendVersion: ctx.backendVersion ?? null, isolation: ctx.isolation ?? null },
    network: {
      origin: ctx.origin ?? null,
      effective: { APP_HOST: '127.0.0.1', APP_PORT: ctx.port ?? null, MCP_APPS_SANDBOX_PORT: ctx.sandboxPort ?? null },
      mcpAppsSandboxListening: sandboxListening,
    },
    paths: { dataRoot: ctx.dataRoot, profileDir: ctx.profileDir, logsDir: ctx.logsDir },
    tools,
  }
}

export async function exportDiagnostics(win: BrowserWindow | null, data: Record<string, unknown>, secrets: string[]): Promise<string | null> {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-')
  const opts: SaveDialogOptions = {
    title: 'Export Diagnostics',
    defaultPath: `chatwalaau-diagnostics-${stamp}.json`,
    filters: [{ name: 'JSON', extensions: ['json'] }],
  }
  const r = win && !win.isDestroyed() ? await dialog.showSaveDialog(win, opts) : await dialog.showSaveDialog(opts)
  if (r.canceled || !r.filePath) return null
  writeFileSync(r.filePath, redact(JSON.stringify(data, null, 2), secrets), 'utf8')
  return r.filePath
}
