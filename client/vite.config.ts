import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

// Stamps a visible build-id comment into index.html, so it's obvious in
// devtools which version is loaded. The __PHARMAKON_BUILD_ID__ placeholder in
// public/sw.js is stamped separately, by scripts/stamp-sw.mjs (a `postbuild`
// step) -- public/ is copied into dist/ by Vite's own internal build step,
// which runs after every plugin hook, so any in-plugin edit to dist/sw.js
// gets clobbered by that copy. A separate script that runs after `vite
// build` has fully exited is the only place that sticks.
function buildStamp(command: 'build' | 'serve'): Plugin {
  const buildId =
    command === 'build' ? new Date().toISOString().replace(/[:.]/g, '-') : 'dev'

  return {
    name: 'pharmakon-build-stamp',
    transformIndexHtml(html) {
      return html.replace(
        '<head>',
        `<head>\n    <!-- pharmakon build: ${buildId} -->`,
      )
    },
  }
}

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  plugins: [react(), buildStamp(command)],
}))
