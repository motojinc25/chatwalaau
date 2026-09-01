import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// TLS / HTTPS support (PRP-0029)
const sslCertfile = process.env.APP_SSL_CERTFILE || ''
const sslKeyfile = process.env.APP_SSL_KEYFILE || ''
const sslEnabled = !!(sslCertfile && sslKeyfile && existsSync(sslCertfile) && existsSync(sslKeyfile))

const backendProtocol = sslEnabled ? 'https' : 'http'
const backendTarget = `${backendProtocol}://localhost:${process.env.APP_PORT || '8000'}`

export default defineConfig({
	plugins: [react()],
	resolve: {
		alias: {
			// `import.meta.dirname`, not `__dirname`: Vite's `configLoader: 'native'`
			// (planned to become the default) loads this file as a real ES module, where
			// `__dirname` does not exist. Under the current 'bundle' loader Vite defines
			// BOTH names to the same value -- the original config file's directory -- so
			// the resolved alias is byte-for-byte unchanged either way.
			'@': resolve(import.meta.dirname, './src'),
		},
	},
	server: {
		host: sslEnabled ? '0.0.0.0' : undefined,
		https: sslEnabled
			? {
					cert: readFileSync(sslCertfile),
					key: readFileSync(sslKeyfile),
				}
			: undefined,
		proxy: {
			'/api': {
				target: backendTarget,
				changeOrigin: true,
				secure: false,
			},
			'/ag-ui': {
				target: backendTarget,
				changeOrigin: true,
				secure: false,
			},
			// Server Notification Channel WebSocket (CTR-0110, PRP-0077). Needs
			// ws:true so Vite forwards the upgrade handshake to the backend during
			// `dev:full`; without it the real-time title push never connects.
			'/ws': {
				target: backendTarget,
				changeOrigin: true,
				secure: false,
				ws: true,
			},
		},
	},
	build: {
		outDir: 'dist',
	},
})
