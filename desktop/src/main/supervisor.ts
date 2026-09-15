/**
 * BackendSupervisor (CTR-0208, UDR-0151 D7/D9): spawns the launcher, runs the handshake,
 * decides readiness, and stops the backend gracefully.
 */

import type { ChildProcess } from 'node:child_process'
import { randomBytes, randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { createInterface } from 'node:readline'
import { DESKTOP_COOKIE, DesktopError, type ErrorCode, LOOPBACK_HOST } from './constants'
import { encodeCommand, parseEventLine } from './events'
import type { RotatingLog } from './log'
import { startProcess } from './spawn'

export interface LaunchTarget {
  python: string
  launcher: string
  profileDir: string
  env: Record<string, string>
  envId: string | null
  expectedVersion: string
}

export interface LaunchSpec extends LaunchTarget {
  preferredPort?: number
  sandboxPort: number
  readyTimeoutMs: number
}

export interface RunningBackend {
  port: number
  origin: string
  pid: number
  token: string
  launchId: string
}

const KNOWN_CODES: ReadonlySet<string> = new Set<ErrorCode>([
  'PAYLOAD_INVALID',
  'UNSUPPORTED_PLATFORM',
  'ENV_CREATE_FAILED',
  'DEPENDENCY_CONFLICT',
  'PACKAGE_UNAVAILABLE',
  'BACKEND_START_FAILED',
  'PORT_CONFLICT',
  'PERMISSION_DENIED',
  'TLS_PROXY_ERROR',
  'DISK_FULL',
])

async function get(url: string, token: string, timeoutMs = 10_000): Promise<Response> {
  return fetch(url, { headers: { cookie: `${DESKTOP_COOKIE}=${token}` }, signal: AbortSignal.timeout(timeoutMs), redirect: 'manual' })
}

export class BackendSupervisor extends EventEmitter {
  private child: ChildProcess | null = null
  private running: RunningBackend | null = null
  private stopping = false

  constructor(
    private readonly log: RotatingLog,
    private readonly backendLog: RotatingLog,
  ) {
    super()
  }

  get current(): RunningBackend | null {
    return this.running
  }

  async start(spec: LaunchSpec): Promise<RunningBackend> {
    if (this.child) await this.stop(10_000)
    this.stopping = false
    const launchId = randomUUID()
    const token = randomBytes(32).toString('base64url')
    this.log.addSecret(token)
    this.backendLog.addSecret(token)

    const child = startProcess(spec.python, ['-u', spec.launcher], { cwd: spec.profileDir, env: spec.env })
    this.child = child
    this.log.write(`launcher spawned (pid ${child.pid}, launch ${launchId})`)
    // stdin stays open: EOF is the launcher's "the Desktop is gone" signal.
    child.stdin?.write(
      encodeCommand({
        type: 'start',
        launchId,
        token,
        port: spec.preferredPort ?? 0,
        sandboxPort: spec.sandboxPort,
        profileDir: spec.profileDir,
        envId: spec.envId,
      }),
    )

    return new Promise<RunningBackend>((resolve, reject) => {
      let settled = false
      const settle = (err: Error | null, value?: RunningBackend) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        if (err) {
          child.kill()
          reject(err)
        } else if (value) {
          resolve(value)
        }
      }
      const timer = setTimeout(
        () => settle(new DesktopError('BACKEND_START_FAILED', `The backend did not become ready within ${Math.round(spec.readyTimeoutMs / 1000)} s.`)),
        spec.readyTimeoutMs,
      )

      if (child.stdout) {
        createInterface({ input: child.stdout, crlfDelay: Infinity }).on('line', (line) => {
          const event = parseEventLine(line, launchId)
          if (!event) {
            this.backendLog.write(line)
            return
          }
          this.log.write(`launcher event: ${event.type}`)
          if (event.type === 'ready') {
            this.verifyHttp(event.port, token, spec.expectedVersion)
              .then(() => {
                this.running = { port: event.port, origin: `http://${LOOPBACK_HOST}:${event.port}`, pid: child.pid ?? 0, token, launchId }
                settle(null, this.running)
              })
              .catch((err: unknown) => settle(err instanceof Error ? err : new Error(String(err))))
          } else if (event.type === 'startup-error') {
            const code = KNOWN_CODES.has(event.code) ? (event.code as ErrorCode) : 'BACKEND_START_FAILED'
            settle(new DesktopError(code, event.message || 'The backend failed to start.', 'See backend.log in the logs folder.'))
          }
        })
      }
      if (child.stderr) createInterface({ input: child.stderr, crlfDelay: Infinity }).on('line', (line) => this.backendLog.write(line))

      child.on('error', (err) => settle(new DesktopError('BACKEND_START_FAILED', `Could not start Python: ${err.message}`)))
      child.on('exit', (code) => {
        this.log.write(`launcher exited (code ${code})`)
        const wasRunning = this.running !== null
        this.child = null
        this.running = null
        if (!settled) settle(new DesktopError('BACKEND_START_FAILED', `The backend exited during startup (exit code ${code}).`, 'See backend.log in the logs folder.'))
        else if (wasRunning && !this.stopping) this.emit('crashed', code)
        this.emit('exit', code)
      })
    })
  }

  /** Readiness beyond the event (UDR-0151 D7): status JSON + version, SPA shell + one asset. */
  private async verifyHttp(port: number, token: string, expectedVersion: string): Promise<void> {
    const origin = `http://${LOOPBACK_HOST}:${port}`
    const fail = (msg: string) => new DesktopError('BACKEND_START_FAILED', msg)

    const status = await get(`${origin}/api/auth/status`, token)
    if (!status.ok) throw fail(`GET /api/auth/status returned ${status.status}.`)
    const body = (await status.json()) as { version?: unknown }
    if (expectedVersion && body.version !== expectedVersion) {
      throw fail(`The backend reports version ${String(body.version)}, but ${expectedVersion} was expected.`)
    }

    const chat = await get(`${origin}/chat`, token)
    const html = await chat.text()
    if (!chat.ok || !/id=["']root["']/.test(html)) throw fail('GET /chat did not return the ChatWalaʻau web app (is the frontend bundled?).')
    const ref = html.match(/<script[^>]+src=["']([^"']+)["']/i) ?? html.match(/<link[^>]+href=["']([^"']+\.css)["']/i)
    if (!ref) throw fail('The web app page references no script or stylesheet.')
    const asset = await get(new URL(ref[1], origin).toString(), token)
    const type = asset.headers.get('content-type') ?? ''
    if (!asset.ok || !/javascript|css/.test(type)) throw fail(`A web app asset could not be loaded (${asset.status} ${type}).`)
  }

  /** GET an API path through the guard with the Desktop cookie. Never throws. */
  async apiGet(path: string): Promise<{ status: number; json: unknown }> {
    const r = this.running
    if (!r) return { status: 0, json: null }
    try {
      const res = await get(`${r.origin}${path}`, r.token, 5_000)
      let json: unknown = null
      try {
        json = await res.json()
      } catch {
        json = null
      }
      return { status: res.status, json }
    } catch {
      return { status: 0, json: null }
    }
  }

  /**
   * Graceful stop: shutdown command -> wait for exit. After the timeout only the launcher
   * PID this supervisor spawned is terminated; its Job Object then ends the descendants.
   */
  async stop(timeoutMs: number): Promise<void> {
    const child = this.child
    if (!child) return
    this.stopping = true
    const exited = new Promise<void>((resolve) => {
      if (child.exitCode !== null) resolve()
      else child.once('exit', () => resolve())
    })
    try {
      child.stdin?.write(encodeCommand({ type: 'shutdown' }))
      child.stdin?.end()
    } catch {
      // stdin already closed
    }
    const timedOut = await Promise.race([exited.then(() => false), new Promise<boolean>((r) => setTimeout(() => r(true), timeoutMs))])
    if (timedOut) {
      this.log.write('graceful stop timed out; terminating the launcher process')
      child.kill()
      await Promise.race([exited, new Promise((r) => setTimeout(r, 5_000))])
    }
    this.running = null
  }
}
