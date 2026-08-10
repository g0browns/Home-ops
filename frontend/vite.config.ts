import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server is the single origin during development: it serves the app and
// forwards /api to the backend, mirroring what nginx does in production. That is
// what lets the frontend use relative paths everywhere (SPEC §2.1) and behave
// identically over all three access paths.
//
// Inside compose the backend is `api`. Running Vite natively on the workstation
// instead? Set API_PROXY_TARGET=http://localhost:8000.
const apiProxyTarget = process.env.API_PROXY_TARGET ?? 'http://api:8000'

// Hostnames the dev server will answer to, beyond localhost and IPs.
//
// Vite refuses a request whose `Host` it does not recognise -- a DNS-rebinding
// protection -- so reaching the *dev* server through a tunnel gets "Blocked
// request. This host is not allowed." rather than the app. Driven by an
// environment variable so a personal hostname lives in `.env` rather than in
// the repository.
//
// This applies to `vite dev` only. Production is nginx, which does not consult
// it, and where the equivalent control is `TRUSTED_ORIGINS`.
const devHosts = (process.env.DEV_ALLOWED_HOSTS ?? '')
  .split(',')
  .map((host) => host.trim())
  .filter(Boolean)

export default defineConfig({
  plugins: [react()],
  server: {
    // Reachable from outside the container, and from the tailnet and LAN.
    host: true,
    port: 8080,
    strictPort: true,
    allowedHosts: devHosts,
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: false,
      },
      // A '/homeops-mobile' proxy sat here until 2026-08-09, pointing at the
      // `mobile` container. A native Android app replaced the PWA, so the path
      // falls through to this app's SPA fallback like any other unknown one.
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
