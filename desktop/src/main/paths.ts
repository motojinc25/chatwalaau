/**
 * Desktop data layout (CTR-0209, UDR-0151 D5). Pure: computes paths, touches nothing.
 *
 * <root>                       %LOCALAPPDATA%\ChatWalaau (packaged) or desktop\.dev-profile (dev)
 *   desktop\
 *     desktop-config.json
 *     runtime\<runtimeId>\     extracted CPython (moved into place once, never after)
 *     environments\<envId>\    venv generations (created at the final path, never moved)
 *     state\                   active pointer, per-generation state, freezes, inventory
 *     cache\uv\  cache\pip\  tmp\  logs\
 *     profile\                 backend cwd: .env, *.jsonc, sessions, uploads, RAG data ...
 *       workspace\             default CODING_WORKSPACE_DIR
 */

import { win32 } from 'node:path'

const p = win32

export interface Layout {
  root: string
  desktopDir: string
  configFile: string
  runtimeRoot: string
  envRoot: string
  stateDir: string
  activeFile: string
  pendingReinstallFile: string
  cacheUv: string
  cachePip: string
  tmpDir: string
  logsDir: string
  profileDir: string
  workspaceDir: string
}

export function computeLayout(root: string): Layout {
  const desktopDir = p.join(root, 'desktop')
  const stateDir = p.join(desktopDir, 'state')
  const profileDir = p.join(desktopDir, 'profile')
  return {
    root,
    desktopDir,
    configFile: p.join(desktopDir, 'desktop-config.json'),
    runtimeRoot: p.join(desktopDir, 'runtime'),
    envRoot: p.join(desktopDir, 'environments'),
    stateDir,
    activeFile: p.join(stateDir, 'active-environment.json'),
    pendingReinstallFile: p.join(stateDir, 'pending-reinstall.json'),
    cacheUv: p.join(desktopDir, 'cache', 'uv'),
    cachePip: p.join(desktopDir, 'cache', 'pip'),
    tmpDir: p.join(desktopDir, 'tmp'),
    logsDir: p.join(desktopDir, 'logs'),
    profileDir,
    workspaceDir: p.join(profileDir, 'workspace'),
  }
}

export function environmentStateFile(layout: Layout, envId: string): string {
  return p.join(layout.stateDir, `environment-${envId}.json`)
}

export function baseFreezeFile(layout: Layout, envId: string): string {
  return p.join(layout.stateDir, `base-freeze-${envId}.txt`)
}

export function venvPython(envDir: string): string {
  return p.join(envDir, 'Scripts', 'python.exe')
}

/**
 * True when `child` is strictly inside `parent` (case-insensitive, Windows semantics).
 * Every recursive delete is gated on this so a crafted or corrupted state file can never
 * point a delete outside the Desktop's own tree.
 */
export function isStrictlyInside(child: string, parent: string): boolean {
  const c = p.resolve(child).toLowerCase()
  const par = p.resolve(parent).toLowerCase()
  const rel = p.relative(par, c)
  return rel !== '' && !rel.startsWith('..') && !p.isAbsolute(rel)
}
