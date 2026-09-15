/**
 * Azure CLI tenant pre-check for `pnpm dev:full` (CTR-0213, DEVELOPMENT ONLY).
 *
 * Mirrors `frontend/dev-full.mjs` ensureAzureLogin: when the active credential lane is
 * `cli` and a tenant is pinned, probe for a Cognitive Services token in THAT tenant and
 * sign in only when the probe fails. A cached refresh token makes `az login` silent; an
 * empty or expired cache opens the interactive browser sign-in.
 *
 * Why here and not in the app: the packaged Desktop deliberately does NOT run an
 * az pre-check (UDR-0151 D7) -- it must start for users who never touch Azure. This is a
 * developer-terminal step, like dev:full's.
 *
 * Environment is read the way the backend will see it: process environment first (the
 * Desktop passes AZURE_* through to the backend), then the Desktop dev profile `.env`
 * (the backend's cwd in dev mode), then `backend/.env`.
 *
 * Usage: node scripts/dev-azure.mjs [--check-only] [--skip-auth-check]
 *        CW_SKIP_AZURE_CHECK=1 also skips.
 */

import { exec } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { promisify } from 'node:util'

const execAsync = promisify(exec)

const DESKTOP = resolve(import.meta.dirname, '..')
const ROOT = resolve(DESKTOP, '..')
const ENV_FILES = [join(DESKTOP, '.dev-profile', 'desktop', 'profile', '.env'), join(ROOT, 'backend', '.env')]
const SCOPE_RESOURCE = 'https://cognitiveservices.azure.com/'
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const args = new Set(process.argv.slice(2))
const checkOnly = args.has('--check-only')
const skip = args.has('--skip-auth-check') || process.env.CW_SKIP_AZURE_CHECK === '1'

const colors = { reset: '\x1b[0m', green: '\x1b[32m', yellow: '\x1b[33m', red: '\x1b[31m' }
const log = (message, color = colors.reset) =>
  console.log(`${color}[AZURE ${new Date().toLocaleTimeString()}]${colors.reset} ${message}`)

/** Minimal dotenv reader (the Desktop has no dotenv dependency). */
function readEnvFile(file) {
  const out = {}
  if (!existsSync(file)) return out
  for (const raw of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq < 1) continue
    let value = line.slice(eq + 1).trim()
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1)
    }
    out[line.slice(0, eq).trim()] = value
  }
  return out
}

function resolveSetting(name) {
  if ((process.env[name] ?? '').trim()) return { value: process.env[name].trim(), from: 'process environment' }
  for (const file of ENV_FILES) {
    const value = (readEnvFile(file)[name] ?? '').trim()
    if (value) return { value, from: file.replace(`${ROOT}\\`, '').replace(`${ROOT}/`, '') }
  }
  return { value: '', from: null }
}

if (skip) {
  log('Skipping the Azure CLI check (--skip-auth-check / CW_SKIP_AZURE_CHECK=1).', colors.yellow)
  process.exit(0)
}

// Credential lane, same precedence as app.azure_credential: API key > AZURE_CREDENTIAL_MODE.
const apiKey = resolveSetting('AZURE_OPENAI_API_KEY')
const mode = resolveSetting('AZURE_CREDENTIAL_MODE')
const lane = apiKey.value ? 'api-key' : (mode.value || 'cli').toLowerCase()
if (lane !== 'cli') {
  log(`Skipping the Azure CLI check (credential lane: ${lane}).`, colors.yellow)
  process.exit(0)
}

const tenant = resolveSetting('AZURE_TENANT_ID')
if (!tenant.value) {
  log(`AZURE_TENANT_ID is not set (looked in the process environment, ${ENV_FILES.map((f) => f.replace(`${ROOT}\\`, '')).join(' and ')}); skipping the Azure login check.`, colors.yellow)
  process.exit(0)
}
if (!UUID_RE.test(tenant.value)) {
  log(`AZURE_TENANT_ID must be a valid UUID, got "${tenant.value}" (from ${tenant.from}).`, colors.red)
  process.exit(1)
}

const tenantId = tenant.value
log(`Verifying the Azure CLI token for tenant ${tenantId} (from ${tenant.from})...`, colors.yellow)

// A token probe is stricter than `az account show`: it proves a usable refresh token for
// THIS tenant exists, which is exactly what AzureCliCredential will need.
const probe =
  `az account get-access-token --tenant ${tenantId} --resource ${SCOPE_RESOURCE} --output none --only-show-errors`

try {
  await execAsync(probe, { timeout: 30_000 })
  log(`Token already valid for tenant ${tenantId}.`, colors.green)
  process.exit(0)
} catch (error) {
  const first = String(error?.message ?? '').split('\n')[0]
  if (/is not recognized|not found|ENOENT/i.test(first)) {
    log('Azure CLI (az) was not found in PATH.', colors.red)
    log('Install the Azure CLI, or set AZURE_OPENAI_API_KEY in the dev profile .env, or run with CW_SKIP_AZURE_CHECK=1.', colors.red)
    process.exit(1)
  }
  if (checkOnly) {
    log(`No valid cached token for tenant ${tenantId} (${first}).`, colors.yellow)
    log(`Run: az login --tenant ${tenantId} --allow-no-subscriptions`, colors.yellow)
    process.exit(0)
  }
  log(`No valid cached token for tenant ${tenantId} (${first}). Logging in...`, colors.yellow)
}

try {
  // Silent when a refresh token is cached; opens the browser sign-in otherwise.
  await execAsync(`az login --tenant ${tenantId} --allow-no-subscriptions --only-show-errors`, { timeout: 180_000 })
  log(`Logged in to tenant ${tenantId}.`, colors.green)
} catch (error) {
  log(`Failed to log in to Azure: ${String(error?.message ?? '').split('\n')[0]}`, colors.red)
  log(`Please run manually: az login --tenant ${tenantId} --allow-no-subscriptions`, colors.red)
  log('Or start without the check: CW_SKIP_AZURE_CHECK=1 pnpm dev:full', colors.red)
  process.exit(1)
}
