import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './index.css'
import { installAuthFetchInterceptor } from './lib/auth-fetch'

// Install the global 401 interceptor before anything else fetches.
// Idempotent under Vite HMR.
installAuthFetchInterceptor()

const rootElement: HTMLElement =
  document.getElementById('root') ??
  (() => {
    throw new Error('Root element not found')
  })()

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
