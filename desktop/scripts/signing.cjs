/**
 * Code-signing mode from the build environment (PRP-0167 Q1, UDR-0151 D11).
 *
 * - azure:       Azure Trusted Signing. Needs AZURE_SIGNING_ENDPOINT, AZURE_SIGNING_ACCOUNT,
 *                AZURE_SIGNING_PROFILE plus the service principal (AZURE_TENANT_ID,
 *                AZURE_CLIENT_ID, AZURE_CLIENT_SECRET) that electron-builder reads.
 * - certificate: a PFX via CSC_LINK / WIN_CSC_LINK (+ CSC_KEY_PASSWORD).
 * - none:        unsigned. The installed app then only NOTIFIES about updates.
 *
 * CW_SIGN_PUBLISHER_NAME must equal the certificate subject CN exactly; electron-updater
 * refuses an update whose signature does not match it.
 */

function signingConfig(env) {
  const publisherName = env.CW_SIGN_PUBLISHER_NAME || 'WeDX Digital Twins Solutions'
  if (env.AZURE_SIGNING_ENDPOINT && env.AZURE_SIGNING_ACCOUNT && env.AZURE_SIGNING_PROFILE) {
    return {
      mode: 'azure',
      publisherName,
      azureSignOptions: {
        publisherName,
        endpoint: env.AZURE_SIGNING_ENDPOINT,
        codeSigningAccountName: env.AZURE_SIGNING_ACCOUNT,
        certificateProfileName: env.AZURE_SIGNING_PROFILE,
      },
    }
  }
  if (env.CSC_LINK || env.WIN_CSC_LINK) return { mode: 'certificate', publisherName }
  return { mode: 'none', publisherName: undefined }
}

module.exports = { signingConfig }
