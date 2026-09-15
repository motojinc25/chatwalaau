/**
 * Shared constants and the error taxonomy of ChatWalaʻau Desktop (PRP-0167, CAP-011).
 *
 * Pure module: no Electron import, so it can be unit-tested.
 */

/** Prefix of every launcher event line on the backend's stdout (CTR-0208). */
export const EVENT_PREFIX = '@@CWDESKTOP@@ '

/** ASCII name used for directories and the executable (UDR-0151, PRP-0167 Q2). */
export const APP_DIR_NAME = 'ChatWalaau'

/** Display name (window titles, dialogs). */
export const DISPLAY_NAME = 'ChatWalaʻau'

/** Persistent session partition of the chat windows (CTR-0214). */
export const PARTITION = 'persist:chatwalaau-desktop'

/** Cookie carrying the per-launch token checked by the local access guard (CTR-0211). */
export const DESKTOP_COOKIE = 'cw_desktop'

/** Loopback host the backend is bound to and addressed by (UDR-0151 D8). */
export const LOOPBACK_HOST = '127.0.0.1'

export const RELEASES_URL = 'https://github.com/motojinc25/chatwalaau/releases'

export const DEFAULT_TIMEOUTS = {
  environmentBuildMs: 15 * 60_000,
  backendReadyMs: 120_000,
  gracefulStopMs: 30_000,
}

/** Update check interval after the first check (CTR-0212). */
export const UPDATE_INTERVAL_MS = 6 * 60 * 60_000

/** Minimum free disk space before an environment build starts. */
export const MIN_FREE_BYTES = 3 * 1024 ** 3

export type ErrorCode =
  | 'PAYLOAD_INVALID'
  | 'UNSUPPORTED_PLATFORM'
  | 'ENV_CREATE_FAILED'
  | 'DEPENDENCY_CONFLICT'
  | 'PACKAGE_UNAVAILABLE'
  | 'BACKEND_START_FAILED'
  | 'PORT_CONFLICT'
  | 'PERMISSION_DENIED'
  | 'TLS_PROXY_ERROR'
  | 'DISK_FULL'

export class DesktopError extends Error {
  readonly code: ErrorCode
  readonly detail?: string

  constructor(code: ErrorCode, message: string, detail?: string) {
    super(message)
    this.name = 'DesktopError'
    this.code = code
    this.detail = detail
  }
}

/** Startup state machine of the Main process (RES-0004 section 8.1). */
export type Phase =
  | 'BOOT'
  | 'PRECHECK'
  | 'PREPARE_RUNTIME'
  | 'PREPARE_ENV'
  | 'START_BACKEND'
  | 'WAIT_READY'
  | 'RUNNING'
  | 'STOPPING'
  | 'STOPPED'
  | 'ERROR'
