/**
 * Bootstrap window preload (CTR-0214). Sandboxed; exposes ONLY state, retry, open logs,
 * rebuild environment and quit. The chat window has no preload at all (UDR-0151 D12).
 *
 * Written as a script (no import/export) so the compiled file needs no CommonJS `exports`
 * object inside the sandboxed preload environment.
 */

const { contextBridge, ipcRenderer } = require('electron') as typeof import('electron')

contextBridge.exposeInMainWorld('desktop', {
  getState: (): Promise<unknown> => ipcRenderer.invoke('desktop:get-state'),
  onState: (callback: (state: unknown) => void): (() => void) => {
    const handler = (_event: unknown, state: unknown) => callback(state)
    ipcRenderer.on('desktop:state', handler)
    return () => ipcRenderer.removeListener('desktop:state', handler)
  },
  retry: (): Promise<void> => ipcRenderer.invoke('desktop:retry'),
  openLogs: (): Promise<void> => ipcRenderer.invoke('desktop:open-logs'),
  rebuildEnvironment: (): Promise<void> => ipcRenderer.invoke('desktop:rebuild'),
  quit: (): Promise<void> => ipcRenderer.invoke('desktop:quit'),
})
