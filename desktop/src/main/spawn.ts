/**
 * The ONE place the Desktop starts external processes (UDR-0151 D9, invariant I4).
 *
 * Always an executable path plus an argument array, never a shell string: user paths
 * with spaces or non-ASCII characters cannot be re-parsed into extra commands.
 */

import { type ChildProcess, spawn } from 'node:child_process'

export interface RunOptions {
  cwd?: string
  env?: Record<string, string>
  timeoutMs?: number
  onOutput?: (chunk: string) => void
}

export interface RunResult {
  code: number | null
  stdout: string
  stderr: string
  timedOut: boolean
}

const MAX_CAPTURE = 2 * 1024 * 1024

export function startProcess(
  exe: string,
  args: readonly string[],
  opts: { cwd?: string; env?: Record<string, string> } = {},
): ChildProcess {
  return spawn(exe, [...args], {
    cwd: opts.cwd,
    env: opts.env,
    shell: false,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
}

export function runProcess(exe: string, args: readonly string[], opts: RunOptions = {}): Promise<RunResult> {
  return new Promise((resolve) => {
    let stdout = ''
    let stderr = ''
    let timedOut = false
    let settled = false
    const child = spawn(exe, [...args], {
      cwd: opts.cwd,
      env: opts.env,
      shell: false,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const timer =
      opts.timeoutMs !== undefined
        ? setTimeout(() => {
            timedOut = true
            child.kill()
          }, opts.timeoutMs)
        : undefined
    const collect = (which: 'out' | 'err') => (buf: Buffer) => {
      const text = buf.toString('utf8')
      opts.onOutput?.(text)
      if (which === 'out' && stdout.length < MAX_CAPTURE) stdout += text
      if (which === 'err' && stderr.length < MAX_CAPTURE) stderr += text
    }
    child.stdout?.on('data', collect('out'))
    child.stderr?.on('data', collect('err'))
    const finish = (code: number | null) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve({ code, stdout, stderr, timedOut })
    }
    child.on('error', (err) => {
      stderr += String(err)
      finish(-1)
    })
    child.on('close', (code) => finish(code))
  })
}
