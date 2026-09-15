/**
 * UpdateManager (CTR-0212, UDR-0151 D11).
 *
 * electron-updater against the public repository's GitHub Releases. Silent download +
 * install only for SIGNED builds (publisher verification); an unsigned build only
 * NOTIFIES and links to the release page. Installation happens at quit or on explicit
 * consent, after the same graceful backend stop as quitting.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { app, type BrowserWindow, dialog, type MessageBoxOptions, shell } from 'electron'
import { autoUpdater, type UpdateInfo } from 'electron-updater'
import type { DesktopConfig } from './config'
import { DISPLAY_NAME, RELEASES_URL, UPDATE_INTERVAL_MS } from './constants'
import type { RotatingLog } from './log'

export interface BuildInfo {
  signed: boolean
  signingMode?: string
  builtAt?: string
}

/** build/build-info.json is written at build time by scripts/copy-static.mjs. */
export function readBuildInfo(): BuildInfo {
  try {
    const raw = JSON.parse(readFileSync(join(__dirname, '..', 'build-info.json'), 'utf8')) as Record<string, unknown>
    return {
      signed: raw.signed === true,
      signingMode: typeof raw.signingMode === 'string' ? raw.signingMode : undefined,
      builtAt: typeof raw.builtAt === 'string' ? raw.builtAt : undefined,
    }
  } catch {
    return { signed: false }
  }
}

export class UpdateManager {
  private wired = false
  private manual = false
  private readonly notified = new Set<string>()
  private timer: NodeJS.Timeout | undefined

  constructor(
    private readonly settings: DesktopConfig['autoUpdate'],
    private readonly info: BuildInfo,
    private readonly log: RotatingLog,
    private readonly getWindow: () => BrowserWindow | null,
    private readonly prepareInstall: () => Promise<void>,
  ) {}

  /** Periodic checks (packaged app, auto-update enabled). */
  start(): void {
    if (!app.isPackaged || !this.settings.enabled || this.timer) return
    this.wire()
    setTimeout(() => void this.check(false), 15_000)
    this.timer = setInterval(() => void this.check(false), UPDATE_INTERVAL_MS)
  }

  async checkNow(): Promise<void> {
    if (!app.isPackaged) {
      await this.say('Updates are checked only in the installed app.')
      return
    }
    this.wire()
    await this.check(true)
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = undefined
  }

  private wire(): void {
    if (this.wired) return
    this.wired = true
    autoUpdater.logger = {
      info: (m?: unknown) => this.log.write(`updater: ${String(m)}`),
      warn: (m?: unknown) => this.log.write(`updater warn: ${String(m)}`),
      error: (m?: unknown) => this.log.write(`updater error: ${String(m)}`),
      debug: () => {},
    }
    autoUpdater.autoDownload = this.info.signed && this.settings.autoDownload
    autoUpdater.autoInstallOnAppQuit = this.info.signed
    autoUpdater.allowPrerelease = false
    autoUpdater.allowDowngrade = false
    autoUpdater.on('update-available', (i: UpdateInfo) => void this.onAvailable(i))
    autoUpdater.on('update-not-available', () => {
      if (this.manual) void this.say('You are using the latest version.')
      this.manual = false
    })
    autoUpdater.on('update-downloaded', (i: UpdateInfo) => void this.onDownloaded(i))
    autoUpdater.on('error', (err: Error) => {
      // A network failure or a release without latest.yml means "no update", not an error dialog.
      this.log.write(`update check failed: ${err?.message ?? String(err)}`)
      if (this.manual) void this.say('Could not check for updates. Check your connection and try again later.')
      this.manual = false
    })
  }

  private async check(manual: boolean): Promise<void> {
    this.manual = manual
    try {
      await autoUpdater.checkForUpdates()
    } catch (err) {
      this.log.write(`update check threw: ${String(err)}`)
      if (manual) await this.say('Could not check for updates. Check your connection and try again later.')
      this.manual = false
    }
  }

  private async onAvailable(i: UpdateInfo): Promise<void> {
    const manual = this.manual
    this.manual = false
    if (!this.info.signed) {
      if (!manual && this.notified.has(i.version)) return
      this.notified.add(i.version)
      const r = await this.ask(
        `${DISPLAY_NAME} ${i.version} is available.`,
        'This build is not code-signed, so updates are installed manually: download the new installer from the release page and run it. Your settings and conversations are kept.',
        ['Open Download Page', 'Later'],
      )
      if (r === 0) void shell.openExternal(`${RELEASES_URL}/tag/v${i.version}`)
      return
    }
    if (!autoUpdater.autoDownload) {
      const r = await this.ask(`${DISPLAY_NAME} ${i.version} is available.`, 'Download it now? It installs when you restart.', ['Download', 'Later'])
      if (r === 0) void autoUpdater.downloadUpdate()
    }
  }

  private async onDownloaded(i: UpdateInfo): Promise<void> {
    const r = await this.ask(
      `${DISPLAY_NAME} ${i.version} is ready to install.`,
      'Restart now to update, or it installs the next time you quit. Your settings and conversations are kept.',
      ['Restart to Update', 'Later'],
    )
    if (r !== 0) return
    await this.prepareInstall()
    autoUpdater.quitAndInstall(false, true)
  }

  private async ask(message: string, detail: string, buttons: string[]): Promise<number> {
    const opts: MessageBoxOptions = { type: 'info', message, detail, buttons, defaultId: 0, cancelId: buttons.length - 1, noLink: true }
    const win = this.getWindow()
    const r = win && !win.isDestroyed() ? await dialog.showMessageBox(win, opts) : await dialog.showMessageBox(opts)
    return r.response
  }

  private async say(message: string): Promise<void> {
    await this.ask(message, '', ['OK'])
  }
}
