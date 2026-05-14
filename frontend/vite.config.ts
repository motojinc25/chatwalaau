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
			'@': resolve(__dirname, './src'),
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
		},
	},
	build: {
		outDir: 'dist',
	},
})
