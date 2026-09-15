/**
 * Native host architecture (UDR-0151 D14, RES-0006 F4).
 *
 * The Desktop ships an x64 build only. Windows 11 on ARM runs it under x64 emulation, and
 * an emulated process reports `process.arch === 'x64'` -- correctly, since that IS the
 * architecture of the code. So the platform guard and the payload manifest need no change.
 *
 * What IS worth knowing is the architecture of the MACHINE, for one reason: a support
 * report from an ARM device must say so, because slow startup there is expected rather
 * than a fault. Windows tells us without a native module: an x64 process on an arm64 host
 * carries PROCESSOR_ARCHITEW6432 (the host) next to PROCESSOR_ARCHITECTURE (the process),
 * and on a real x64 host the first variable is absent.
 *
 * Pure function over an environment so it is testable without Windows.
 */

export type Arch = 'x64' | 'arm64' | 'x86' | 'unknown'

export interface HostArch {
  /** Architecture of the running process ("what the code is"). */
  process: Arch
  /** Architecture of the machine ("what the silicon is"). */
  native: Arch
  /** The process is running under an architecture translation layer. */
  emulated: boolean
}

function normalize(value: string | undefined): Arch {
  switch ((value ?? '').toUpperCase()) {
    case 'AMD64':
    case 'X64':
      return 'x64'
    case 'ARM64':
      return 'arm64'
    case 'X86':
    case 'IA32':
      return 'x86'
    default:
      return 'unknown'
  }
}

export function detectHostArch(processArch: string, env: NodeJS.ProcessEnv | Record<string, string | undefined>): HostArch {
  const proc = normalize(processArch)
  // Set only when the process architecture differs from the host's.
  const host = normalize(env.PROCESSOR_ARCHITEW6432)
  const native = host === 'unknown' ? (normalize(env.PROCESSOR_ARCHITECTURE) === 'unknown' ? proc : normalize(env.PROCESSOR_ARCHITECTURE)) : host
  return { process: proc, native, emulated: native !== 'unknown' && proc !== 'unknown' && native !== proc }
}

/** One-line form for desktop.log and the startup error screen. */
export function describeHostArch(h: HostArch): string {
  return h.emulated ? `${h.process} on ${h.native} (emulated)` : h.process
}
