/**
 * Pinned third-party binaries bundled into the Desktop payload (CTR-0213, UDR-0151 D3).
 *
 * Changing a pin changes `runtimeId` and therefore every user's environment generation.
 * Update deliberately: new URL + SHA-256 from the upstream release's checksum file.
 */

export const PINS = {
  python: {
    version: '3.12.14',
    build: '20260901',
    url: 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz',
    sha256: 'e90c1b6419da3bd812dd73bb3de40287a21abf153438147639ec5e20375ea93f',
    // Stored in the payload under an ASCII name without "+".
    payloadName: 'cpython-3.12.14-20260901-win-x64.tar.gz',
  },
  uv: {
    version: '0.12.13',
    url: 'https://github.com/astral-sh/uv/releases/download/0.12.13/uv-x86_64-pc-windows-msvc.zip',
    sha256: 'a86c9dc7bad9b03f388583b7187c05fe9951c2e0d392217e8fd43d97787f6ec2',
  },
}

export const RUNTIME_ID = `cpython-${PINS.python.version}-${PINS.python.build}-win-x64`
