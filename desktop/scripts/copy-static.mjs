/**
 * Post-tsc build step: copy the bootstrap UI next to the compiled Main code and record
 * whether this build is signed (read at runtime by updater.ts, UDR-0151 D11).
 */

import { copyFileSync, cpSync, mkdirSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const require = createRequire(import.meta.url)
const { signingConfig } = require('./signing.cjs')

const DESKTOP = resolve(import.meta.dirname, '..')
const BUILD = resolve(DESKTOP, 'build')

mkdirSync(BUILD, { recursive: true })
cpSync(resolve(DESKTOP, 'bootstrap-ui'), resolve(BUILD, 'bootstrap-ui'), { recursive: true })
// The startup screen shows the product brand mark; keep ONE source (brand-assets.md).
copyFileSync(resolve(DESKTOP, '..', 'frontend', 'public', 'favicon.svg'), resolve(BUILD, 'bootstrap-ui', 'favicon.svg'))

const signing = signingConfig(process.env)
const info = { signed: signing.mode !== 'none', signingMode: signing.mode, builtAt: new Date().toISOString() }
writeFileSync(resolve(BUILD, 'build-info.json'), `${JSON.stringify(info, null, 2)}\n`)
console.log(`build: bootstrap-ui copied; signing mode = ${signing.mode}`)
