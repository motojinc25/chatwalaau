/**
 * ChatWalaʻau Desktop -- Electron Main entry (PRP-0167, FEAT-0068, CAP-011, UDR-0151).
 *
 * The Desktop is a shell around the UNCHANGED product (UDR-0151 D1): it prepares a bundled
 * Python environment, starts the existing FastAPI app through a thin launcher, and shows
 * the existing /chat page. No HTTP API is replaced by IPC and the SPA does not know it
 * runs inside Electron.
 *
 *   BOOT -> PRECHECK -> PREPARE_RUNTIME -> PREPARE_ENV -> START_BACKEND -> WAIT_READY -> RUNNING
 *                                                   \-> ERROR (retry)          RUNNING -> STOPPING -> STOPPED
 */

import { existsSync, readFileSync } from 'node:fs'
import { win32 as p } from 'node:path'
import { app, BrowserWindow, dialog, type IpcMainInvokeEvent, ipcMain, Menu, type MessageBoxOptions, shell } from 'electron'
import { buildChildEnv } from './child-env'
import { type DesktopConfig, loadConfig, saveConfig } from './config'
import { APP_DIR_NAME, DesktopError, DISPLAY_NAME, type Phase } from './constants'
import { collectDiagnostics, exportDiagnostics } from './diagnostics'
import { EnvironmentManager, ensureProfile, isForeignPythonDir, makeLayoutDirs, type ReadyEnvironment } from './environment'
import { RotatingLog } from './log'
import { computeLayout } from './paths'
import { isPortFree, pickPort } from './ports'
import { BackendSupervisor, type LaunchTarget } from './supervisor'
import { readBuildInfo, UpdateManager } from './updater'
import { buildMenu, chatSession, createBootstrapWindow, createChatWindow, setDesktopCookie } from './windows'

// ---- where things live -------------------------------------------------------------------

const devMode = !app.isPackaged
const devToolsAllowed = devMode || process.argv.includes('--diagnostics')
/** desktop/build/main -> repository root (dev mode only). */
const repoRoot = p.resolve(__dirname, '..', '..', '..')
const dataRoot = devMode
  ? p.join(repoRoot, 'desktop', '.dev-profile')
  : p.join(process.env.LOCALAPPDATA || app.getPath('appData'), APP_DIR_NAME)
const layout = computeLayout(dataRoot)

// Chromium's own profile (partitions, caches) under the Desktop root, never Roaming (UDR-0151 D5).
app.setPath('userData', p.join(layout.desktopDir, 'electron'))
app.setAppUserModelId('cc.wedx.chatwalaau')

const desktopLog = new RotatingLog(p.join(layout.logsDir, 'desktop.log'))
const backendLog = new RotatingLog(p.join(layout.logsDir, 'backend.log'))
const envLog = new RotatingLog(p.join(layout.logsDir, 'environment.log'))

let config: DesktopConfig = loadConfig(layout.configFile)
const buildInfo = readBuildInfo()
const supervisor = new BackendSupervisor(desktopLog, backendLog)

// ---- state ---------------------------------------------------------------------------------

interface UiState {
  phase: Phase
  message: string
  error?: { code: string; message: string; detail?: string }
  logsDir: string
  devMode: boolean
}

let ui: UiState = { phase: 'BOOT', message: 'Starting', logsDir: layout.logsDir, devMode }
let bootstrapWin: BrowserWindow | null = null
let chatWin: BrowserWindow | null = null
let envManager: EnvironmentManager | null = null
let ready: ReadyEnvironment | null = null
let childEnv: Record<string, string> | undefined
let expectedVersion = ''
let sandboxPort: number | undefined
let booting = false
let closeConfirmed = false
let quitReady = false
let stopping = false

const currentOrigin = (): string => supervisor.current?.origin ?? ''

const updater = new UpdateManager(config.autoUpdate, buildInfo, desktopLog, () => chatWin ?? bootstrapWin, prepareForExit)

function setPhase(phase: Phase, message: string): void {
  ui = { ...ui, phase, message, error: phase === 'ERROR' ? ui.error : undefined }
  desktopLog.write(`[${phase}] ${message}`)
  if (bootstrapWin && !bootstrapWin.isDestroyed()) bootstrapWin.webContents.send('desktop:state', ui)
}

function ensureBootstrap(): BrowserWindow {
  if (bootstrapWin && !bootstrapWin.isDestroyed()) return bootstrapWin
  bootstrapWin = createBootstrapWindow(p.join(__dirname, '..', 'preload', 'bootstrap.js'), p.join(__dirname, '..', 'bootstrap-ui', 'index.html'))
  bootstrapWin.on('closed', () => {
    bootstrapWin = null
  })
  return bootstrapWin
}

function showError(err: unknown): void {
  const e = err instanceof DesktopError ? err : new DesktopError('BACKEND_START_FAILED', err instanceof Error ? err.message : String(err))
  ui.error = { code: e.code, message: e.message, detail: e.detail?.slice(-4000) }
  desktopLog.write(`error ${e.code}: ${e.message}${e.detail ? `\n${e.detail}` : ''}`)
  ensureBootstrap().show()
  setPhase('ERROR', e.message)
}

async function messageBox(opts: MessageBoxOptions): Promise<number> {
  const parent = chatWin && !chatWin.isDestroyed() ? chatWin : bootstrapWin && !bootstrapWin.isDestroyed() ? bootstrapWin : null
  const r = parent ? await dialog.showMessageBox(parent, opts) : await dialog.showMessageBox(opts)
  return r.response
}

// ---- boot ----------------------------------------------------------------------------------

function prepareDev(): LaunchTarget {
  setPhase('PRECHECK', 'Development mode: using backend/.venv')
  const venv = p.join(repoRoot, 'backend', '.venv')
  const python = p.join(venv, 'Scripts', 'python.exe')
  if (!existsSync(python)) throw new DesktopError('ENV_CREATE_FAILED', 'backend/.venv was not found. Run "uv sync" in backend/ first.')
  makeLayoutDirs(layout)
  ensureProfile(layout, p.join(repoRoot, 'backend', 'src', 'app', 'templates', '.env.template'))
  childEnv = buildChildEnv(
    process.env,
    { envDir: venv, uvCacheDir: layout.cacheUv, pipCacheDir: layout.cachePip, tmpDir: layout.tmpDir },
    isForeignPythonDir,
  )
  const m = readFileSync(p.join(repoRoot, 'backend', 'pyproject.toml'), 'utf8').match(/^version\s*=\s*"([^"]+)"/m)
  return {
    python,
    launcher: p.join(repoRoot, 'desktop', 'python', 'backend_launcher.py'),
    profileDir: layout.profileDir,
    env: childEnv,
    envId: null,
    expectedVersion: m?.[1] ?? '',
  }
}

async function preparePackaged(): Promise<LaunchTarget> {
  const payloadDir = p.join(process.resourcesPath, 'desktop-payload')
  envManager = envManager ?? new EnvironmentManager(layout, payloadDir, envLog, config.timeouts.environmentBuildMs)
  ready = await envManager.ensure(setPhase)
  ensureProfile(layout, p.join(ready.envDir, 'Lib', 'site-packages', 'app', 'templates', '.env.template'))
  childEnv = envManager.childEnv(ready)
  return {
    python: ready.python,
    launcher: p.join(payloadDir, 'launcher', 'backend_launcher.py'),
    profileDir: layout.profileDir,
    env: childEnv,
    envId: ready.envId,
    expectedVersion: ready.manifest.backendVersion,
  }
}

async function boot(): Promise<void> {
  if (booting) return
  booting = true
  closeConfirmed = false
  ui.error = undefined
  try {
    setPhase('BOOT', 'Starting')
    const target = devMode ? prepareDev() : await preparePackaged()
    expectedVersion = target.expectedVersion
    setPhase('START_BACKEND', 'Starting the ChatWalaʻau backend')
    sandboxPort = await pickPort(config.lastSandboxPort, config.lastPort ? [config.lastPort] : [])
    setPhase('WAIT_READY', 'Waiting for the backend to become ready (MCP servers may take a moment)')
    const running = await supervisor.start({
      ...target,
      preferredPort: config.lastPort,
      sandboxPort,
      readyTimeoutMs: config.timeouts.backendReadyMs,
    })
    config = { ...config, lastPort: running.port, lastSandboxPort: sandboxPort }
    saveConfig(layout.configFile, config)

    const ses = chatSession(currentOrigin, desktopLog)
    await setDesktopCookie(ses, running.origin, running.token)
    openChatWindow(`${running.origin}/chat`)
    setPhase('RUNNING', `Running on ${running.origin}`)

    if (envManager) {
      envManager.markLaunchedOk()
      void offerReinstall()
    }
    updater.start()
    setTimeout(() => void checkSandbox(), 20_000)
  } catch (err) {
    await supervisor.stop(5_000).catch(() => undefined)
    showError(err)
  } finally {
    booting = false
  }
}

function openChatWindow(url: string): void {
  const win = createChatWindow({ url, getOrigin: currentOrigin, log: desktopLog, bounds: config.window?.bounds })
  chatWin = win
  win.once('ready-to-show', () => {
    win.show()
    if (bootstrapWin && !bootstrapWin.isDestroyed()) bootstrapWin.close()
  })
  win.on('close', (event) => onChatClose(event, win))
  win.on('closed', () => {
    if (chatWin === win) chatWin = null
  })
}

/** Gap G2: the existing sandbox logs a bind failure and carries on; we at least surface it in the log. */
async function checkSandbox(): Promise<void> {
  if (!sandboxPort || !supervisor.current) return
  if (await isPortFree(sandboxPort)) {
    desktopLog.write(`MCP Apps sandbox is not listening on ${sandboxPort} (normal when no MCP server started; otherwise see backend.log)`)
  }
}

// ---- close / quit (UDR-0151 D9) ------------------------------------------------------------

async function scheduledWorkWarning(): Promise<string | null> {
  const parts: string[] = []
  let unknown = false
  const cron = await supervisor.apiGet('/api/cron/jobs')
  if (cron.status === 200) {
    const jobs = ((cron.json as { jobs?: { enabled?: boolean }[] })?.jobs ?? []).filter((j) => j.enabled)
    if (jobs.length) parts.push(`${jobs.length} scheduled Cron job(s) will not run while ${DISPLAY_NAME} is closed.`)
  } else if (cron.status !== 404) unknown = true
  const pipeline = await supervisor.apiGet('/api/pipeline/jobs')
  if (pipeline.status === 200) {
    const jobs = ((pipeline.json as { jobs?: { status?: string }[] })?.jobs ?? []).filter((j) => j.status === 'running' || j.status === 'pending')
    if (jobs.length) parts.push(`${jobs.length} pipeline job(s) are pending or running and will stop.`)
  } else if (pipeline.status !== 404) unknown = true
  if (parts.length) return parts.join('\n')
  return unknown ? `Scheduled work (Cron, Pipeline) does not run while ${DISPLAY_NAME} is closed.` : null
}

function onChatClose(event: Electron.Event, win: BrowserWindow): void {
  if (closeConfirmed || quitReady) return
  event.preventDefault()
  void (async () => {
    const warning = await scheduledWorkWarning()
    if (warning) {
      const r = await messageBox({ type: 'question', buttons: ['Quit', 'Cancel'], defaultId: 1, cancelId: 1, noLink: true, message: `Quit ${DISPLAY_NAME}?`, detail: warning })
      if (r !== 0) return
    }
    closeConfirmed = true
    config = { ...config, window: { bounds: win.getBounds() } }
    app.quit()
  })()
}

async function prepareForExit(): Promise<void> {
  if (quitReady) return
  setPhase('STOPPING', 'Stopping the backend')
  if (chatWin && !chatWin.isDestroyed()) config = { ...config, window: { bounds: chatWin.getBounds() } }
  try {
    saveConfig(layout.configFile, config)
  } catch {
    // best effort
  }
  closeConfirmed = true
  await supervisor.stop(config.timeouts.gracefulStopMs).catch((e: unknown) => desktopLog.write(`stop failed: ${String(e)}`))
  updater.dispose()
  setPhase('STOPPED', 'Stopped')
  quitReady = true
}

// ---- actions -------------------------------------------------------------------------------

async function offerReinstall(): Promise<void> {
  if (!envManager || !ready) return
  const pending = envManager.readPendingReinstall()
  if (!pending || pending.packages.length === 0) return
  const shown = pending.packages.slice(0, 30).join('\n') + (pending.packages.length > 30 ? `\n... and ${pending.packages.length - 30} more` : '')
  const r = await messageBox({
    type: 'question',
    buttons: ['Reinstall', 'Not Now', 'Skip'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
    message: `Reinstall ${pending.packages.length} package(s) you added before?`,
    detail: `These packages were installed in the previous environment (for example by the assistant) and are not part of ${DISPLAY_NAME}:\n\n${shown}\n\nReinstalling needs an internet connection.`,
  })
  if (r === 1) return
  envManager.clearPendingReinstall()
  if (r === 2) return
  const result = await envManager.reinstall(ready, pending.packages)
  const failed = result.failed.map((f) => `${f.pkg} (${f.reason})`).join('\n')
  await messageBox({
    type: result.failed.length ? 'warning' : 'info',
    buttons: ['OK'],
    message: `Reinstalled ${result.installed.length} of ${pending.packages.length} package(s).`,
    detail: `${failed ? `Not reinstalled:\n${failed}\n\n` : ''}New tool runs see them now; restart ${DISPLAY_NAME} for the backend itself to import them.`,
  })
}

async function rebuildEnvironment(): Promise<void> {
  if (devMode) {
    await messageBox({ type: 'info', buttons: ['OK'], message: 'Not available in development mode', detail: 'Development mode uses backend/.venv; run "uv sync" there instead.' })
    return
  }
  const r = await messageBox({
    type: 'warning',
    buttons: ['Rebuild', 'Cancel'],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
    message: 'Rebuild the Python environment?',
    detail: `${DISPLAY_NAME} stops, recreates its Python environment from the installer (offline) and starts again. Your settings, conversations, uploads and RAG data are kept. Packages you added are listed afterwards and can be reinstalled.`,
  })
  if (r !== 0) return
  closeConfirmed = true
  ensureBootstrap().show()
  for (const w of BrowserWindow.getAllWindows()) if (w !== bootstrapWin) w.destroy()
  chatWin = null
  setPhase('STOPPING', 'Stopping the backend')
  await supervisor.stop(config.timeouts.gracefulStopMs)
  envManager = envManager ?? new EnvironmentManager(layout, p.join(process.resourcesPath, 'desktop-payload'), envLog, config.timeouts.environmentBuildMs)
  await envManager.requestRebuild()
  ready = null
  await boot()
}

async function doExportDiagnostics(): Promise<void> {
  let isolation: unknown = null
  if (envManager && ready) isolation = await envManager.isolationReport(ready.python, envManager.childEnv(ready)).catch((e: unknown) => String(e))
  const running = supervisor.current
  const data = await collectDiagnostics(
    {
      phase: ui.phase,
      lastError: ui.error ? { code: ui.error.code, message: ui.error.message } : undefined,
      devMode,
      signedBuild: buildInfo.signed,
      dataRoot,
      profileDir: layout.profileDir,
      logsDir: layout.logsDir,
      envId: ready?.envId ?? null,
      python: ready?.python ?? (devMode ? p.join(repoRoot, 'backend', '.venv', 'Scripts', 'python.exe') : undefined),
      backendVersion: expectedVersion,
      isolation,
      origin: running?.origin,
      port: running?.port,
      sandboxPort,
    },
    childEnv,
  )
  const file = await exportDiagnostics(chatWin ?? bootstrapWin, data, running ? [running.token] : [])
  if (file) desktopLog.write(`diagnostics exported to ${file}`)
}

async function about(): Promise<void> {
  await messageBox({
    type: 'info',
    buttons: ['OK'],
    message: `${DISPLAY_NAME} Desktop ${app.getVersion()}`,
    detail: [
      `Backend: ${expectedVersion || 'unknown'}`,
      `Electron ${process.versions.electron} / Chromium ${process.versions.chrome}`,
      `Build: ${devMode ? 'development' : buildInfo.signed ? 'signed' : 'unsigned'}`,
      `Data: ${layout.profileDir}`,
      '',
      'Developer: Jingun Jung',
      'Backed by WeDX Digital Twins Solutions',
    ].join('\n'),
  })
}

// ---- IPC (bootstrap window only) -----------------------------------------------------------

function fromBootstrap(event: IpcMainInvokeEvent): boolean {
  return (
    bootstrapWin !== null &&
    !bootstrapWin.isDestroyed() &&
    event.sender === bootstrapWin.webContents &&
    (event.senderFrame?.url ?? '').startsWith('file:')
  )
}

function registerIpc(): void {
  ipcMain.handle('desktop:get-state', (e) => (fromBootstrap(e) ? ui : null))
  ipcMain.handle('desktop:retry', (e) => {
    if (fromBootstrap(e) && ui.phase === 'ERROR') void boot()
  })
  ipcMain.handle('desktop:open-logs', (e) => {
    if (fromBootstrap(e)) void shell.openPath(layout.logsDir)
  })
  ipcMain.handle('desktop:rebuild', (e) => {
    if (fromBootstrap(e)) void rebuildEnvironment()
  })
  ipcMain.handle('desktop:quit', (e) => {
    if (fromBootstrap(e)) app.quit()
  })
}

// ---- lifecycle -----------------------------------------------------------------------------

process.on('uncaughtException', (err) => desktopLog.write(`uncaught: ${err.stack ?? String(err)}`))
process.on('unhandledRejection', (err) => desktopLog.write(`unhandled rejection: ${String(err)}`))

if (!app.requestSingleInstanceLock()) {
  // A second launch never starts a second backend (UDR-0151 D9).
  app.quit()
} else {
  app.on('second-instance', () => {
    const win = chatWin ?? bootstrapWin
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore()
      win.show()
      win.focus()
    }
  })

  app.on('web-contents-created', (_e, wc) => {
    wc.on('will-attach-webview', (event) => event.preventDefault())
  })

  supervisor.on('crashed', (code: number | null) => {
    closeConfirmed = true
    ensureBootstrap().show()
    for (const w of BrowserWindow.getAllWindows()) if (w !== bootstrapWin) w.destroy()
    chatWin = null
    showError(new DesktopError('BACKEND_START_FAILED', `The backend stopped unexpectedly (exit code ${code}).`, 'See backend.log in the logs folder, then choose Retry.'))
  })

  app.on('before-quit', (event) => {
    if (quitReady) return
    event.preventDefault()
    if (stopping) return
    stopping = true
    void prepareForExit().finally(() => app.quit())
  })

  app.on('window-all-closed', () => app.quit())

  void app.whenReady().then(async () => {
    Menu.setApplicationMenu(
      buildMenu(
        {
          openData: () => void shell.openPath(layout.profileDir),
          openLogs: () => void shell.openPath(layout.logsDir),
          exportDiagnostics: () => void doExportDiagnostics(),
          rebuild: () => void rebuildEnvironment(),
          checkUpdates: () => void updater.checkNow(),
          about: () => void about(),
        },
        devToolsAllowed,
      ),
    )
    registerIpc()
    ensureBootstrap()
    desktopLog.write(`ChatWalaau Desktop ${app.getVersion()} starting (${devMode ? 'dev' : 'packaged'}, data ${dataRoot})`)
    await boot()
  })
}
