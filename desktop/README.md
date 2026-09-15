# ChatWalaʻau Desktop (Windows)

An Electron shell that installs and runs the **unchanged** ChatWalaʻau web app as a
Windows desktop application. It bundles its own Python runtime, so users install nothing
else. User documentation: <https://www.chatwalaau.com/docs/getting-started/desktop>.

One artifact, the **x64** installer, serves both x64 and Windows on ARM devices -- the
latter through Windows' x64 emulation (not yet validated on ARM hardware). There is no
native arm64 build: 13 locked dependencies publish no `win_arm64` wheel, and the
environment must be installed offline from hash-locked wheels. Check with
`pnpm arm64:readiness`.

## How it works

```text
Electron Main (src/main)
  +-- builds a private Python venv offline from resources/desktop-payload (first run / update)
  +-- starts python/backend_launcher.py with that venv
  |     +-- Windows Job Object, binds 127.0.0.1, serves app.main:app behind desktop_guard.py
  +-- shows http://127.0.0.1:<port>/chat in a sandboxed window
  +-- electron-updater (GitHub Releases): silent for signed builds, notify-only when unsigned
```

Data lives in `%LOCALAPPDATA%\ChatWalaau\desktop\` (`profile\` holds `.env`, settings,
sessions, uploads and RAG data). The installer goes to `%LOCALAPPDATA%\Programs\ChatWalaau`.

## Development

Prerequisites: Windows 10/11 x64 (development and the payload build both resolve wheels
for the host), Node >= 22.12, pnpm, uv, and the backend set up (`cd backend && uv sync`).

```bash
cd frontend && pnpm build          # the SPA served in dev mode
cd ../desktop
pnpm install
pnpm dev:full                      # Azure check + Electron + backend/.venv; data in desktop/.dev-profile/
pnpm check                         # tsc --noEmit + vitest
```

Development mode never builds a venv: it runs the launcher with `backend/.venv`. Put a
`.env` in `desktop/.dev-profile/desktop/profile/` (created from the template on first run).

The script is named `dev:full` for the same reason the frontend's is: it starts the whole
stack (backend through the launcher, plus the Electron window), not one part of it.

`pnpm dev:full` verifies the Azure CLI sign-in first, like the frontend's `dev:full`: when the
credential lane is `cli` and `AZURE_TENANT_ID` is set (dev profile `.env`, else
`backend/.env`, else the process environment), it probes for a Cognitive Services token in
that tenant and runs `az login --tenant <id>` only if the probe fails. It is skipped for
the API-key and managed-identity lanes, and with `--skip-auth-check` or
`CW_SKIP_AZURE_CHECK=1`. `pnpm dev:azure-check` reports the state without signing in.
The packaged app never runs this check.

## Build and release

```bash
cd frontend && pnpm run version:set --target product --to X.Y.Z   # also writes desktop/package.json
cd ../desktop
pnpm payload:build     # pinned CPython + uv + win_amd64 wheelhouse + manifest + offline trial install
pnpm dist              # payload:verify + NSIS installer in dist/
pnpm release           # after pypi:publish, github:sync, github:release for the same version
```

`pnpm release` uploads `ChatWalaau-Setup-X.Y.Z.exe`, its `.blockmap` and `latest.yml` to
the GitHub release `vX.Y.Z` with the `gh` CLI.

| Build-time variable | Purpose |
| --- | --- |
| `CSC_LINK` / `WIN_CSC_LINK`, `CSC_KEY_PASSWORD` | Sign with a PFX certificate |
| `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_SIGNING_PROFILE` + `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Sign with Azure Trusted Signing |
| `CW_SIGN_PUBLISHER_NAME` | Certificate subject CN (default `WeDX Digital Twins Solutions`) |
| `GH_TOKEN` | Optional token for `gh` |

The window, taskbar and installer images come from `frontend/public/favicon.svg`. They are
committed under `build-resources/`; after changing the favicon run `pnpm icons` (it needs
the backend environment, `uv sync` in `backend/`) and commit the result.

Without signing variables the build is **unsigned**: Windows SmartScreen warns on install
and the app only *notifies* about updates instead of installing them.

## Layout

| Path | What |
| --- | --- |
| `src/main/` | Electron Main: environment, launcher supervision, windows, updater, diagnostics |
| `src/preload/bootstrap.ts` | Preload of the startup window only (the chat window has none) |
| `bootstrap-ui/` | Startup / error screen |
| `python/` | `backend_launcher.py`, `desktop_guard.py` (run inside the bundled venv) |
| `scripts/` | Payload build / verify, release, pinned binaries, signing, icon generation |
| `build-resources/` | Generated Windows images (`pnpm icons`): app icon + installer bitmaps |
| `tests/` | vitest unit tests |
