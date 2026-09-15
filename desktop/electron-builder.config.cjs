/**
 * electron-builder configuration (CTR-0213, UDR-0151 D2/D11).
 *
 * JavaScript instead of YAML so the signing block is added only when signing credentials
 * are present: an unsigned build must not claim a publisherName, or electron-updater
 * would reject every update of it.
 */

const { signingConfig } = require('./scripts/signing.cjs')

const signing = signingConfig(process.env)

const win = {
  target: [{ target: 'nsis', arch: ['x64'] }],
  executableName: 'ChatWalaau',
  // Generated from frontend/public/favicon.svg by `pnpm icons` (brand-assets.md).
  icon: 'build-resources/icon.ico',
}
if (signing.mode === 'azure') win.azureSignOptions = signing.azureSignOptions
if (signing.mode === 'certificate') win.signtoolOptions = { publisherName: signing.publisherName }

module.exports = {
  // Stable identity -- never change after the first release (PRP-0167 Q2).
  appId: 'cc.wedx.chatwalaau',
  productName: 'ChatWalaau',
  copyright: 'Copyright (c) WeDX Digital Twins Solutions',
  directories: { output: 'dist', buildResources: 'build-resources' },
  files: ['build/**/*', 'package.json', '!**/*.map'],
  asar: true,
  // The payload is executed from disk, never from app.asar (RES-0004 section 5.1).
  extraResources: [{ from: 'resources/generated/desktop-payload', to: 'desktop-payload' }],
  win,
  nsis: {
    oneClick: false,
    perMachine: false,
    allowElevation: false,
    allowToChangeInstallationDirectory: false,
    deleteAppDataOnUninstall: false,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: 'ChatWalaʻau',
    uninstallDisplayName: 'ChatWalaʻau ${version}',
    artifactName: 'ChatWalaau-Setup-${version}.${ext}',
    differentialPackage: true,
    installerIcon: 'build-resources/icon.ico',
    uninstallerIcon: 'build-resources/icon.ico',
    installerSidebar: 'build-resources/installerSidebar.bmp',
    uninstallerSidebar: 'build-resources/installerSidebar.bmp',
    installerHeader: 'build-resources/installerHeader.bmp',
  },
  publish: [{ provider: 'github', owner: 'motojinc25', repo: 'chatwalaau', releaseType: 'release' }],
}
