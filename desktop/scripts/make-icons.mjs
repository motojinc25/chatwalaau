/**
 * Build the Desktop's Windows image assets from the ONE brand source,
 * `frontend/public/favicon.svg` (CTR-0213, brand-assets.md).
 *
 *   favicon.svg --(PyMuPDF, backend venv)--> PNG sizes + raw RGB canvases
 *               --(this script)-----------> build-resources/icon.ico
 *                                           build-resources/installerSidebar.bmp
 *                                           build-resources/installerHeader.bmp
 *
 * The container formats are assembled here so nothing has to decode an image: Python
 * hands over PNG bytes (packed verbatim into the ICO, which is legal since Vista) and
 * raw 24-bit RGB (wrapped in a BMP header). No new dependency on either side.
 *
 * Run `pnpm icons` after changing favicon.svg; the outputs are committed.
 */

import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const DESKTOP = resolve(import.meta.dirname, '..')
const ROOT = resolve(DESKTOP, '..')
const SVG = join(ROOT, 'frontend', 'public', 'favicon.svg')
const OUT = join(DESKTOP, 'build-resources')
const ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
const SIDEBAR = { width: 164, height: 314 }
const HEADER = { width: 150, height: 57 }

const die = (msg) => {
  console.error(`icons ERROR: ${msg}`)
  process.exit(1)
}

/** ICO container: 6-byte header, 16-byte directory entry per image, then the PNGs. */
function buildIco(images) {
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0) // reserved
  header.writeUInt16LE(1, 2) // type: icon
  header.writeUInt16LE(images.length, 4)
  const directory = Buffer.alloc(16 * images.length)
  let offset = header.length + directory.length
  images.forEach((image, i) => {
    const at = i * 16
    directory.writeUInt8(image.size >= 256 ? 0 : image.size, at) // 0 means 256
    directory.writeUInt8(image.size >= 256 ? 0 : image.size, at + 1)
    directory.writeUInt8(0, at + 2) // palette size
    directory.writeUInt8(0, at + 3) // reserved
    directory.writeUInt16LE(1, at + 4) // colour planes
    directory.writeUInt16LE(32, at + 6) // bits per pixel
    directory.writeUInt32LE(image.data.length, at + 8)
    directory.writeUInt32LE(offset, at + 12)
    offset += image.data.length
  })
  return Buffer.concat([header, directory, ...images.map((i) => i.data)])
}

/** 24-bit bottom-up BMP from a top-down RGB buffer (NSIS accepts nothing fancier). */
function buildBmp(rgb, width, height) {
  const rowSize = Math.ceil((width * 3) / 4) * 4
  const pixels = Buffer.alloc(rowSize * height)
  for (let y = 0; y < height; y++) {
    const src = (height - 1 - y) * width * 3 // BMP rows run bottom-up
    const dst = y * rowSize
    for (let x = 0; x < width; x++) {
      pixels[dst + x * 3] = rgb[src + x * 3 + 2] // B
      pixels[dst + x * 3 + 1] = rgb[src + x * 3 + 1] // G
      pixels[dst + x * 3 + 2] = rgb[src + x * 3] // R
    }
  }
  const file = Buffer.alloc(14)
  const info = Buffer.alloc(40)
  file.write('BM', 0, 'ascii')
  file.writeUInt32LE(14 + 40 + pixels.length, 2)
  file.writeUInt32LE(14 + 40, 10)
  info.writeUInt32LE(40, 0)
  info.writeInt32LE(width, 4)
  info.writeInt32LE(height, 8)
  info.writeUInt16LE(1, 12)
  info.writeUInt16LE(24, 14)
  info.writeUInt32LE(0, 16) // BI_RGB
  info.writeUInt32LE(pixels.length, 20)
  info.writeInt32LE(2835, 24) // 72 DPI
  info.writeInt32LE(2835, 28)
  return Buffer.concat([file, info, pixels])
}

const staging = mkdtempSync(join(tmpdir(), 'cw-icons-'))
try {
  console.log(`rasterizing ${SVG}`)
  const render = spawnSync(
    'uv',
    ['run', '--project', join(ROOT, 'backend'), 'python', join(DESKTOP, 'scripts', 'render-brand.py'), SVG, staging],
    { stdio: 'inherit', shell: true },
  )
  if (render.status !== 0) die('rasterization failed (is the backend environment set up? run `uv sync` in backend/)')

  mkdirSync(OUT, { recursive: true })
  const images = ICO_SIZES.map((size) => ({ size, data: readFileSync(join(staging, `icon-${size}.png`)) }))
  writeFileSync(join(OUT, 'icon.ico'), buildIco(images))

  const sidebar = readFileSync(join(staging, 'sidebar.rgb'))
  const header = readFileSync(join(staging, 'header.rgb'))
  if (sidebar.length !== SIDEBAR.width * SIDEBAR.height * 3) die('sidebar.rgb has an unexpected size')
  if (header.length !== HEADER.width * HEADER.height * 3) die('header.rgb has an unexpected size')
  writeFileSync(join(OUT, 'installerSidebar.bmp'), buildBmp(sidebar, SIDEBAR.width, SIDEBAR.height))
  writeFileSync(join(OUT, 'installerHeader.bmp'), buildBmp(header, HEADER.width, HEADER.height))

  console.log(`wrote ${OUT}:`)
  console.log(`  icon.ico (${ICO_SIZES.join(', ')} px)`)
  console.log(`  installerSidebar.bmp (${SIDEBAR.width}x${SIDEBAR.height})`)
  console.log(`  installerHeader.bmp (${HEADER.width}x${HEADER.height})`)
} finally {
  rmSync(staging, { recursive: true, force: true })
}
