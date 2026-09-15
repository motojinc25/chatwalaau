'use strict'

// Bootstrap screen renderer (CTR-0214). Talks to Main only through the preload's
// window.desktop API; no Node access.

const $ = (id) => document.getElementById(id)

const LABELS = {
  BOOT: 'Starting',
  PRECHECK: 'Checking the installation',
  PREPARE_RUNTIME: 'Preparing Python',
  PREPARE_ENV: 'Setting up the environment',
  START_BACKEND: 'Starting',
  WAIT_READY: 'Almost ready',
  RUNNING: 'Ready',
  STOPPING: 'Stopping',
  STOPPED: 'Stopped',
  ERROR: 'ChatWalaʻau could not start',
}

let busy = false

function render(state) {
  if (!state) return
  const error = state.phase === 'ERROR' ? state.error : null
  $('phase').textContent = LABELS[state.phase] || state.phase
  $('message').textContent = error ? '' : state.message || ''
  $('hint').hidden = state.phase !== 'PREPARE_ENV'
  $('spinner').hidden = Boolean(error) || state.phase === 'RUNNING' || state.phase === 'STOPPED'
  $('error').hidden = !error
  $('rebuild').hidden = Boolean(state.devMode)
  if (error) {
    $('error-code').textContent = error.code
    $('error-message').textContent = error.message
    $('error-detail').textContent = error.detail || ''
    $('error-detail').hidden = !error.detail
    busy = false
  }
}

window.desktop.onState(render)
window.desktop.getState().then(render)

function once(action) {
  return () => {
    if (busy) return
    busy = true
    action()
  }
}

$('retry').addEventListener('click', once(() => window.desktop.retry()))
$('rebuild').addEventListener('click', once(() => window.desktop.rebuildEnvironment()))
$('logs').addEventListener('click', () => window.desktop.openLogs())
$('quit').addEventListener('click', () => window.desktop.quit())
