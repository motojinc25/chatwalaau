/**
 * Electron wiring of the window policy (CTR-0214, UDR-0151 D12). Decisions live in the
 * pure window-policy.ts; this module only applies them.
 */

import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { BrowserWindow, Menu, type MenuItemConstructorOptions, type Session, screen, session, shell, type WebContents } from 'electron'
import type { WindowBounds } from './config'
import { DESKTOP_COOKIE, DISPLAY_NAME, PARTITION } from './constants'
import type { RotatingLog } from './log'
import { decideWindowOpen, isAllowedNavigation, isExternalUrlAllowed, isPermissionGranted } from './window-policy'

export type OriginGetter = () => string

/**
 * A 1 px rule directly under the application menu, so the menu bar and the page do not
 * blend into each other. Presentation only: a fixed overlay line, injected by the Desktop
 * into the page it hosts -- the SPA itself is unchanged (UDR-0151 D1). The colour is
 * semi-transparent grey so it reads on both light and dark themes.
 */
export const MENU_SEPARATOR_CSS =
  'html::before{content:"";position:fixed;top:0;left:0;right:0;height:1px;background:rgba(127,134,150,.45);z-index:2147483647;pointer-events:none}'

const CHAT_PREFS = {
  partition: PARTITION,
  nodeIntegration: false,
  contextIsolation: true,
  sandbox: true,
  webSecurity: true,
  webviewTag: false,
  spellcheck: false,
} as const

/**
 * Window icon (title bar + taskbar). A packaged build takes it from the executable, but
 * development runs under electron.exe and would otherwise show the Electron logo, so the
 * generated brand icon is passed explicitly (CTR-0213 `pnpm icons`).
 */
function appIcon(): string | undefined {
  const icon = join(__dirname, '..', '..', 'build-resources', 'icon.ico')
  return existsSync(icon) ? icon : undefined
}

export function createBootstrapWindow(preload: string, html: string): BrowserWindow {
  const win = new BrowserWindow({
    width: 560,
    height: 440,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    title: DISPLAY_NAME,
    icon: appIcon(),
    show: false,
    backgroundColor: '#0b1220',
    webPreferences: { preload, nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true },
  })
  win.setMenuBarVisibility(false)
  win.webContents.on('will-navigate', (event) => event.preventDefault())
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  win.once('ready-to-show', () => win.show())
  void win.loadFile(html)
  return win
}

let sessionConfigured = false

/** The chat partition, with permission and download handlers installed once. */
export function chatSession(getOrigin: OriginGetter, log: RotatingLog): Session {
  const ses = session.fromPartition(PARTITION)
  if (!sessionConfigured) {
    sessionConfigured = true
    ses.setPermissionRequestHandler((wc, permission, callback, details) => {
      const url = details.requestingUrl || wc.getURL()
      const granted = isPermissionGranted(permission, url, getOrigin())
      if (!granted) log.write(`permission denied: ${permission} for ${url}`)
      callback(granted)
    })
    ses.setPermissionCheckHandler((_wc, permission, requestingOrigin) => isPermissionGranted(permission, requestingOrigin, getOrigin()))
    ses.on('will-download', (_event, item) => {
      item.once('done', (_e, state) => log.write(`download ${state}: ${item.getFilename()}`))
    })
  }
  return ses
}

/** Per-launch token cookie read by the local access guard (CTR-0211). Session-only. */
export async function setDesktopCookie(ses: Session, origin: string, token: string): Promise<void> {
  await ses.cookies.set({ url: origin, name: DESKTOP_COOKIE, value: token, httpOnly: true, sameSite: 'strict', path: '/' })
}

function attachPolicy(wc: WebContents, getOrigin: OriginGetter, log: RotatingLog): void {
  // Re-applied on every load (including in-app reloads and the SPA's own navigations).
  wc.on('did-finish-load', () => {
    void wc.insertCSS(MENU_SEPARATOR_CSS).catch(() => undefined)
  })
  wc.on('will-navigate', (event, url) => {
    if (isAllowedNavigation(url, getOrigin())) return
    event.preventDefault()
    if (isExternalUrlAllowed(url)) void shell.openExternal(url)
    else log.write(`blocked navigation: ${url}`)
  })
  wc.on('will-attach-webview', (event) => event.preventDefault())
  wc.setWindowOpenHandler(({ url }) => {
    const decision = decideWindowOpen(url, getOrigin())
    if (decision === 'desktop-window') {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: { width: 1100, height: 800, title: DISPLAY_NAME, icon: appIcon(), webPreferences: { ...CHAT_PREFS } },
      }
    }
    if (decision === 'external') void shell.openExternal(url)
    else log.write(`blocked window.open: ${url}`)
    return { action: 'deny' }
  })
  wc.on('did-create-window', (child) => attachPolicy(child.webContents, getOrigin, log))
}

function boundsVisible(b: WindowBounds): boolean {
  if (b.width < 400 || b.height < 300) return false
  return screen.getAllDisplays().some((d) => {
    const a = d.workArea
    return b.x < a.x + a.width && b.x + b.width > a.x && b.y < a.y + a.height && b.y + b.height > a.y
  })
}

export function createChatWindow(opts: { url: string; getOrigin: OriginGetter; log: RotatingLog; bounds?: WindowBounds }): BrowserWindow {
  const bounds = opts.bounds && boundsVisible(opts.bounds) ? opts.bounds : { width: 1280, height: 860 }
  const win = new BrowserWindow({
    ...bounds,
    minWidth: 400,
    minHeight: 300,
    title: DISPLAY_NAME,
    icon: appIcon(),
    show: false,
    webPreferences: { ...CHAT_PREFS },
  })
  attachPolicy(win.webContents, opts.getOrigin, opts.log)
  void win.loadURL(opts.url)
  return win
}

export interface MenuActions {
  openData: () => void
  openLogs: () => void
  exportDiagnostics: () => void
  rebuild: () => void
  checkUpdates: () => void
  about: () => void
}

export function buildMenu(actions: MenuActions, devTools: boolean): Menu {
  const view: MenuItemConstructorOptions[] = [{ role: 'reload' }, { role: 'forceReload' }]
  if (devTools) view.push({ role: 'toggleDevTools' })
  view.push({ type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'togglefullscreen' })
  const template: MenuItemConstructorOptions[] = [
    { label: 'File', submenu: [{ role: 'quit' }] },
    { role: 'editMenu' },
    { label: 'View', submenu: view },
    {
      label: 'Help',
      submenu: [
        { label: 'Open Data Folder', click: () => actions.openData() },
        { label: 'Open Logs', click: () => actions.openLogs() },
        { label: 'Export Diagnostics...', click: () => actions.exportDiagnostics() },
        { label: 'Rebuild Environment...', click: () => actions.rebuild() },
        { type: 'separator' },
        { label: 'Check for Updates...', click: () => actions.checkUpdates() },
        { label: `About ${DISPLAY_NAME}`, click: () => actions.about() },
      ],
    },
  ]
  return Menu.buildFromTemplate(template)
}
