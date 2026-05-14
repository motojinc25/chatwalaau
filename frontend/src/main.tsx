import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './index.css'

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
