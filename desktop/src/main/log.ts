/**
 * Size-capped rotating log files with secret redaction (RES-0004 section 13).
 */

import { appendFileSync, existsSync, mkdirSync, renameSync, rmSync, statSync } from 'node:fs'
import { dirname } from 'node:path'

const SECRET_PATTERNS: RegExp[] = [
  // "Authorization: Bearer <cred>" must lose the credential, not just the scheme word.
  /((?:authorization|x-api-key|api[_-]?key|cookie|set-cookie|token|password|secret)\s*[:=]\s*)((?:bearer|basic)\s+\S+|"[^"]*"|'[^']*'|\S+)/gi,
  /(bearer\s+)[A-Za-z0-9._~+/=-]+/gi,
  /(https?:\/\/)[^/\s:@]+:[^/\s@]+@/gi,
]

/** Remove known secrets and credential-shaped substrings from a log line. Pure. */
export function redact(text: string, secrets: readonly string[] = []): string {
  let out = text
  for (const s of secrets) {
    if (s && s.length >= 8) out = out.split(s).join('<redacted>')
  }
  for (const re of SECRET_PATTERNS) {
    out = out.replace(re, (_m, lead: string) => `${lead}<redacted>${lead.endsWith('//') ? '@' : ''}`)
  }
  return out
}

export class RotatingLog {
  private readonly secrets: string[] = []

  constructor(
    private readonly file: string,
    private readonly maxBytes = 5 * 1024 * 1024,
    private readonly keep = 3,
  ) {
    mkdirSync(dirname(file), { recursive: true })
  }

  addSecret(secret: string): void {
    this.secrets.push(secret)
  }

  get path(): string {
    return this.file
  }

  write(line: string): void {
    try {
      this.rotateIfNeeded()
      appendFileSync(this.file, `${new Date().toISOString()} ${redact(line, this.secrets)}\n`, 'utf8')
    } catch {
      // Logging must never take the app down.
    }
  }

  private rotateIfNeeded(): void {
    if (!existsSync(this.file) || statSync(this.file).size < this.maxBytes) return
    const oldest = `${this.file}.${this.keep}`
    if (existsSync(oldest)) rmSync(oldest, { force: true })
    for (let i = this.keep - 1; i >= 1; i--) {
      const from = `${this.file}.${i}`
      if (existsSync(from)) renameSync(from, `${this.file}.${i + 1}`)
    }
    renameSync(this.file, `${this.file}.1`)
  }
}
